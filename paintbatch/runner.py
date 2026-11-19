"""Acceptance orchestra: scripted multi-batch runs with hold / restart /
abort commands and fault injection.

A :class:`Scenario` describes

* which batches to start (recipe, id, lot, priority, parameters),
* a script: timed hooks that inject faults or issue commands,
* an optional per-batch expected transition table,
* a duration cap and a step.

:class:`Runner` executes it deterministically and returns a :class:`Run`
holding the engine, the plant, and the generated reports.  The same object
is what the verifier, the batch-record writer and the UI snapshot use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .engine import Engine
from .plant_layout import build_plant
from .recipes import all_recipes, seed_raw_materials
from .sim import Plant, fmt_ms
from .states import Cmd

# --------------------------------------------------------------------------
# timed hooks
# --------------------------------------------------------------------------
@dataclass
class Hook:
    """A timed scripted action.

    ``when`` (optional) is a predicate over the run; the hook fires at the
    first tick where the clock has reached ``at_ms`` *and* the predicate is
    true.  Predicates are how drill scripts say "hold while the cook is
    running" instead of guessing a timestamp.
    """
    at_ms: int
    label: str
    fn: Callable[["Run"], None]
    when: Optional[Callable[["Run"], bool]] = None
    fired: bool = False


@dataclass
class StepLog:
    """One row of the expected transition table for a phase.

    ``batch`` optionally scopes the row to one batch (the same phase name
    can exist in several batches with different histories); ``contains``
    allows extra transitions as long as the listed ones appear in order.
    """
    phase: str
    transitions: List[Tuple[str, str]]  # [(from, to), ...] in order
    contains: bool = False              # subsequence instead of exact
    batch: Optional[str] = None


@dataclass
class Scenario:
    name: str
    batches: List[dict]                 # kwargs for Runner.start_batch
    script: List[Hook] = field(default_factory=list)
    expectations: List[StepLog] = field(default_factory=list)
    expected_states: Dict[str, str] = field(default_factory=dict)
    max_ms: int = 3_600_000
    tick_ms: int = 100
    description: str = ""
    #: invariants checked in addition to the standard set
    checks: List[str] = field(default_factory=list)


class Run:
    def __init__(self, scenario: Scenario, plant: Plant, engine: Engine):
        self.scenario = scenario
        self.plant = plant
        self.engine = engine
        self.batches: Dict[str, object] = {}
        self.hook_log: List[dict] = []
        self.stopped_reason: str = ""
        self.steps = 0
        self.commands: List[dict] = []
        #: per-tick samples of the whole runtime tree, used by the verifier
        #: to prove upper states are an aggregation of the phases below
        self.samples: List[dict] = []
        self.sample_every = 5

    # -- helpers available to hooks --------------------------------------
    def cmd(self, batch_id: str, cmd: Cmd, reason: str = "",
            target: Optional[str] = None) -> bool:
        target = target or batch_id
        ok = self.engine.command(target, cmd, reason)
        self.commands.append({
            "t": self.plant.now, "time": fmt_ms(self.plant.now),
            "batch": batch_id, "target": target, "command": cmd.value,
            "reason": reason, "accepted": ok,
        })
        self.hook_log.append({
            "t": self.plant.now, "time": fmt_ms(self.plant.now),
            "action": f"{cmd.value} {batch_id}",
            "detail": reason, "accepted": ok,
        })
        return ok

    def inject(self, label: str, fn: Callable[[], None]) -> None:
        fn()
        self.hook_log.append({
            "t": self.plant.now, "time": fmt_ms(self.plant.now),
            "action": "inject", "detail": label, "accepted": True,
        })

    def state(self, batch_id: str) -> str:
        b = self.batches[batch_id]
        return b.state.value

    def phase_state(self, batch_id: str, phase: str) -> str:
        return self.batches[batch_id].phase(phase).state.value

    def running(self) -> bool:
        return any(not b.is_done() for b in self.batches.values())

    def done(self) -> bool:
        return all(b.is_done() for b in self.batches.values())

    # -- report -----------------------------------------------------------
    def report(self) -> dict:
        return self.engine.report()


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------
class Runner:
    def __init__(self, scenario: Scenario, recipes: Optional[dict] = None,
                 plant: Optional[Plant] = None):
        self.scenario = scenario
        self.recipes = recipes or all_recipes()
        self.plant = plant or build_plant()
        if plant is None:
            seed_raw_materials(self.plant)
        self.engine = Engine(self.plant, tick_ms=scenario.tick_ms)
        self.run_obj: Optional[Run] = None

    def build(self) -> Run:
        run = Run(self.scenario, self.plant, self.engine)
        self.run_obj = run
        self.pending_hooks = list(self.scenario.script)
        for spec in self.scenario.batches:
            spec = dict(spec)
            rid = spec.pop("recipe")
            bid = spec.pop("batch_id")
            recipe = self.recipes[rid]
            batch = self.engine.start_batch(recipe, bid, **spec)
            run.batches[bid] = batch
        return run

    def _fire(self, hook: Hook) -> None:
        if hook.fired or self.run_obj is None:
            return
        hook.fired = True
        hook.fn(self.run_obj)

    def _fire_due_hooks(self, run: Run) -> None:
        now = self.plant.now
        for hook in list(self.pending_hooks):
            if now < hook.at_ms:
                continue
            if hook.when is not None:
                try:
                    ready = hook.when(run)
                except KeyError:
                    ready = False
                if not ready:
                    continue
            self.pending_hooks.remove(hook)
            self._fire(hook)

    def run(self, run: Optional[Run] = None) -> Run:
        run = run or self.build()
        max_steps = self.scenario.max_ms // self.scenario.tick_ms
        while run.steps < max_steps:
            self.engine.tick()
            self._fire_due_hooks(run)
            run.steps += 1
            if run.steps % run.sample_every == 0:
                run.samples.append(self.snapshot(run))
            if run.done():
                run.stopped_reason = "all batches finished"
                break
        else:
            run.stopped_reason = "time cap reached"
        run.samples.append(self.snapshot(run))
        run.report()
        return run

    def snapshot(self, run: Run) -> dict:
        """One point-in-time sample of every element state and every line."""
        eng = self.engine
        batches = {}
        for bid, b in run.batches.items():
            elements = {}
            if b.root is not None:
                for el in [b.root] + b.root.descendant_elements():
                    if el.started_at is None:
                        continue
                    elements[el.path] = eng.state_of(el)
            batches[bid] = {
                "state": b.state.value,
                "elements": elements,
                "phases": {p.path: p.state.value for p in b.phases()
                           if p.started_at is not None},
            }
        return {
            "t": self.plant.now,
            "batches": batches,
            "lines": {name: {"holder": seg.holder,
                             "token": seg.active_token,
                             "kg": round(seg.contents.qty_kg, 3)
                             if seg.contents else 0.0}
                      for name, seg in self.plant.lines.items()},
            "reservations": dict(self.plant.reservations),
        }


def run_scenario(scenario: Scenario, **kw) -> Run:
    return Runner(scenario, **kw).run()
