"""Batch record: one printable HTML document per batch.

The record is the production document a paint plant keeps for traceability.
It contains, in this order:

1. header - batch id, recipe and version, product, lot, size, start/end,
   final state, priority/queue position;
2. executive summary - phases run, holds, faults, alarms, commands;
3. the *state transition table* of every phase, with times and reasons;
4. the operator / scripted command log with accepted/rejected outcomes;
5. material movements per line (who carried what, when) - the evidence
   that no line ever carried two batches;
6. resource reservations (acquire/release, including safe release on abort);
7. the raw event log;
8. the verification result for this batch.

The HTML is self-contained (no external assets) and print-friendly.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import List, Optional

from .sim import fmt_ms

CSS = """
:root { --ink:#1d232a; --muted:#66727d; --line:#d8dee4; --ok:#1a7f37;
        --warn:#b35900; --bad:#b42318; --bg:#fbfcfd; }
* { box-sizing:border-box; }
body { font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
       "Helvetica Neue",Arial,sans-serif; color:var(--ink); background:#fff;
       margin:0; padding:28px 34px; max-width:1180px; }
h1 { font-size:22px; margin:0 0 2px; letter-spacing:-.2px; }
h2 { font-size:15px; margin:26px 0 8px; padding-bottom:4px;
     border-bottom:2px solid var(--ink); text-transform:uppercase;
     letter-spacing:.06em; }
h3 { font-size:13px; margin:16px 0 6px; }
.sub { color:var(--muted); margin-bottom:16px; }
.meta { display:grid; grid-template-columns:repeat(4,1fr); gap:8px 22px;
        margin:12px 0 4px; }
.meta div { border-left:3px solid var(--line); padding-left:8px; }
.meta b { display:block; font-size:11px; color:var(--muted);
          text-transform:uppercase; letter-spacing:.05em; font-weight:600; }
.meta span { font-size:14px; }
table { border-collapse:collapse; width:100%; margin:6px 0 4px; }
th,td { text-align:left; padding:4px 8px; border-bottom:1px solid var(--line);
        vertical-align:top; }
th { background:#f2f5f8; font-size:11px; text-transform:uppercase;
     letter-spacing:.05em; color:var(--muted); }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
tr:nth-child(even) td { background:#fafbfc; }
.badge { display:inline-block; padding:1px 7px; border-radius:9px;
         font-size:11px; font-weight:600; letter-spacing:.02em; }
.ok { background:#e6f4ea; color:var(--ok); }
.warn { background:#fff4e5; color:var(--warn); }
.bad { background:#fde8e6; color:var(--bad); }
.state { font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
         font-size:12px; }
.state.ok2 { color:var(--ok); } .state.held { color:var(--warn); }
.state.bad2 { color:var(--bad); }
.summary { display:grid; grid-template-columns:repeat(5,1fr); gap:10px;
           margin:10px 0; }
.card { border:1px solid var(--line); border-radius:6px; padding:8px 10px; }
.card b { display:block; font-size:20px; line-height:1.2; }
.card span { font-size:11px; color:var(--muted); text-transform:uppercase;
             letter-spacing:.05em; }
.trans td { font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
            font-size:12px; }
footer { margin-top:28px; color:var(--muted); font-size:11px;
         border-top:1px solid var(--line); padding-top:8px; }
.pass { color:var(--ok); font-weight:700; }
.fail { color:var(--bad); font-weight:700; }
@media print { body{padding:0;max-width:none;} h2{page-break-after:avoid;}
               table{page-break-inside:auto;} tr{page-break-inside:avoid;} }
"""

GOOD = {"COMPLETE"}
BAD = {"ABORTED"}
WARN = {"HELD", "HOLDING", "STOPPED", "STOPPING", "ABORTING", "RESTARTING"}


def e(x) -> str:
    return html.escape("" if x is None else str(x))


def badge(state: str) -> str:
    cls = "ok" if state in GOOD else "bad" if state in BAD else "warn"
    return f'<span class="badge {cls}">{e(state)}</span>'


def state_cls(state: str) -> str:
    if state in ("COMPLETE", "RUNNING"):
        return "state ok2"
    if state in ("HELD", "HOLDING", "RESTARTING"):
        return "state held"
    if state in ("ABORTED", "ABORTING", "STOPPED"):
        return "state bad2"
    return "state"


def _short(ms) -> str:
    return fmt_ms(ms) if ms is not None else ""


# --------------------------------------------------------------------------
def batch_record_html(report: dict, verification: Optional[dict] = None,
                      events_limit: int = 4000) -> str:
    bid = report["batch_id"]
    rec = report["recipe"]
    events = report.get("events", [])
    alarms = report.get("alarms", [])
    # a record can be pulled at any time; if the batch is still running the
    # caller supplies the live phase log
    phases = report.get("phases") or report.get("live_phases", [])
    held_end = report.get("held_resources_at_end", [])
    out: List[str] = []
    w = out.append

    w("<!doctype html><html><head><meta charset='utf-8'>")
    w(f"<title>Batch record {e(bid)} - {e(rec['id'])}</title>")
    w(f"<style>{CSS}</style></head><body>")

    # -- header -----------------------------------------------------------
    w(f"<h1>Batch record &nbsp;<span class='state'>{e(bid)}</span></h1>")
    w("<div class='sub'>Paint batch control &mdash; ISA-88 recipe execution "
      "record</div>")
    w("<div class='meta'>")
    for label, value in [
        ("Recipe", f"{rec['id']} v{rec['version']} &ndash; {rec['name']}"),
        ("Product", rec["product"]),
        ("Lot", report["lot"]),
        ("Batch size", f"{rec['batch_size_kg']:.0f} kg"),
        ("Started", report["started"]),
        ("Finished", report["finished"] or "&mdash;"),
        ("Final state", badge(report["state"])),
        ("Priority / queue", f"p{report['priority']} / #{report['sequence']}"),
    ]:
        w(f"<div><b>{label}</b><span>{value}</span></div>")
    w("</div>")

    # -- summary ----------------------------------------------------------
    holds = sum(1 for p in phases
                for t in p["transitions"] if t["to"] == "HELD")
    faults = [ev for ev in events if ev["category"] == "fault"]
    pulls = [ev for ev in events if ev["event"] == "transfer_done"]
    w("<h2>Summary</h2><div class='summary'>")
    for label, value in [
        ("Phases", str(len(phases))),
        ("Holds", str(holds)),
        ("Faults", str(len(faults))),
        ("Alarms", str(len(alarms))),
        ("Transfers", str(len(pulls))),
    ]:
        w(f"<div class='card'><b>{value}</b><span>{label}</span></div>")
    w("</div>")

    if verification is not None:
        overall = verification.get("ok")
        w("<p>verification: "
          f"<span class='{'pass' if overall else 'fail'}'>"
          f"{'PASS' if overall else 'FAIL'}</span> ")
        if not overall:
            w("<ul>" + "".join(f"<li>{e(f)}</li>"
                               for f in verification.get("failures", [])[:12])
              + "</ul>")
        w("</p>")

    # -- phase transitions ------------------------------------------------
    w("<h2>Phase state transitions</h2>")
    w("<p class='sub'>Every phase, its equipment, the propagation rule it "
      "was written with, every transition with time and reason.</p>")
    w("<table class='trans'><tr><th>Phase</th><th>Equipment</th><th>Prop.</th>"
      "<th>Final</th><th>Transitions (time - from - to - reason)</th></tr>")
    for p in phases:
        chain = " &rarr; ".join(
            f"{_short(t['at'])} <span class='state'>{e(t['from'])}</span>&rarr;"
            f"<span class='state'>{e(t['to'])}</span> <i>{e(t['reason'])}</i>"
            for t in p["transitions"])
        w(f"<tr><td>{e(p['path'])}</td><td>{e(p['equipment'])}</td>"
          f"<td>{e(p['propagation'])}</td><td>{badge(p['state'])}</td>"
          f"<td>{chain}</td></tr>")
    w("</table>")

    # -- commands ---------------------------------------------------------
    w("<h2>Operator commands</h2>")
    cmds = [ev for ev in events if ev["category"] == "command"]
    if not cmds:
        w("<p class='sub'>No operator commands were issued.</p>")
    else:
        w("<table><tr><th>Time</th><th>Command</th><th>Target</th>"
          "<th>Result</th><th>Reason</th></tr>")
        for ev in cmds:
            res = ("<span class='badge ok'>ACCEPTED</span>"
                   if ev.get("accepted") or ev["event"] != "command_rejected"
                   else "<span class='badge bad'>REJECTED</span>")
            when = ev["time"]
            w(f"<tr><td>{when}</td><td>{e(ev.get('command'))}</td>"
              f"<td>{e(ev.get('path') or ev.get('targets'))}</td>"
              f"<td>{res}</td><td>{e(ev.get('reason') or ev.get('text'))}"
              f"</td></tr>")
        w("</table>")

    # -- material movements ----------------------------------------------
    w("<h2>Material movements</h2>")
    w("<p class='sub'>Every transfer through a shared line, as recorded by "
      "the line itself. Each line carries one batch at a time.</p>")
    w("<table><tr><th>Line</th><th>From</th><th>To</th><th>Start</th>"
      "<th>End</th><th class='num'>kg</th><th>Batch</th><th>Material</th>"
      "</tr>")
    lines = report.get("line_spans", [])
    if not lines:
        w("<tr><td colspan='8' class='sub'>No transfers recorded.</td></tr>")
    for s in lines:
        w(f"<tr><td>{e(s['line'])}</td><td>{e(s['from'])}</td>"
          f"<td>{e(s['to'])}</td><td>{_short(s['start'])}</td>"
          f"<td>{_short(s['end'])}</td>"
          f"<td class='num'>{s['kg']:.1f}</td><td>{e(s['batch'])}</td>"
          f"<td>{e(s['material'])}</td></tr>")
    w("</table>")

    # -- resources --------------------------------------------------------
    w("<h2>Shared-resource reservations</h2>")
    w("<table><tr><th>Time</th><th>Action</th><th>Resource</th>"
      "<th>Batch</th></tr>")
    for r in report.get("reservations", []):
        cls = "ok" if r["action"] == "ACQUIRE" else "warn"
        w(f"<tr><td>{r['time']}</td>"
          f"<td><span class='badge {cls}'>{e(r['action'])}</span></td>"
          f"<td>{e(r['resource'])}</td><td>{e(r['batch'])}</td></tr>")
    w("</table>")

    # -- faults and alarms -----------------------------------------------
    w("<h2>Faults and alarms</h2>")
    if not faults and not alarms:
        w("<p class='sub'>None.</p>")
    else:
        w("<table><tr><th>Time</th><th>Kind</th><th>Detail</th></tr>")
        for ev in events:
            if ev["category"] not in ("fault", "alarm"):
                continue
            w(f"<tr><td>{ev['time']}</td><td>{e(ev['event'])}</td>"
              f"<td>{e(ev.get('text') or ev.get('reason'))}</td></tr>")
        w("</table>")

    # -- process values ---------------------------------------------------
    w("<h2>Final process values</h2>")
    w("<table><tr><th>Tank</th><th class='num'>kg</th>"
      "<th class='num'>temp &deg;C</th><th>Contents</th></tr>")
    for name, t in report.get("tanks", {}).items():
        w(f"<tr><td>{e(name)}</td><td class='num'>{t['kg']:.1f}</td>"
          f"<td class='num'>{t['temp_c']:.1f}</td>"
          f"<td>{e(t['contents'])}</td></tr>")
    w("</table>")

    # -- raw event log ----------------------------------------------------
    w("<h2>Event log</h2>")
    w(f"<p class='sub'>{len(events)} events"
      f"{' (first %d shown)' % events_limit if len(events) > events_limit else ''}"
      "</p>")
    w("<table><tr><th>Time</th><th>Category</th><th>Event</th>"
      "<th>Detail</th></tr>")
    for ev in events[:events_limit]:
        detail = {k: v for k, v in ev.items()
                  if k not in ("t", "time", "batch", "category", "event")}
        w(f"<tr><td>{ev['time']}</td><td>{e(ev['category'])}</td>"
          f"<td>{e(ev['event'])}</td>"
          f"<td><span class='state'>{e(json.dumps(detail, default=str))}"
          "</span></td></tr>")
    w("</table>")

    if held_end:
        w(f"<footer class='fail'>Resources still held at end: "
          f"{e(', '.join(held_end))}</footer>")
    w("<footer>Generated by the paint batch control simulation. "
      "All timestamps are simulation time (hh:mm:ss.mmm).</footer>")
    w("</body></html>")
    return "\n".join(out)


# --------------------------------------------------------------------------
def write_batch_record(report: dict, directory, verification=None) -> Path:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"batch-{report['batch_id']}.html"
    path.write_text(batch_record_html(report, verification), encoding="utf-8")
    return path
