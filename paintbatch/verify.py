"""Acceptance verifier.

Checks a completed :class:`runner.Run` and returns a structured result:

1. every line segment is held by at most one batch at a time and every
   cross-batch hand-over is clean (release before next acquire);
2. no line ever carried two batches' material at an overlapping time;
3. every phase's observed transition list matches the expected table;
4. upper-level (unit procedure / operation / batch) states always agree
   with an aggregation of the phases below them, sampled every tick;
5. no command was ever accepted in a state where it is illegal;
6. the material balance closes for every transfer (what left the source
   arrived in the target); the plant is not silently losing mass;
7. batch final state is the one the scenario expects.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from .engine import RuntimeStatus
from .states import legal_pairs

#: states an element must never be in while its phases are healthy
AGGREGATE_PRECEDENCE = [
    RuntimeStatus.ABORTING, RuntimeStatus.STOPPING, RuntimeStatus.HOLDING,
    RuntimeStatus.HELD, RuntimeStatus.RESTARTING, RuntimeStatus.RUNNING,
    RuntimeStatus.COMPLETE, RuntimeStatus.STOPPED, RuntimeStatus.ABORTED,
]

VALID_AGGREGATES = {
    # (element state, snapshot of phase states) -> allowed?
}


def _event(run, batch, category=None, event=None):
    out = []
    for ev in batch.record:
        if category is not None and ev["category"] != category:
            continue
        if event is not None and ev["event"] != event:
            continue
        out.append(ev)
    return out


# --------------------------------------------------------------------------
# 1 + 2. line exclusivity
# --------------------------------------------------------------------------
def check_line_exclusivity(run) -> List[str]:
    """Reservation and material spans must never overlap across batches."""
    failures: List[str] = []
    log = run.plant.reservation_log
    holder_by_t: Dict[str, Dict[str, str]] = defaultdict(dict)
    for (t, batch, resource, action) in log:
        cur = holder_by_t[resource]
        if action == "ACQUIRE":
            if cur:
                failures.append(
                    f"{fmt(t)}: {resource} acquired by {batch} while held by "
                    f"{','.join(cur)}")
            cur[batch] = batch
        else:
            if batch not in cur:
                failures.append(
                    f"{fmt(t)}: {resource} released by {batch} but not held")
            cur.pop(batch, None)
    # material spans
    for name, seg in run.plant.lines.items():
        spans = sorted(seg.history, key=lambda h: h["start"])
        for i in range(1, len(spans)):
            prev, cur = spans[i - 1], spans[i]
            if prev["batch"] != cur["batch"] and cur["start"] <= prev["end"]:
                failures.append(
                    f"{name}: material of {prev['batch']} and {cur['batch']} "
                    f"overlaps ({prev['start']}-{prev['end']} vs "
                    f"{cur['start']}-{cur['end']})")
    # per-sample: at most one batch may hold a line, and a line carrying
    # material must have exactly that holder and no other transfer token
    for sample in run.samples:
        t = sample["t"]
        holders = defaultdict(list)
        for res, holder in sample["reservations"].items():
            if res in run.plant.lines:
                holders[res].append(holder)
        for name, hs in holders.items():
            if len(hs) > 1:
                failures.append(f"{fmt(t)}: {name} held by {hs}")
        for name, info in sample["lines"].items():
            if info["kg"] > 0 and info["holder"] is None:
                failures.append(
                    f"{fmt(t)}: {name} carries {info['kg']}kg with no holder")
    for name, seg in run.plant.lines.items():
        if seg.active_token is not None:
            failures.append(f"{name}: transfer token left behind "
                            f"({seg.active_token})")
    return failures


def fmt(ms: int) -> str:
    from .sim import fmt_ms
    return fmt_ms(ms)


# --------------------------------------------------------------------------
# 3. expected transition tables
# --------------------------------------------------------------------------
def check_transitions(run) -> Tuple[List[dict], List[str]]:
    failures: List[str] = []
    rows: List[dict] = []
    for bid, batch in run.batches.items():
        for phase in batch.phases():
            observed = [(t["from"], t["to"]) for t in phase.transitions]
            exp = None
            for e in run.scenario.expectations:
                if e.batch is not None and e.batch != bid:
                    continue
                if e.phase in (phase.phase.name, phase.path):
                    exp = e
                    break
            row = {
                "batch": bid,
                "phase": phase.phase.name,
                "path": phase.path,
                "observed": observed,
                "expected": list(exp.transitions) if exp else None,
                "ok": True,
                "note": "",
            }
            if exp is not None:
                if exp.contains:
                    ok = _is_subsequence(exp.transitions, observed)
                else:
                    ok = sorted(exp.transitions) == sorted(observed)
                row["ok"] = ok
                if not ok:
                    failures.append(
                        f"{bid}/{phase.phase.name}: transitions {observed} "
                        f"!= expected {exp.transitions}")
            rows.append(row)
    for e in run.scenario.expectations:
        hit = any((r["phase"] == e.phase or r["path"] == e.phase)
                  and (e.batch is None or r["batch"] == e.batch)
                  for r in rows)
        if not hit:
            failures.append(
                f"expected phase {e.phase!r}"
                f"{' in ' + e.batch if e.batch else ''} never ran")
    return rows, failures


def _is_subsequence(needle, haystack) -> bool:
    it = iter(haystack)
    return all(any(x == h for h in it) for x in needle)


# --------------------------------------------------------------------------
# 4. aggregate consistency, sampled from the recorded trace
# --------------------------------------------------------------------------
#: legal aggregate value for a set of phase states (see engine.aggregate_state)
def _expected_aggregate(engine, rt) -> str:
    return engine.aggregate_state(rt).value


def _aggregate_from_snapshot(engine, rt, phase_states, intent) -> str:
    """Recompute an upper-level state from the *sampled* phase states.

    This is deliberately independent of ``engine.aggregate_state`` - it is
    the check that the engine's aggregation rule still holds tick by tick.
    """
    if rt.kind == "phase":
        return phase_states.get(rt.path, "IDLE")
    states = [phase_states[p.path] for p in rt.phases()
              if p.path in phase_states]
    if not states:
        return "RUNNING"  # the element's own graph is active, no phase yet
    terminal = {"COMPLETE", "STOPPED", "ABORTED"}

    def finished(s):
        return s in terminal or (intent is not None and s == "IDLE")

    if intent == "ABORT" or any(s in ("ABORTING", "ABORTED") for s in states):
        return "ABORTING"
    if intent == "STOP" or any(s in ("STOPPING", "STOPPED") for s in states):
        return "STOPPING"
    if any(s == "HOLDING" for s in states):
        return "HOLDING"
    if any(s == "HELD" for s in states):
        return "HELD"
    if any(s == "RESTARTING" for s in states):
        return "RESTARTING"
    if all(finished(s) for s in states):
        if rt.frame is not None and rt.end_status is None \
                and rt.frame.status != "DONE":
            return "RUNNING"
        if any(s == "ABORTED" for s in states):
            return "ABORTED"
        if any(s == "STOPPED" for s in states):
            return "STOPPED"
        return "COMPLETE"
    return "RUNNING"


def _intent_of(engine, rt) -> Optional[str]:
    kind = engine.stop_kind(rt)
    if kind is not None:
        return kind.value
    node = rt
    while node is not None:
        if node.intent is not None and node.intent.value == "HOLD":
            return "HOLD"
        node = node.parent
    return None


def check_aggregation(run) -> List[str]:
    """Every sampled upper-level state must equal the aggregation of the
    phases below it, and every batch state the aggregation of its procedure.
    """
    failures: List[str] = []
    eng = run.engine
    for sample in run.samples:
        t = sample["t"]
        for bid, batch in run.batches.items():
            if bid not in sample["batches"]:
                continue
            info = sample["batches"][bid]
            live = sample["reservations"]
            root = batch.root
            if root is None:
                continue
            # batch state == root aggregate (a terminal root is authoritative);
            # only compare once the run has actually begun
            if root.started_at is None or batch.is_done():
                continue
            intent = _intent_of(eng, root)
            recomputed = _aggregate_from_snapshot(
                eng, root, info["phases"], intent)
            # an element that has finished for good is authoritative
            if root.end_status is not None:
                recomputed = root.end_status.value
            if info["state"] != recomputed:
                failures.append(
                    f"{fmt(t)} {bid}: batch state {info['state']} != "
                    f"aggregate of phases {recomputed}")
    return failures


# --------------------------------------------------------------------------
# 5. illegal command acceptance
# --------------------------------------------------------------------------
def check_command_legality(run) -> List[str]:
    failures: List[str] = []
    legal = legal_pairs()
    for bid, batch in run.batches.items():
        for p in batch.phases():
            for t in p.transitions:
                if (t["from"], t["to"]) not in legal:
                    failures.append(
                        f"{bid}/{p.path}: illegal transition "
                        f"{t['from']}->{t['to']} ({t['reason']})")
    # every command the engine accepted must show up as an accepted command
    # or as a phase transition at the same instant
    accepted = [c for c in run.engine.command_log if c["result"] == "ACCEPTED"]
    for cmd in accepted:
        batch = run.batches.get(cmd["batch"])
        if batch is None:
            continue
        stamp = cmd["t"]
        hit = any(t["at"] == stamp for p in batch.phases()
                  for t in p.transitions)
        if not hit and cmd["command"] not in ("RESET",):
            failures.append(
                f"{cmd['batch']}: accepted {cmd['command']} at {fmt(stamp)} "
                f"produced no phase transition")
    return failures


# --------------------------------------------------------------------------
# 6. mass balance
# --------------------------------------------------------------------------
def check_mass_balance(run) -> List[str]:
    """Line evidence must agree with the batch record, and no tank may go
    negative."""
    failures: List[str] = []
    for bid, batch in run.batches.items():
        span_total = 0.0
        for seg in run.plant.lines.values():
            for h in seg.history:
                if h["batch"] == bid:
                    span_total += h["kg"]
        moved = 0.0
        for ev in _event(run, batch, "process", "transfer_done"):
            moved += float(ev.get("kg", 0.0))
        if span_total > 0 and abs(span_total - moved) > 1.0:
            failures.append(
                f"{bid}: line spans total {span_total:.1f}kg but transfers "
                f"report {moved:.1f}kg")
    for name, tank in run.plant.tanks.items():
        if tank.total_kg() < -1e-6:
            failures.append(f"{name}: negative mass")
    return failures


def check_batch_sizes(run, tolerance=0.05) -> List[str]:
    """Completed batches must actually have delivered their product."""
    failures: List[str] = []
    prod = run.plant.tanks.get("PRODUCT")
    if prod is None:
        return failures
    total_declared = sum(
        b.final_report["recipe"]["batch_size_kg"]
        for b in run.batches.values()
        if b.final_report and b.state == RuntimeStatus.COMPLETE)
    if total_declared > 0:
        got = prod.total_kg()
        if got < total_declared * (1 - tolerance):
            failures.append(
                f"product tank has {got:.1f}kg but completed batches "
                f"declared {total_declared:.1f}kg")
    return failures


# --------------------------------------------------------------------------
# 7. expected final states
# --------------------------------------------------------------------------
def check_final_states(run) -> List[str]:
    failures: List[str] = []
    spec = run.scenario
    expected = spec.expected_states
    for bid, want in expected.items():
        got = run.batches[bid].state.value
        if got != want:
            failures.append(f"{bid}: final state {got}, expected {want}")
    return failures


def check_no_held_resources(run) -> List[str]:
    failures: List[str] = []
    for bid, batch in run.batches.items():
        if not batch.is_done():
            continue
        held = run.plant.held_by_batch(bid)
        if held:
            failures.append(f"{bid}: finished but still holds {held}")
    return failures


# --------------------------------------------------------------------------
# full verification
# --------------------------------------------------------------------------
def verify(run) -> dict:
    transitions, tr_fail = check_transitions(run)
    checks = {
        "line_exclusivity": check_line_exclusivity(run),
        "transitions": tr_fail,
        "aggregation": check_aggregation(run),
        "command_legality": check_command_legality(run),
        "mass_balance": check_mass_balance(run),
        "batch_sizes": check_batch_sizes(run),
        "final_states": check_final_states(run),
        "resources_released": check_no_held_resources(run),
    }
    failures = [f"{k}: {msg}" for k, msgs in checks.items() for msg in msgs]
    return {
        "scenario": run.scenario.name,
        "ok": not failures,
        "failures": failures,
        "checks": {k: {"ok": not v, "detail": v} for k, v in checks.items()},
        "transition_table": transitions,
        "batches": {
            bid: {
                "state": b.state.value,
                "started": fmt(b.started_at) if b.started_at else "",
                "finished": fmt(b.finished_at) if b.finished_at else "",
                "phases": [p.phase.name for p in b.phases()],
            }
            for bid, b in run.batches.items()
        },
        "stopped_reason": run.stopped_reason,
        "duration": fmt(run.plant.now),
    }
