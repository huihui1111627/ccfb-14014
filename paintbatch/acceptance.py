"""Command line: run scenarios / recipes and write the reports.

Examples
--------
Run the whole acceptance catalogue (twelve recipes, holds, restarts, aborts,
fault injection) and write records + progress pages + a summary::

    python -m paintbatch.acceptance --out reports

Run one scenario::

    python -m paintbatch.acceptance --scenario S3 --out reports

Run a recipe on its own::

    python -m paintbatch.acceptance --recipe P-101 --out reports

Serve the progress page live (re-renders while the run executes)::

    python -m paintbatch.acceptance --serve 8000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .record import write_batch_record
from .runner import Runner, Scenario
from .scenarios import catalogue
from .ui import progress_html, write_progress
from .verify import verify

REPO = Path(__file__).resolve().parent.parent


def _find(scenario_id: str) -> Scenario:
    """Match "S12", "s12", "S1" (exact id) before substring matches."""
    want = scenario_id.strip().lower()
    for sc in catalogue():
        sid = sc.name.split()[0].lower()
        if sid == want:
            return sc
    for sc in catalogue():
        if want in sc.name.lower():
            return sc
    raise SystemExit(f"no scenario matching {scenario_id!r}")


def single_recipe_scenario(recipe_id: str) -> Scenario:
    return Scenario(
        name=f"single {recipe_id}",
        description=f"{recipe_id} alone on the plant",
        batches=[dict(recipe=recipe_id, batch_id=f"{recipe_id}-B",
                      lot=f"LOT-{recipe_id}", priority=5)],
        max_ms=1_800_000,
        expected_states={f"{recipe_id}-B": "COMPLETE"},
    )


def run_one(sc, out: Path, write: bool = True) -> dict:
    run = Runner(sc).run()
    v = verify(run)
    if not write:
        return {"scenario": sc.name, "verification": v, "run": run}
    out.mkdir(parents=True, exist_ok=True)
    slug = sc.name.split()[0]
    if not any(ch.isdigit() for ch in slug):
        slug = sc.name.replace(" ", "-")
    links = {}
    for bid, b in run.batches.items():
        report = b.final_report
        path = write_batch_record(report, out / "records", v)
        links[bid] = f"records/{path.name}"
    progress = out / f"{slug}-progress.html"
    write_progress(run, progress, v, links=links)
    (out / f"{slug}-verification.json").write_text(
        json.dumps(v, indent=2, default=str), encoding="utf-8")
    return {"scenario": sc.name, "slug": slug, "verification": v,
            "progress": str(progress), "run": run}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=None, help="output directory")
    ap.add_argument("--scenario", action="append", default=None,
                    help="scenario id prefix, repeatable")
    ap.add_argument("--recipe", action="append", default=None,
                    help="run a single recipe, repeatable")
    ap.add_argument("--all-recipes", action="store_true",
                    help="run every recipe on its own")
    ap.add_argument("--serve", type=int, default=None, metavar="PORT",
                    help="serve a live progress page")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.serve:
        return _serve(args.serve, args.out or REPO / "reports")

    scenarios: List[Scenario] = []
    if args.recipe:
        scenarios += [single_recipe_scenario(r) for r in args.recipe]
    if args.all_recipes:
        from .recipes import all_recipes
        scenarios += [single_recipe_scenario(r)
                      for r in sorted(all_recipes())]
    if args.scenario:
        scenarios += [_find(s) for s in args.scenario]
    if not scenarios:
        scenarios = catalogue()

    out = Path(args.out) if args.out else REPO / "reports"
    results = []
    failures = 0
    for sc in scenarios:
        res = run_one(sc, out)
        v = res["verification"]
        failures += 0 if v["ok"] else 1
        results.append({
            "scenario": sc.name,
            "ok": v["ok"],
            "duration": v["duration"],
            "stopped_reason": v["stopped_reason"],
            "batches": v["batches"],
            "failures": v["failures"],
            "progress": res.get("progress"),
        })
        if not args.quiet:
            mark = "PASS" if v["ok"] else "FAIL"
            print(f"[{mark}] {sc.name}  ({v['duration']}, "
                  f"{v['stopped_reason']})")
            for f in v["failures"]:
                print("       -", f)

    (out / "acceptance-summary.json").write_text(
        json.dumps({"results": results, "failures": failures},
                   indent=2, default=str), encoding="utf-8")
    if not args.quiet:
        print(f"\n{len(results) - failures}/{len(results)} scenarios passed; "
              f"reports in {out}")
    return 0 if failures == 0 else 1


def _serve(port: int, out: Path) -> int:
    """Live progress page: runs the S2 scenario in a background thread and
    re-renders on every request.  Static output is still written to ``out``."""
    import http.server
    import socketserver
    import threading
    import time

    sc = _find("S2")
    run = Runner(sc).build()
    runner = Runner.__new__(Runner)
    runner.scenario = sc
    runner.plant = run.plant
    runner.engine = run.engine
    runner.run_obj = run
    runner.pending_hooks = []
    state = {"done": False, "verification": None}

    def worker():
        while not run.done() and run.steps < sc.max_ms // sc.tick_ms:
            run.engine.tick()
            run.steps += 1
            if run.steps % 5 == 0:
                run.samples.append(runner.snapshot(run))
            time.sleep(0.01)
        run.samples.append(runner.snapshot(run))
        run.report()
        state["verification"] = verify(run)
        state["done"] = True

    threading.Thread(target=worker, daemon=True).start()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.rstrip("/") in ("", "/progress", "/index.html"):
                body = progress_html(run, state["verification"]).encode("utf-8")
                ctype = "text/html; charset=utf-8"
            elif self.path.startswith("/records/"):
                name = self.path.split("/", 2)[2]
                from .record import batch_record_html
                bid = name.replace("batch-", "").replace(".html", "")
                batch = run.batches.get(bid)
                body = (batch_record_html(batch.final_report,
                                          state["verification"])
                        if batch and batch.final_report else b"not ready")
                ctype = "text/html; charset=utf-8"
            else:
                body = b"not found"
                ctype = "text/plain"
                self.send_response(404)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        print(f"serving http://127.0.0.1:{port}/  (ctrl-c to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
