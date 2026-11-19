"""Progress UI: a self-contained HTML view of a run.

Generates one page with

* a Gantt-style timeline per batch: every phase as a bar coloured by its
  state, hold / restart / abort markers, fault markers;
* the live recipe tree with each element's aggregated state and progress;
* the plant: which batch holds each line, mass and temperature readings;
* the command log and fault injections;
* links to the per-batch records.

The page is static, no JavaScript required to read it.  ``--serve`` in
``ui.py``'s CLI can wrap it in a tiny HTTP server that re-renders on each
request while a run is in progress.
"""

from __future__ import annotations

import html
from typing import Dict, List, Optional

from .sim import fmt_ms

CSS = """
:root{--ink:#13191f;--muted:#647079;--line:#dbe1e7;--bg:#f7f9fb;
      --ok:#1a7f37;--run:#0b6bcb;--hold:#b35900;--bad:#b42318;
      --idle:#c9d2da;--done:#2f7d4f;}
*{box-sizing:border-box}
body{margin:0;font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",
     Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg);}
header{background:#101820;color:#fff;padding:14px 26px;}
header h1{margin:0;font-size:18px;font-weight:650;letter-spacing:-.2px}
header .meta{color:#9fb0bf;font-size:12px;margin-top:3px}
main{padding:18px 26px 40px;max-width:1500px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;
   color:var(--muted);margin:22px 0 8px;border-bottom:1px solid var(--line);
   padding-bottom:4px}
.card{background:#fff;border:1px solid var(--line);border-radius:8px;
      padding:14px 16px;margin-bottom:14px}
.batch-head{display:flex;justify-content:space-between;align-items:baseline;
            gap:12px;margin-bottom:10px;flex-wrap:wrap}
.batch-head h3{margin:0;font-size:15px}
.batch-head .r{color:var(--muted);font-size:12px}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;
       font-weight:650}
.b-complete{background:#e6f4ea;color:var(--ok)}
.b-running{background:#e7f0fb;color:var(--run)}
.b-held{background:#fff3e2;color:var(--hold)}
.b-aborted{background:#fde8e6;color:var(--bad)}
.b-stopped{background:#fde8e6;color:var(--bad)}
.b-pending{background:#eef1f4;color:var(--muted)}
.gantt{position:relative;border:1px solid var(--line);border-radius:6px;
       overflow:hidden;background:#fff}
.row{display:grid;grid-template-columns:290px 1fr;border-bottom:1px solid
     #eef1f4;min-height:22px}
.row:last-child{border-bottom:none}
.row .name{padding:3px 8px;font-size:11.5px;border-right:1px solid #eef1f4;
           white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .name .eq{color:var(--muted)}
.row .track{position:relative;height:22px;background:
  repeating-linear-gradient(90deg,#fbfcfd 0 59px,#f2f5f8 59px 60px);}
.bar{position:absolute;top:5px;height:12px;border-radius:2px;min-width:2px}
.bar.RUNNING{background:var(--run)} .bar.COMPLETE{background:var(--done)}
.bar.HELD,.bar.HOLDING{background:var(--hold)}
.bar.ABORTED,.bar.ABORTING,.bar.STOPPED{background:var(--bad)}
.bar.RESTARTING{background:#8a5cf6} .bar.IDLE{background:var(--idle)}
.bar.STOPPING{background:#d97706}
.mk{position:absolute;top:1px;width:2px;height:20px;background:var(--bad)}
.mk.hold{background:var(--hold)} .mk.restart{background:#8a5cf6}
.mk.fault{background:#d6336c;width:3px;border-radius:2px}
.mk.cmd{background:#0b6bcb}
.axis{display:grid;grid-template-columns:290px 1fr;font-size:10px;
      color:var(--muted);padding-top:2px}
.axis .ticks{position:relative;height:14px}
.axis .t{position:absolute;transform:translateX(-50%)}
.legend{display:flex;gap:14px;font-size:11px;color:var(--muted);
        margin-top:6px;flex-wrap:wrap}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;
          margin-right:4px;vertical-align:-1px}
.grid{display:grid;grid-template-columns:1.35fr 1fr;gap:14px}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{text-align:left;padding:3px 8px;border-bottom:1px solid #eef1f4}
th{font-size:10px;text-transform:uppercase;letter-spacing:.06em;
   color:var(--muted)}
td.n{text-align:right;font-variant-numeric:tabular-nums}
.tree{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;
      line-height:1.65}
.tree .st{font-weight:700}
.st-RUNNING{color:var(--run)} .st-COMPLETE{color:var(--done)}
.st-HELD,.st-HOLDING{color:var(--hold)}
.st-ABORTED,.st-ABORTING,.st-STOPPED{color:var(--bad)}
.st-PENDING{color:var(--muted)}
.lines td .dot{display:inline-block;width:8px;height:8px;border-radius:50%;
               background:var(--idle);margin-right:5px}
.lines td .dot.on{background:var(--run)}
footer{color:var(--muted);font-size:11px;padding:0 26px 30px}
a{color:var(--run);text-decoration:none} a:hover{text-decoration:underline}
"""

COLOR_CLASS = {
    "RUNNING": "RUNNING", "COMPLETE": "COMPLETE", "HELD": "HELD",
    "HOLDING": "HOLDING", "RESTARTING": "RESTARTING", "STOPPING": "STOPPING",
    "STOPPED": "STOPPED", "ABORTING": "ABORTING", "ABORTED": "ABORTED",
    "IDLE": "IDLE",
}


def e(x) -> str:
    return html.escape("" if x is None else str(x))


def _ms(t) -> str:
    return fmt_ms(t) if t is not None else ""


def _state_badge(state: str) -> str:
    key = state.lower().split("_")[0]
    return f'<span class="badge b-{key}">{e(state)}</span>'


# --------------------------------------------------------------------------
def progress_html(run, verification: Optional[dict] = None,
                  duration_ms: Optional[int] = None,
                  links: Optional[Dict[str, str]] = None) -> str:
    now = duration_ms if duration_ms is not None else run.plant.now
    out: List[str] = []
    w = out.append
    w("<!doctype html><html><head><meta charset='utf-8'>")
    w("<title>Batch progress</title>")
    w(f"<style>{CSS}</style></head><body>")
    w("<header><h1>Paint batch control &mdash; execution progress</h1>")
    w(f"<div class='meta'>{len(run.batches)} batch(es) &middot; "
      f"sim clock {_ms(now)} &middot; "
      f"tick {run.scenario.tick_ms} ms</div></header><main>")

    # -- overview ---------------------------------------------------------
    w("<h2>Batches</h2><div class='card'><table><tr>"
      "<th>Batch</th><th>Recipe</th><th>Product</th><th>Lot</th>"
      "<th>Queue</th><th>State</th><th>Started</th><th>Finished</th>"
      "<th>Record</th></tr>")
    for bid, b in run.batches.items():
        rep = b.final_report or {}
        rec = rep.get("recipe", {})
        link = (links or {}).get(bid)
        w(f"<tr><td><b>{e(bid)}</b></td>"
          f"<td>{e(rec.get('id', b.recipe.recipe_id))} "
          f"v{e(rec.get('version', b.recipe.version))}</td>"
          f"<td>{e(rec.get('product', b.recipe.product))}</td>"
          f"<td>{e(b.lot)}</td><td>p{b.priority} #{b.seq}</td>"
          f"<td>{_state_badge(b.state.value)}</td>"
          f"<td>{_ms(b.started_at)}</td><td>{_ms(b.finished_at)}</td>"
          f"<td>{f'<a href={e(link)}>{e(link)}</a>' if link else ''}</td>"
          "</tr>")
    w("</table></div>")

    # -- gantt ------------------------------------------------------------
    for bid, b in run.batches.items():
        w(f"<h2>{e(bid)} &mdash; {_state_badge(b.state.value)}</h2>")
        w("<div class='card'>")
        phases = _phase_rows(run, b)
        # the timeline runs a little past the last activity so end-state
        # bars (complete / aborted) are visible instead of being clipped
        last = max([p["finished_at"] or now for p in phases] + [now, 1])
        span = int(last * 1.02) + 1000
        # faults and commands as markers
        markers = []
        for ev in b.record:
            if ev["category"] == "fault":
                markers.append((ev["t"], "fault", ev["event"]))
            elif ev["category"] == "command" and ev.get("command"):
                cls = "cmd" if ev.get("accepted") else "cmd"
                markers.append((ev["t"], cls, ev["command"]))
        w("<div class='gantt'>")
        for p in phases:
            start = p["started_at"] or 0
            end = p["finished_at"] or now
            segs = _segments(p["transitions"], start, end, span)
            w("<div class='row'>")
            w(f"<div class='name'>{e(p['name'])} "
              f"<span class='eq'>{e(p['equipment'])}</span></div>")
            w("<div class='track'>")
            for (s, en, state) in segs:
                left = 100.0 * s / span
                width = max(0.4, 100.0 * (en - s) / span)
                w(f"<div class='bar {COLOR_CLASS.get(state, 'IDLE')}' "
                  f"style='left:{left:.3f}%;width:{width:.3f}%' "
                  f"title='{e(p['name'])} {e(state)} "
                  f"{_ms(s)}-{_ms(en)}'></div>")
            for (t, cls, label) in markers:
                if start <= t <= end:
                    left = 100.0 * t / span
                    w(f"<div class='mk {cls}' style='left:{left:.3f}%' "
                      f"title='{e(label)} {_ms(t)}'></div>")
            w("</div></div>")
        w("</div>")
        w(f"<div class='axis'><div></div><div class='ticks'>")
        ticks = 8
        for i in range(ticks + 1):
            t = span * i / ticks
            w(f"<span class='t' style='left:{100.0*i/ticks:.2f}%'>"
              f"{_ms(t)}</span>")
        w("</div></div>")
        w("<div class='legend'>"
          "<span><i style='background:var(--run)'></i>running</span>"
          "<span><i style='background:var(--done)'></i>complete</span>"
          "<span><i style='background:var(--hold)'></i>held</span>"
          "<span><i style='background:#8a5cf6'></i>restarting</span>"
          "<span><i style='background:var(--bad)'></i>aborted/stopped</span>"
          "<span><i style='background:#d6336c'></i>fault</span>"
          "<span><i style='background:#0b6bcb'></i>command</span>"
          "</div>")
        w("</div>")

    # -- tree + plant -----------------------------------------------------
    w("<div class='grid'>")
    w("<div>")
    for bid, b in run.batches.items():
        w(f"<h2>Recipe tree &mdash; {e(bid)}</h2><div class='card tree'>")
        if b.root is not None:
            w(_tree_html(run.engine, b.root, 0))
        w("</div>")
    w("</div>")

    w("<div>")
    w("<h2>Plant lines</h2><div class='card'><table class='lines'>")
    w("<tr><th>Line</th><th>Holder</th><th>In transit</th></tr>")
    for name, seg in run.plant.lines.items():
        holder = seg.holder or "&mdash;"
        kg = seg.contents.qty_kg if seg.contents else 0.0
        on = "on" if seg.active_token else ""
        w(f"<tr><td><span class='dot {on}'></span>{e(name)}</td>"
          f"<td>{e(holder)}</td><td class='n'>{kg:.1f} kg</td></tr>")
    w("</table></div>")

    w("<h2>Vessels</h2><div class='card'><table>")
    w("<tr><th>Tank</th><th class='n'>kg</th><th class='n'>temp</th>"
      "<th>agitator</th><th>contents</th></tr>")
    for name, t in run.plant.tanks.items():
        w(f"<tr><td>{e(name)}</td><td class='n'>{t.total_kg():.1f}</td>"
          f"<td class='n'>{t.temperature():.1f}</td>"
          f"<td>{t.agitator_speed():.2f}</td>"
          f"<td>{e(t.content.name if t.content else '')}</td></tr>")
    w("</table></div>")

    w("<h2>Faults injected</h2><div class='card'><table>")
    if not run.plant.injected_faults:
        w("<tr><td>none</td></tr>")
    for f in run.plant.injected_faults:
        w(f"<tr><td>{e(f['time'])}</td><td>{e(f['type'])}</td>"
          f"<td>{e(f['target'])}</td><td>{e(f['detail'])}</td></tr>")
    w("</table></div>")

    w("<h2>Commands</h2><div class='card'><table>")
    if not run.commands:
        w("<tr><td>none</td></tr>")
    for c in run.commands:
        ok = "<span class='badge b-complete'>accepted</span>" if c["accepted"] \
            else "<span class='badge b-aborted'>rejected</span>"
        w(f"<tr><td>{e(c['time'])}</td><td>{e(c['command'])}</td>"
          f"<td>{e(c['target'])}</td><td>{ok}</td>"
          f"<td>{e(c['reason'])}</td></tr>")
    w("</table></div></div></div>")

    if verification is not None:
        w("<h2>Verification</h2><div class='card'>")
        ok = verification.get("ok")
        w(f"<p><b>{'PASS' if ok else 'FAIL'}</b> &mdash; "
          f"{len(verification.get('failures', []))} failure(s)</p>")
        for name, c in verification.get("checks", {}).items():
            mark = "&#10003;" if c["ok"] else "&#10007;"
            w(f"<div>{mark} {e(name)}</div>")
        w("</div>")

    w("</main><footer>Static snapshot generated from the "
      "deterministic simulation. Times are simulation time.</footer>")
    w("</body></html>")
    return "\n".join(out)


def _phase_rows(run, batch):
    """Phase rows from the finished report, or live from the runtime tree."""
    if batch.final_report is not None:
        return [
            {"name": p["name"], "equipment": p["equipment"],
             "started_at": p["started_at"], "finished_at": p["finished_at"],
             "transitions": p["transitions"]}
            for p in batch.final_report.get("phases", [])
        ]
    return run.engine.phase_log(batch)


def _segments(transitions, start: int, end: int, now: int):
    """Split a phase's life into (from, to, state) spans from its trace."""
    segs = []
    if not transitions:
        return [("IDLE", start, now)]
    cur_state = transitions[0]["from"]
    cur_from = start
    for t in transitions:
        if t["at"] > cur_from:
            segs.append((cur_from, t["at"], cur_state))
        cur_state = t["to"]
        cur_from = t["at"]
    if cur_from < end:
        segs.append((cur_from, end, cur_state))
    elif segs or cur_state:
        # terminal / instantaneous state: draw a stub so the outcome is
        # visible on the timeline
        segs.append((cur_from, cur_from + max(1500, int(end * 0.006)),
                     cur_state))
    return segs or [(cur_state, start, end)]


def _tree_html(engine, rt, depth: int) -> str:
    state = engine.state_of(rt)
    pad = "&nbsp;&nbsp;" * depth
    glyph = {"COMPLETE": "&#9679;", "RUNNING": "&#9654;", "HELD": "&#10074;",
             "HOLDING": "&#10073;", "RESTARTING": "&#8635;",
             "ABORTED": "&#10005;", "STOPPED": "&#9632;"}.get(state, "&#9675;")
    line = (f"<div>{pad}<span class='st st-{e(state)}'>{glyph} "
            f"{e(rt.element.name)}</span> "
            f"<span class='st-{e(state)}'>{e(state)}</span></div>")
    out = [line]
    for c in rt.children:
        out.append(_tree_html(engine, c, depth + 1))
    return "".join(out)


# --------------------------------------------------------------------------
def write_progress(run, path, verification=None, links=None) -> str:
    doc = progress_html(run, verification, links=links)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return str(path)
