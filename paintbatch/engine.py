"""Execution engine: turns a recipe into running batches.

The engine owns the plant model (``sim.Plant``), the shared-resource
arbiter, and the runtime tree for each batch.  It is stepped with a fixed
time increment; everything is deterministic.

Runtime tree
------------
``BatchRuntime`` -> ``ElementRuntime`` (procedure / unit procedure /
operation) -> ``PhaseRuntime``.  Upper elements execute a :class:`Frame`,
a small interpreter for the step graph.  A phase runs its operation list as
a generator; hold/restart/stop/abort each run *their own written sequence*
and only report HELD / RUNNING / STOPPED / ABORTED once that sequence has
physically finished.

Upper-level states are derived from the phases below them
(``aggregate_state``), never commanded directly.

Commands
--------
``engine.command(target, Cmd.X)`` where target is a batch id, a phase
qualified name (``"B001:UP1.OP1.PH1"``), or any element path.  Commands
illegal in the current state are rejected and recorded, never raised.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import BatchViolation, OpFailed, RecipeError
from .ops import Wait
from .recipe import (
    Acquire,
    Choice,
    Element,
    Parallel,
    Phase,
    Recipe,
    Release,
    Transition,
)
from .sim import Plant, fmt_ms
from .states import (
    Cmd,
    PhaseState,
    is_legal,
    next_state,
)

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------
ACTIVE_STATES = {
    PhaseState.RUNNING,
    PhaseState.HOLDING,
    PhaseState.HELD,
    PhaseState.RESTARTING,
    PhaseState.STOPPING,
    PhaseState.ABORTING,
}
TERMINAL_STATES = {PhaseState.COMPLETE, PhaseState.STOPPED, PhaseState.ABORTED}


class FrameStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    DONE = "DONE"
    STOPPED = "STOPPED"
    ABORTED = "ABORTED"


class RuntimeStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    HOLDING = "HOLDING"
    HELD = "HELD"
    RESTARTING = "RESTARTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ABORTING = "ABORTING"
    ABORTED = "ABORTED"
    COMPLETE = "COMPLETE"


PHASE_TO_RUNTIME = {
    PhaseState.COMPLETE: RuntimeStatus.COMPLETE,
    PhaseState.STOPPED: RuntimeStatus.STOPPED,
    PhaseState.ABORTED: RuntimeStatus.ABORTED,
    PhaseState.RUNNING: RuntimeStatus.RUNNING,
    PhaseState.HOLDING: RuntimeStatus.HOLDING,
    PhaseState.HELD: RuntimeStatus.HELD,
    PhaseState.RESTARTING: RuntimeStatus.RESTARTING,
    PhaseState.STOPPING: RuntimeStatus.STOPPING,
    PhaseState.ABORTING: RuntimeStatus.ABORTING,
    PhaseState.IDLE: RuntimeStatus.PENDING,
}


# --------------------------------------------------------------------------
# context handed to operations
# --------------------------------------------------------------------------
class PhaseContext:
    def __init__(self, engine: "Engine", phase_rt: "PhaseRuntime"):
        self.engine = engine
        self.phase_rt = phase_rt
        self.batch = phase_rt.batch
        self.op_key = "op"

    # -- identity / data --------------------------------------------------
    @property
    def plant(self) -> Plant:
        return self.engine.plant

    @property
    def state(self) -> dict:
        """Checkpoint dict.  The main program keeps its own; each written
        special sequence gets a fresh one so its steps never see the main
        program's bookkeeping."""
        if self.phase_rt.special_kind is not None:
            return self.phase_rt.special_checkpoints
        return self.phase_rt.checkpoints

    @property
    def batch_id(self) -> str:
        return self.batch.batch_id

    @property
    def params(self) -> dict:
        return self.batch.params

    @property
    def lot(self) -> str:
        return self.batch.lot

    @property
    def label(self) -> str:
        return self.phase_rt.path

    @property
    def phase_path(self) -> str:
        return self.phase_rt.path

    def now_ms(self) -> int:
        return self.engine.plant.now

    def qty(self, kg: float) -> float:
        return kg * self.batch.scale

    def temp(self, temp_c: float) -> float:
        return temp_c + self.batch.temp_offset

    def op_elapsed_s(self) -> float:
        return self.phase_rt.op_elapsed_s(self.engine.plant.now)

    # -- bookkeeping ------------------------------------------------------
    def event(self, event_type: str, **fields) -> None:
        self.engine.record(self.batch, "process", event_type,
                           phase=self.phase_rt.path, **fields)

    def log(self, message: str) -> None:
        self.engine.record(self.batch, "log", "message",
                           phase=self.phase_rt.path, text=message)

    # -- nested ops -------------------------------------------------------
    def run_op(self, op):
        yield from op(self)


# --------------------------------------------------------------------------
# runtime objects
# --------------------------------------------------------------------------
class ElementRuntime:
    def __init__(self, engine: "Engine", element: Element, batch: "BatchRuntime",
                 parent: Optional["ElementRuntime"], path: str):
        self.engine = engine
        self.element = element
        self.batch = batch
        self.parent = parent
        self.path = path
        self.kind = element.kind
        self.children: List[ElementRuntime] = []
        self.frame: Optional[Frame] = None
        self.started_at: Optional[int] = None
        self.finished_at: Optional[int] = None
        self.end_status: Optional[RuntimeStatus] = None
        self.intent: Optional[Cmd] = None

    # -- tree -------------------------------------------------------------
    def add_child(self, rt: "ElementRuntime") -> None:
        self.children.append(rt)

    # -- what transition conditions can look at ---------------------------
    @property
    def plant(self) -> Plant:
        return self.engine.plant

    @property
    def params(self) -> dict:
        return self.batch.params

    def active_elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.engine.plant.now - self.started_at) / 1000.0

    def phase_state(self, name: str) -> Optional[str]:
        try:
            return self.batch.phase(name).state.value
        except KeyError:
            return None

    def phases(self) -> List["PhaseRuntime"]:
        out: List[PhaseRuntime] = []
        for c in self.children:
            if c.kind == "phase":
                out.append(c)  # type: ignore[arg-type]
            else:
                out.extend(c.phases())
        return out

    def active_phases(self) -> List["PhaseRuntime"]:
        return [p for p in self.phases() if p.state in ACTIVE_STATES]

    def descendant_elements(self) -> List["ElementRuntime"]:
        out: List[ElementRuntime] = []
        for c in self.children:
            out.append(c)
            out.extend(c.descendant_elements())
        return out

    def status(self) -> RuntimeStatus:
        """Coarse status used by the step graph.  COMPLETE/STOPPED/ABORTED
        only once the element's frame has actually ended."""
        if self.end_status is not None:
            return self.end_status
        if self.started_at is None:
            return RuntimeStatus.PENDING
        return self.engine.aggregate_state(self)

    def start(self) -> None:
        self.started_at = self.engine.plant.now
        self.engine.record(
            self.batch, "state", "element_start", path=self.path,
            element_kind=self.kind, state=RuntimeStatus.RUNNING.value,
        )
        self.frame = Frame(self.engine, self, self.element.steps)
        self.frame.start()

    def finish(self, status: RuntimeStatus) -> None:
        if self.end_status is not None:
            return
        self.end_status = status
        self.finished_at = self.engine.plant.now
        self.engine.record(self.batch, "state", "element_end", path=self.path,
                           element_kind=self.kind, state=status.value)
        if self.parent is not None and self.parent.frame is not None:
            self.parent.frame.child_ended(self, status)

    def is_terminal(self) -> bool:
        return self.end_status in (RuntimeStatus.COMPLETE, RuntimeStatus.STOPPED,
                                   RuntimeStatus.ABORTED)

    def __repr__(self) -> str:  # pragma: no cover
        st = self.end_status.value if self.end_status else "ACTIVE"
        return f"<ElementRuntime {self.path} {st}>"


class PhaseRuntime(ElementRuntime):
    def __init__(self, engine, element: Phase, batch, parent, path):
        super().__init__(engine, element, batch, parent, path)
        self.phase = element
        self.state: PhaseState = PhaseState.IDLE
        self.program = None           # generator of the main operation list
        self.special = None           # generator of hold/restart/stop/abort seq
        self.special_kind: Optional[str] = None
        self.op_started_at: Optional[int] = None
        self.op_paused_total: int = 0
        self.op_paused_at: Optional[int] = None
        self.special_started_at: Optional[int] = None
        self.checkpoints: Dict[str, object] = {}
        self.special_checkpoints: Dict[str, object] = {}
        self.hold_reason: str = ""
        self.fault_reason: str = ""
        self.transitions: List[dict] = []
        self.entered_at: Optional[int] = None
        self.completed_at: Optional[int] = None
        self.started_at = None
        self.end_status = None

    # -- state trace ------------------------------------------------------
    def set_state(self, new: PhaseState, reason: str = "") -> None:
        if new == self.state:
            return
        old = self.state
        now = self.engine.plant.now
        self.state = new
        self.entered_at = now
        self.transitions.append(
            {"at": now, "from": old.value, "to": new.value, "reason": reason}
        )
        self.engine.record(
            self.batch, "state", "phase_transition", phase=self.path,
            phase_name=self.phase.name, frm=old.value, to=new.value,
            reason=reason,
        )
        if new == PhaseState.COMPLETE:
            self.completed_at = now
        if new in TERMINAL_STATES:
            self.finished_at = now
            self.end_status = PHASE_TO_RUNTIME[new]
            self.engine.record(self.batch, "state", "element_end",
                               path=self.path, element_kind="phase",
                               state=new.value)
            if self.parent is not None and self.parent.frame is not None:
                self.parent.frame.child_ended(self, self.end_status)

    def status(self) -> RuntimeStatus:
        if self.started_at is None:
            return RuntimeStatus.PENDING
        return PHASE_TO_RUNTIME[self.state]

    # -- op timing --------------------------------------------------------
    def op_elapsed_s(self, now: int) -> float:
        if self.special_kind is not None:
            if self.special_started_at is None:
                return 0.0
            return max(0.0, (now - self.special_started_at) / 1000.0)
        if self.op_started_at is None:
            return 0.0
        paused = self.op_paused_total
        if self.op_paused_at is not None:
            paused += now - self.op_paused_at
        return max(0.0, (now - self.op_started_at - paused) / 1000.0)

    def _pause_op_clock(self, now: int) -> None:
        if self.op_paused_at is None:
            self.op_paused_at = now

    def _resume_op_clock(self, now: int) -> None:
        if self.op_paused_at is not None:
            self.op_paused_total += now - self.op_paused_at
            self.op_paused_at = None

    # -- lifecycle --------------------------------------------------------
    def begin(self) -> None:
        """The parent sequence has reached this phase: issue START."""
        self.started_at = self.engine.plant.now
        self.engine.record(self.batch, "state", "element_start",
                           path=self.path, element_kind="phase",
                           state=PhaseState.IDLE.value)
        self.command(Cmd.START, "sequence", source="sequence")

    def _on_running(self) -> None:
        now = self.engine.plant.now
        self._resume_op_clock(now)
        if self.op_started_at is None:
            self.op_started_at = now
        if self.program is None:
            ctx = PhaseContext(self.engine, self)
            self.program = _run_ops(self.phase.ops, ctx)

    def command(self, cmd: Cmd, reason: str, source: str = "operator") -> bool:
        if not is_legal(self.state, cmd):
            self.engine.reject(self.batch, self.path, self.state.value,
                               cmd.value, source, f"illegal command: {cmd.value}")
            return False
        now = self.engine.plant.now
        nxt = next_state(self.state, cmd, self.phase.name)
        if cmd == Cmd.HOLD:
            self.hold_reason = reason or "operator hold"
            self._pause_op_clock(now)
            self.set_state(nxt, self.hold_reason)
            self._start_special("hold", self.phase.on_hold)
        elif cmd == Cmd.RESTART:
            self.set_state(nxt, reason or "restart")
            self._start_special("restart", self.phase.on_restart)
        elif cmd == Cmd.STOP:
            self._pause_op_clock(now)
            self.set_state(nxt, reason or "stop")
            self._start_special("stop", self.phase.on_stop)
        elif cmd == Cmd.ABORT:
            self._pause_op_clock(now)
            self.set_state(nxt, reason or "abort")
            self._start_special("abort", self.phase.on_abort)
        elif cmd == Cmd.START:
            self.set_state(nxt, reason or "start")
            self._on_running()
        elif cmd == Cmd.RESET:
            self.set_state(nxt, reason or "reset")
            self.program = None
            self.special = None
            self.special_kind = None
            self.op_started_at = None
            self.op_paused_total = 0
            self.op_paused_at = None
            self.end_status = None
            self.finished_at = None
            self.started_at = None
        return True

    def _start_special(self, kind: str, ops: Sequence) -> None:
        self.special_kind = kind
        # a written hold/restart/stop/abort sequence is a sequence in its own
        # right: its op timeouts count from when it started, not from the main
        # program's start
        self.special_started_at = self.engine.plant.now
        self.special_checkpoints = {}
        ctx = PhaseContext(self.engine, self)
        self.special = _run_ops(ops, ctx) if ops else None
        if not ops:
            self._special_complete()

    def _special_complete(self) -> None:
        kind = self.special_kind
        now = self.engine.plant.now
        if kind == "hold":
            self.set_state(PhaseState.HELD, "hold sequence complete")
        elif kind == "restart":
            self.set_state(PhaseState.RUNNING, "restart sequence complete")
            self._resume_op_clock(now)
            self._on_running()
        elif kind == "stop":
            self.set_state(PhaseState.STOPPED, "stop sequence complete")
        elif kind == "abort":
            self.set_state(PhaseState.ABORTED, "abort sequence complete")
        self.special_kind = None

    # -- per-tick driving -------------------------------------------------
    def tick(self) -> None:
        if self.state in (PhaseState.HOLDING, PhaseState.RESTARTING,
                          PhaseState.STOPPING, PhaseState.ABORTING):
            self._drive_special()
        elif self.state == PhaseState.RUNNING:
            self._drive_program()

    def _drive_special(self) -> None:
        if self.special is None:
            self._special_complete()
            return
        try:
            next(self.special)
        except StopIteration:
            self.special = None
            self._special_complete()
        except OpFailed as exc:
            kind = self.special_kind
            self.special = None
            self.fault_reason = str(exc)
            self.engine.record(self.batch, "fault", "sequence_fault",
                               phase=self.path, sequence=kind, text=str(exc))
            self.engine.alarm(self.batch, self.path, f"{kind} sequence: {exc}")
            if kind == "hold":
                self.set_state(PhaseState.HELD, f"hold sequence fault: {exc}")
            elif kind == "restart":
                self.set_state(PhaseState.HELD, f"restart sequence fault: {exc}")
            elif kind == "stop":
                self.set_state(PhaseState.STOPPED, f"stop sequence fault: {exc}")
            elif kind == "abort":
                self.set_state(PhaseState.ABORTED, f"abort sequence fault: {exc}")
            self.special_kind = None

    def _drive_program(self) -> None:
        if self.program is None:
            return
        try:
            next(self.program)
        except StopIteration:
            self.program = None
            self.set_state(PhaseState.COMPLETE, "operations complete")
            self.engine.record(self.batch, "process", "phase_complete",
                               phase=self.path)
        except OpFailed as exc:
            self.program = None
            self.fault_reason = str(exc)
            self.hold_reason = f"fault: {exc}"
            self.engine.record(self.batch, "fault", "phase_fault",
                               phase=self.path, text=str(exc))
            self.engine.alarm(self.batch, self.path, str(exc))
            self.engine.on_phase_fault(self)

    def abandon(self) -> None:
        self.program = None
        self.special = None
        self.special_kind = None


# --------------------------------------------------------------------------
# generator plumbing
# --------------------------------------------------------------------------
def _run_ops(ops: Sequence, ctx: PhaseContext):
    """Drive a list of ops.  ``Wait`` objects block inside this generator,
    ``Tick`` just yields control back to the engine for one step."""
    for idx, op in enumerate(ops):
        ctx.op_key = f"{idx}"
        gen = op(ctx)
        if gen is None:
            continue
        while True:
            try:
                item = next(gen)
            except StopIteration:
                break
            if isinstance(item, Wait):
                while not item.ready():
                    yield None
                continue
            yield None


# --------------------------------------------------------------------------
# step graph interpreter
# --------------------------------------------------------------------------
class _NodeState:
    def __init__(self):
        self.decided = None
        self.branch_frame: Optional["Frame"] = None
        self.child_rt: Optional[ElementRuntime] = None
        self.request: Optional["ResourceRequest"] = None
        self.parallel_frames: Optional[List["Frame"]] = None


class Frame:
    """Interpreter for one step graph.

    A frame either *owns* its element (the element ends when the frame ends)
    or is a branch of a Parallel/Choice node inside another frame, in which
    case ending the frame only reports back to the node - the owning
    element's graph decides what happens next.
    """

    def __init__(self, engine: "Engine", owner: ElementRuntime, nodes: Sequence,
                 owns_element: bool = True):
        self.engine = engine
        self.owner = owner
        self.nodes = list(nodes)
        self.owns_element = owns_element
        self.pc = 0
        self.status = FrameStatus.PENDING
        self.states: Dict[int, _NodeState] = {}
        self.parent: Optional["Frame"] = None
        self.active_since: Optional[int] = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self.status = FrameStatus.ACTIVE
        self.active_since = self.engine.plant.now

    def end(self, status: FrameStatus) -> None:
        """End this frame; finish the element only if this frame owns it."""
        if self.status in (FrameStatus.DONE, FrameStatus.STOPPED,
                           FrameStatus.ABORTED):
            return
        self.status = status
        self.terminate_children(status)
        if self.owns_element:
            self.owner.finish(
                RuntimeStatus.ABORTED if status == FrameStatus.ABORTED
                else RuntimeStatus.STOPPED if status == FrameStatus.STOPPED
                else RuntimeStatus.COMPLETE)

    def terminate_children(self, status: FrameStatus) -> None:
        for st in self.states.values():
            frames = []
            if st.branch_frame is not None:
                frames.append(st.branch_frame)
            frames.extend(st.parallel_frames or [])
            for fr in frames:
                if fr.status == FrameStatus.ACTIVE:
                    fr.status = status
                    fr.terminate_children(status)
            if st.request is not None and st.request.status == "pending":
                st.request.status = "cancelled"

    def child_ended(self, child: ElementRuntime, status: RuntimeStatus) -> None:
        """A child element/phase reached a terminal state."""
        if self.status != FrameStatus.ACTIVE:
            return
        if status == RuntimeStatus.COMPLETE:
            return  # the normal advance is handled by tick()
        intent = self.engine.stop_kind(self.owner)
        if intent is None and status == RuntimeStatus.ABORTED:
            # phase aborted on its own: the containing element goes too
            self.end(FrameStatus.ABORTED)
            return
        if intent is None:
            self.end(FrameStatus.STOPPED)
            return
        # during a stop/abort wait until every phase below is terminal
        if self.engine.all_terminal(self.owner, intent):
            self.end(FrameStatus.ABORTED if intent == Cmd.ABORT
                     else FrameStatus.STOPPED)

    # -- ticking ----------------------------------------------------------
    def tick(self) -> None:
        if self.status != FrameStatus.ACTIVE:
            return
        intent = self.engine.stop_kind(self.owner)
        if intent is not None:
            # no new steps are dispatched once a stop/abort is in flight
            if self.engine.all_terminal(self.owner, intent):
                self.end(FrameStatus.ABORTED if intent == Cmd.ABORT
                         else FrameStatus.STOPPED)
            return
        if self.engine.hold_intent(self.owner) and self.owns_element:
            # batch-level hold: keep whatever runs, dispatch nothing new
            self.engine.refresh_state(self.owner.batch)
            return
        guard = 0
        while self.status == FrameStatus.ACTIVE:
            guard += 1
            if guard > 1000:  # pragma: no cover - safety net
                raise RuntimeError(f"step graph of {self.owner.path} does not advance")
            if self.pc >= len(self.nodes):
                self.end(FrameStatus.DONE)
                return
            node = self.nodes[self.pc]
            st = self.states.setdefault(self.pc, _NodeState())
            if self._step_node(node, st) == "advance":
                self.pc += 1
                continue
            return

    def _step_node(self, node, st: _NodeState) -> str:
        engine = self.engine
        if isinstance(node, Transition):
            return "advance" if node.condition.ready(self.owner) else "block"
        if isinstance(node, Acquire):
            return self._step_acquire(node, st)
        if isinstance(node, Release):
            engine.release_resources(self.owner.batch, node.resources,
                                     reason=self.owner.path)
            return "advance"
        if isinstance(node, Parallel):
            return self._step_parallel(node, st)
        if isinstance(node, Choice):
            return self._step_choice(node, st)
        if node.kind in ("unit_procedure", "operation", "phase"):
            return self._step_element(node, st)
        raise RecipeError(f"unknown node kind {node.kind!r}")

    def _step_acquire(self, node: Acquire, st: _NodeState) -> str:
        engine = self.engine
        if st.request is None:
            st.request = engine.request_resources(
                self.owner.batch, node.resources, node.priority,
                why=self.owner.path,
            )
        if st.request.status == "granted":
            return "advance"
        return "block"

    def _step_parallel(self, node: Parallel, st: _NodeState) -> str:
        if st.parallel_frames is None:
            st.parallel_frames = []
            for branch in node.branches:
                fr = Frame(self.engine, self.owner, branch,
                           owns_element=False)
                fr.parent = self
                fr.start()
                st.parallel_frames.append(fr)
        all_done = True
        for fr in st.parallel_frames:
            fr.tick()
            if fr.status == FrameStatus.ACTIVE:
                all_done = False
            elif fr.status in (FrameStatus.ABORTED, FrameStatus.STOPPED):
                self.end(fr.status)
                return "block"
            # a branch that ended DONE stays done and no longer ticks
        return "advance" if all_done else "block"

    def _step_choice(self, node: Choice, st: _NodeState) -> str:
        if st.branch_frame is None and st.decided is None:
            for idx, (cond, branch) in enumerate(node.options):
                if cond.ready(self.owner):
                    st.decided = idx
                    fr = Frame(self.engine, self.owner, branch,
                               owns_element=False)
                    fr.parent = self
                    fr.start()
                    st.branch_frame = fr
                    self.engine.record(
                        self.owner.batch, "control", "choice",
                        path=self.owner.path, condition=cond.describe(),
                        branch=f"branch-{idx}",
                    )
                    break
            else:
                return "block"
        fr = st.branch_frame
        if fr is None:
            return "advance"
        fr.tick()
        if fr.status == FrameStatus.DONE:
            return "advance"
        if fr.status in (FrameStatus.ABORTED, FrameStatus.STOPPED):
            self.end(fr.status)
        return "block"

    def _step_element(self, node: Element, st: _NodeState) -> str:
        if st.child_rt is None:
            st.child_rt = self.engine.create_runtime(
                self.owner, node, f"{self.owner.path}.{node.name}")
            st.child_rt.started_at = self.engine.plant.now
            self.engine.record(self.owner.batch, "state", "element_start",
                               path=st.child_rt.path, element_kind=node.kind,
                               state=RuntimeStatus.RUNNING.value)
            if isinstance(st.child_rt, PhaseRuntime):
                st.child_rt.begin()
            else:
                st.child_rt.frame = Frame(self.engine, st.child_rt,
                                          node.steps)
                st.child_rt.frame.start()
        child = st.child_rt
        if not isinstance(child, PhaseRuntime):
            child.frame.tick()
        status = child.status()
        if status == RuntimeStatus.COMPLETE:
            return "advance"
        if status in (RuntimeStatus.STOPPED, RuntimeStatus.ABORTED):
            self.end(FrameStatus.ABORTED
                     if status == RuntimeStatus.ABORTED
                     else FrameStatus.STOPPED)
        return "block"


# --------------------------------------------------------------------------
# batches
# --------------------------------------------------------------------------
class BatchRuntime:
    def __init__(self, engine: "Engine", batch_id: str, recipe: Recipe,
                 seq: int, lot: str = "", scale: float = 1.0,
                 temp_offset: float = 0.0, priority: int = 5,
                 params: Optional[dict] = None):
        self.engine = engine
        self.batch_id = batch_id
        self.recipe = recipe
        self.seq = seq
        self.lot = lot or batch_id
        self.scale = scale
        self.temp_offset = temp_offset
        self.priority = priority
        self.params = dict(recipe.parameters)
        if params:
            self.params.update(params)
        self.root: Optional[ElementRuntime] = None
        self.state: RuntimeStatus = RuntimeStatus.PENDING
        self.created_at = engine.plant.now
        self.started_at: Optional[int] = None
        self.finished_at: Optional[int] = None
        self.intent: Optional[Cmd] = None
        self.record: List[dict] = []
        self.phase_index: Dict[str, PhaseRuntime] = {}
        self.alarms: List[dict] = []
        self.violations: List[str] = []
        self.final_report: Optional[dict] = None
        self.held_resources: List[str] = []

    # -- state ------------------------------------------------------------
    def is_done(self) -> bool:
        return self.state in (RuntimeStatus.COMPLETE, RuntimeStatus.STOPPED,
                              RuntimeStatus.ABORTED)

    def phases(self) -> List[PhaseRuntime]:
        return list({id(p): p for p in self.phase_index.values()}.values())

    def phase(self, qualified: str) -> PhaseRuntime:
        if qualified in self.phase_index:
            return self.phase_index[qualified]
        matches = [p for name, p in self.phase_index.items()
                   if name == qualified or name.endswith("." + qualified)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise KeyError(f"no phase {qualified!r} in batch {self.batch_id}")
        raise KeyError(f"ambiguous phase {qualified!r} in batch {self.batch_id}")

    def element_runtime(self, path: str) -> Optional[ElementRuntime]:
        if self.root is None:
            return None
        if self.root.path == path:
            return self.root
        for el in self.root.descendant_elements():
            if el.path == path:
                return el
        return None

    # -- commands ---------------------------------------------------------
    def hold(self, reason: str = "operator hold") -> bool:
        return self._command_subtree(Cmd.HOLD, reason)

    def restart(self, reason: str = "operator restart") -> bool:
        return self._command_subtree(Cmd.RESTART, reason)

    def stop(self, reason: str = "operator stop") -> bool:
        return self._command_subtree(Cmd.STOP, reason)

    def abort(self, reason: str = "operator abort") -> bool:
        return self._command_subtree(Cmd.ABORT, reason)

    def _command_subtree(self, cmd: Cmd, reason: str,
                         source: str = "operator") -> bool:
        if self.root is None:
            self.engine.reject(self, "<batch>", "PENDING", cmd.value, source,
                               "batch has not started")
            return False
        if self.is_done():
            self.engine.reject(self, "<batch>", self.state.value, cmd.value,
                               source, "batch is finished")
            return False
        if cmd in (Cmd.STOP, Cmd.ABORT):
            self.intent = cmd
        self.engine.refresh_state(self)
        phases = self.root.phases()
        if cmd == Cmd.HOLD:
            targets = [p for p in phases if p.state in
                       (PhaseState.RUNNING, PhaseState.RESTARTING)]
        elif cmd == Cmd.RESTART:
            targets = [p for p in phases if p.state == PhaseState.HELD]
        elif cmd == Cmd.STOP:
            targets = [p for p in phases if p.state in ACTIVE_STATES
                       and p.state not in (PhaseState.STOPPING,
                                           PhaseState.ABORTING)]
        else:
            targets = [p for p in phases if p.state in ACTIVE_STATES
                       and p.state != PhaseState.ABORTING]
        accepted = False
        for p in targets:
            accepted = p.command(cmd, reason, source) or accepted
        if not accepted and cmd in (Cmd.HOLD, Cmd.RESTART):
            # an operator command that cannot be applied must show up as a
            # rejection in the record, not as a silent no-op
            self.engine.reject(
                self, "<batch>", self.state.value, cmd.value, source,
                "nothing to hold" if cmd == Cmd.HOLD else "nothing held")
        if cmd == Cmd.HOLD:
            self.intent = Cmd.HOLD
        elif cmd == Cmd.RESTART:
            if self.intent == Cmd.HOLD:
                self.intent = None
            # a fault-propagated hold leaves HOLD intent all the way up to
            # the root; a restart releases the whole tree, not just the batch
            self.engine.clear_hold_intent(self.root)
        self.engine.record(self, "command", "batch_command", command=cmd.value,
                           accepted=accepted, reason=reason, source=source,
                           targets=[p.path for p in targets])
        if cmd in (Cmd.STOP, Cmd.ABORT):
            self.engine.cancel_requests(self, keep_held=True)
        self.engine.refresh_state(self)
        return accepted


# --------------------------------------------------------------------------
# resource arbitration
# --------------------------------------------------------------------------
@dataclass
class ResourceRequest:
    batch: BatchRuntime
    resources: List[str]
    priority: int
    seq: int
    why: str
    status: str = "pending"  # pending | granted | cancelled
    granted_at: Optional[int] = None


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------
class Engine:
    def __init__(self, plant: Optional[Plant] = None, tick_ms: int = 100):
        self.plant = plant or Plant()
        self.tick_ms = tick_ms
        self.batches: List[BatchRuntime] = []
        self._requests: List[ResourceRequest] = []
        self._req_seq = 0
        self._batch_seq = 0
        self.command_log: List[dict] = []
        self.violations: List[str] = []
        self._line_guard: List[dict] = []

    # -- construction -----------------------------------------------------
    def start_batch(self, recipe: Recipe, batch_id: Optional[str] = None,
                    lot: str = "", scale: float = 1.0, temp_offset: float = 0.0,
                    priority: int = 5, params: Optional[dict] = None
                    ) -> BatchRuntime:
        self._batch_seq += 1
        seq = self._batch_seq
        bid = batch_id or f"B{seq:03d}"
        if any(b.batch_id == bid for b in self.batches):
            raise RecipeError(f"duplicate batch id {bid}")
        batch = BatchRuntime(self, bid, recipe, seq, lot=lot, scale=scale,
                             temp_offset=temp_offset, priority=priority,
                             params=params)
        self.batches.append(batch)
        self.record(batch, "batch", "batch_created",
                    recipe=recipe.recipe_id, lot=batch.lot,
                    batch_size_kg=round(recipe.batch_size_kg * scale, 3),
                    priority=priority, scale=scale)
        root = ElementRuntime(self, recipe.procedure, batch, None,
                              recipe.procedure.name or "procedure")
        batch.root = root
        root.started_at = self.plant.now
        self.record(batch, "state", "element_start", path=root.path,
                    element_kind="procedure", state=RuntimeStatus.RUNNING.value)
        root.frame = Frame(self, root, recipe.procedure.steps)
        root.frame.start()
        batch.started_at = self.plant.now
        batch.state = RuntimeStatus.RUNNING
        return batch

    def create_runtime(self, parent: ElementRuntime, element: Element,
                       path: str) -> ElementRuntime:
        if element.kind == "phase":
            rt: ElementRuntime = PhaseRuntime(self, element, parent.batch,
                                              parent, path)
        else:
            rt = ElementRuntime(self, element, parent.batch, parent, path)
        parent.add_child(rt)
        if isinstance(rt, PhaseRuntime):
            parent.batch.phase_index[rt.path] = rt
            parent.batch.phase_index[element.name] = rt
        return rt

    # -- recording --------------------------------------------------------
    def record(self, batch: BatchRuntime, category: str, event_type: str,
               **fields) -> dict:
        ev = {"t": self.plant.now, "time": fmt_ms(self.plant.now),
              "batch": batch.batch_id, "category": category,
              "event": event_type}
        ev.update(fields)
        batch.record.append(ev)
        return ev

    def alarm(self, batch: BatchRuntime, path: str, message: str) -> dict:
        ev = self.record(batch, "alarm", "alarm", path=path, text=message)
        batch.alarms.append(ev)
        return ev

    def reject(self, batch: BatchRuntime, path: str, state: str, command: str,
               source: str, reason: str) -> None:
        self.command_log.append({
            "t": self.plant.now, "batch": batch.batch_id, "path": path,
            "state": state, "command": command, "source": source,
            "result": "REJECTED", "reason": reason,
        })
        self.record(batch, "command", "command_rejected", path=path,
                    state=state, command=command, source=source, text=reason)

    # -- resources --------------------------------------------------------
    def request_resources(self, batch: BatchRuntime, resources: Sequence[str],
                          priority: int, why: str) -> ResourceRequest:
        if batch.intent in (Cmd.STOP, Cmd.ABORT):
            return ResourceRequest(batch, list(resources), priority, 0, why,
                                   status="cancelled")
        self._req_seq += 1
        req = ResourceRequest(batch, list(resources), priority, self._req_seq,
                              why)
        self._requests.append(req)
        self.record(batch, "resource", "request", resources=list(resources),
                    priority=priority, why=why)
        return req

    def cancel_requests(self, batch: BatchRuntime,
                        keep_held: bool = True) -> None:
        for req in self._requests:
            if req.batch is batch and req.status == "pending":
                req.status = "cancelled"
                self.record(batch, "resource", "request_cancelled",
                            resources=req.resources, why=req.why)
        if not keep_held:
            self.release_all(batch, reason="batch abort - safe release")

    def _request_key(self, req: ResourceRequest):
        # recipe priority first, then batch sequence, then request order;
        # a request for longer is never granted ahead of an older one
        return (req.priority, req.batch.seq, req.seq)

    def arbitrate(self) -> None:
        changed = True
        while changed:
            changed = False
            for req in sorted(self._requests, key=self._request_key):
                if req.status != "pending":
                    continue
                if req.batch.intent in (Cmd.STOP, Cmd.ABORT):
                    req.status = "cancelled"
                    changed = True
                    continue
                ok = True
                for res in req.resources:
                    # strict exclusion, even between two branches of the same
                    # batch: a line may only carry one material at a time and
                    # an equipment item only be driven by one step at a time
                    if self.plant.held_by(res) is not None:
                        ok = False
                        break
                    for other in self._requests:
                        if other is req or other.status != "pending":
                            continue
                        if res not in other.resources:
                            continue
                        # a request routed through a different branch of the
                        # same batch still has to wait its turn, otherwise the
                        # two materials would meet in the line
                        if self._request_key(other) < self._request_key(req):
                            ok = False
                            break
                    if not ok:
                        break
                if not ok:
                    continue
                for res in req.resources:
                    holder = self.plant.held_by(res)
                    if holder is None:
                        self.plant.reserve(res, req.batch.batch_id,
                                           self.plant.now)
                        req.batch.held_resources.append(res)
                    else:
                        # only the same batch *and the same request* may see
                        # it already held (multi-resource request granted
                        # atomically).  Any other holder is a violation.
                        raise BatchViolation(
                            f"resource {res} held by {holder} while granting "
                            f"to {req.batch.batch_id}")
                    self.record(req.batch, "resource", "grant", resource=res,
                                why=req.why)
                req.status = "granted"
                req.granted_at = self.plant.now
                changed = True

    def release_resources(self, batch: BatchRuntime, resources: Sequence[str],
                          reason: str = "") -> None:
        for res in resources:
            if self.plant.held_by(res) == batch.batch_id:
                self.plant.release(res, batch.batch_id, self.plant.now)
                if res in batch.held_resources:
                    batch.held_resources.remove(res)
                self.record(batch, "resource", "release", resource=res,
                            reason=reason)

    def _safe_release_order(self, resources: Sequence[str]) -> List[str]:
        """Stop pumps and close valves before releasing lines; release lines
        before the tanks/equipment they connect."""
        def rank(name):
            if name in self.plant.pumps:
                return 0
            if name in self.plant.valves:
                return 1
            if name in self.plant.lines:
                return 2
            if name in self.plant.tanks:
                return 3
            return 4
        return sorted(resources, key=lambda n: (rank(n), n))

    def _physical_shutdown(self, batch: BatchRuntime, resources: Sequence[str],
                           reason: str) -> None:
        for res in resources:
            if res in self.plant.pumps:
                p = self.plant.pumps[res]
                if p.state.value != "STOPPED":
                    p.command(False, reason)
                    self.record(batch, "process", "pump", pump=res,
                                command="OFF", reason=reason)
            elif res in self.plant.valves:
                v = self.plant.valves[res]
                if v.open:
                    v.command(False, reason)
                    self.record(batch, "process", "valve", valve=res,
                                command="CLOSE", reason=reason)
            elif res in self.plant.lines:
                seg = self.plant.lines[res]
                if seg.contents is not None:
                    self.record(batch, "process", "line_drain", line=res,
                                kg=round(seg.contents.qty_kg, 3),
                                material=seg.contents.name)

    def release_all(self, batch: BatchRuntime, reason: str) -> None:
        held = self.plant.held_by_batch(batch.batch_id)
        if not held:
            return
        ordered = self._safe_release_order(held)
        self.record(batch, "resource", "safe_release_start",
                    resources=ordered, reason=reason)
        self._physical_shutdown(batch, ordered, reason)
        for res in ordered:
            if self.plant.held_by(res) == batch.batch_id:
                self.plant.release(res, batch.batch_id, self.plant.now)
                if res in batch.held_resources:
                    batch.held_resources.remove(res)
                self.record(batch, "resource", "safe_release", resource=res,
                            reason=reason)
                seg = self.plant.lines.get(res)
                if seg is not None:
                    seg.contents = None
        self.record(batch, "resource", "safe_release_done", reason=reason)

    # -- state aggregation ------------------------------------------------
    def stop_kind(self, rt: ElementRuntime) -> Optional[Cmd]:
        """ABORT beats STOP; intent is inherited from ancestors/batch."""
        node: Optional[ElementRuntime] = rt
        abort = stop = False
        while node is not None:
            if node.intent == Cmd.ABORT:
                abort = True
            elif node.intent == Cmd.STOP:
                stop = True
            node = node.parent
        if rt.batch.intent == Cmd.ABORT:
            abort = True
        elif rt.batch.intent == Cmd.STOP:
            stop = True
        if abort:
            return Cmd.ABORT
        if stop:
            return Cmd.STOP
        return None

    def phase_log(self, batch: "BatchRuntime") -> List[dict]:
        """Snapshot of every instantiated phase (by full path) with its
        state, start/end and transition trace.  Used by the reports and the
        progress page - the batch record is taken at the end, the UI at
        whatever point the run has reached."""
        out = []
        for path, p in batch.phase_index.items():
            if p.path != path or p.started_at is None:
                continue
            out.append({
                "path": path,
                "name": p.phase.name,
                "equipment": p.phase.equipment,
                "state": p.state.value,
                "started_at": p.started_at,
                "finished_at": p.finished_at,
                "transitions": list(p.transitions),
                "fault_reason": p.fault_reason,
            })
        return out

    @staticmethod
    def _terminalish(phase: "PhaseRuntime", intent: Optional[Cmd]) -> bool:
        """A phase counts as finished during stop/abort even if it never
        started (IDLE phases are cancelled, not run)."""
        if phase.state in TERMINAL_STATES:
            return True
        return intent is not None and phase.state == PhaseState.IDLE

    def all_terminal(self, rt: ElementRuntime, intent: Optional[Cmd]) -> bool:
        return all(self._terminalish(p, intent) for p in rt.phases())

    def hold_intent(self, rt: ElementRuntime) -> bool:
        """Batch-level hold: suppress dispatch of new steps anywhere.

        A hold issued on a sub-element (the propagation path) only reaches
        the phases through their own state machine; sibling operations of a
        parallel branch keep running unless the propagation scope reaches
        them.
        """
        if rt.batch.intent == Cmd.HOLD:
            return True
        node: Optional[ElementRuntime] = rt
        while node is not None:
            if node.intent == Cmd.HOLD:
                return True
            node = node.parent
        return False

    def aggregate_state(self, rt: ElementRuntime) -> RuntimeStatus:
        """State of an upper-level element, composed from the phases below.

        Precedence, worst first: ABORTING > STOPPING > HOLDING > HELD >
        RESTARTING > RUNNING > COMPLETE.  A terminal aggregate requires both
        every phase terminal and the element's own step graph finished.
        """
        if rt.end_status is not None:
            return rt.end_status
        phases = rt.phases()
        if not phases:
            return (RuntimeStatus.RUNNING if rt.started_at is not None
                    else RuntimeStatus.PENDING)
        states = [p.state for p in phases]
        intent = self.stop_kind(rt)
        if intent == Cmd.ABORT or any(
                s in (PhaseState.ABORTING, PhaseState.ABORTED) for s in states):
            return RuntimeStatus.ABORTING
        if intent == Cmd.STOP or any(
                s in (PhaseState.STOPPING, PhaseState.STOPPED) for s in states):
            return RuntimeStatus.STOPPING
        if any(s == PhaseState.HOLDING for s in states):
            return RuntimeStatus.HOLDING
        if any(s == PhaseState.HELD for s in states):
            return RuntimeStatus.HELD
        if any(s == PhaseState.RESTARTING for s in states):
            return RuntimeStatus.RESTARTING
        if all(self._terminalish(p, intent) for p in phases):
            fr = rt.frame
            if fr is not None and fr.status != FrameStatus.DONE:
                return RuntimeStatus.RUNNING
            if any(s == PhaseState.ABORTED for s in states):
                return RuntimeStatus.ABORTED
            if any(s == PhaseState.STOPPED for s in states):
                return RuntimeStatus.STOPPED
            return RuntimeStatus.COMPLETE
        return RuntimeStatus.RUNNING

    def state_of(self, rt: ElementRuntime) -> str:
        if rt.end_status is not None:
            return rt.end_status.value
        if isinstance(rt, PhaseRuntime):
            return rt.state.value
        return self.aggregate_state(rt).value

    # -- hold propagation -------------------------------------------------
    def on_phase_fault(self, phase: PhaseRuntime) -> None:
        """A phase failed while running: hold it, then apply its scope."""
        reason = phase.hold_reason or "fault"
        if is_legal(phase.state, Cmd.HOLD):
            phase.command(Cmd.HOLD, reason, source="fault")
        scope = phase.phase.propagation
        self.record(phase.batch, "fault", "propagation", phase=phase.path,
                    scope=scope, reason=reason)
        if scope == "self":
            return
        target: Optional[ElementRuntime] = phase.parent
        if scope == "unit_procedure":
            while target is not None and target.kind != "unit_procedure":
                target = target.parent
        elif scope == "batch":
            target = phase.batch.root
        if target is None:
            return
        self.hold_subtree(target, reason=f"propagated: {reason}", exclude=phase)
        node: Optional[ElementRuntime] = target
        while node is not None:
            node.intent = Cmd.HOLD
            node = node.parent

    def clear_hold_intent(self, rt: ElementRuntime) -> None:
        if rt.intent == Cmd.HOLD:
            rt.intent = None
        for child in rt.children:
            self.clear_hold_intent(child)

    def hold_subtree(self, target: ElementRuntime, reason: str,
                     exclude: Optional[PhaseRuntime] = None) -> None:
        held = []
        for p in target.phases():
            if p is exclude:
                continue
            if p.state in (PhaseState.RUNNING, PhaseState.RESTARTING):
                if p.command(Cmd.HOLD, reason, source="propagation"):
                    held.append(p.path)
        self.record(target.batch, "state", "propagate_hold", path=target.path,
                    scope=target.kind, reason=reason, phases=held)

    # -- commands on arbitrary targets ------------------------------------
    def command(self, target, cmd: Cmd, reason: str = "",
                source: str = "operator") -> bool:
        batch, rt = self._resolve(target)
        path = rt.path if rt is not None else "<batch>"
        if cmd in (Cmd.HOLD, Cmd.RESTART):
            if rt is None or rt is batch.root:
                ok = batch.hold(reason) if cmd == Cmd.HOLD else batch.restart(reason)
            else:
                ok = self._element_command(batch, rt, cmd, reason, source)
        elif cmd in (Cmd.STOP, Cmd.ABORT):
            if rt is None or rt is batch.root:
                ok = batch.stop(reason) if cmd == Cmd.STOP else batch.abort(reason)
            else:
                ok = self._element_command(batch, rt, cmd, reason, source)
        elif cmd == Cmd.RESET:
            if not isinstance(rt, PhaseRuntime):
                self.reject(batch, path, "n/a", cmd.value, source,
                            "reset needs a phase")
                return False
            ok = rt.command(cmd, reason, source)
        elif cmd == Cmd.START:
            self.reject(batch, path, "n/a", cmd.value, source,
                        "start is driven by the recipe sequence")
            return False
        else:  # pragma: no cover
            raise ValueError(cmd)
        self.command_log.append({
            "t": self.plant.now, "batch": batch.batch_id, "path": path,
            "state": None, "command": cmd.value, "source": source,
            "result": "ACCEPTED" if ok else "REJECTED", "reason": reason,
        })
        self.refresh_state(batch)
        return ok

    def _element_command(self, batch: BatchRuntime, rt: ElementRuntime,
                         cmd: Cmd, reason: str, source: str) -> bool:
        phases = rt.phases()
        if cmd == Cmd.HOLD:
            targets = [p for p in phases if p.state in
                       (PhaseState.RUNNING, PhaseState.RESTARTING)]
        elif cmd == Cmd.RESTART:
            targets = [p for p in phases if p.state == PhaseState.HELD]
        elif cmd == Cmd.STOP:
            targets = [p for p in phases if p.state in ACTIVE_STATES
                       and p.state not in (PhaseState.STOPPING,
                                           PhaseState.ABORTING)]
        else:
            targets = [p for p in phases if p.state in ACTIVE_STATES
                       and p.state != PhaseState.ABORTING]
        accepted = False
        for p in targets:
            accepted = p.command(cmd, reason, source) or accepted
        if cmd in (Cmd.STOP, Cmd.ABORT):
            rt.intent = cmd
            if rt is batch.root:
                batch.intent = cmd
            self.cancel_requests(batch, keep_held=True)
            self.record(batch, "state",
                        "abort_element" if cmd == Cmd.ABORT else "stop_element",
                        path=rt.path, reason=reason,
                        phases=[p.path for p in targets])
        elif cmd == Cmd.HOLD and accepted:
            node: Optional[ElementRuntime] = rt
            while node is not None:
                if node.intent is None:
                    node.intent = Cmd.HOLD
                node = node.parent
            self.record(batch, "state", "hold_element", path=rt.path,
                        reason=reason, phases=[p.path for p in targets])
        elif cmd == Cmd.RESTART and accepted:
            node = rt
            while node is not None:
                if node.intent == Cmd.HOLD:
                    node.intent = None
                node = node.parent
            self.clear_hold_intent(rt)
            self.record(batch, "state", "restart_element", path=rt.path,
                        reason=reason, phases=[p.path for p in targets])
        if not accepted and cmd in (Cmd.HOLD, Cmd.RESTART):
            self.reject(batch, rt.path, self.state_of(rt), cmd.value, source,
                        "nothing to hold" if cmd == Cmd.HOLD else "nothing held")
        return accepted

    def _resolve(self, target) -> Tuple[BatchRuntime, Optional[ElementRuntime]]:
        if isinstance(target, BatchRuntime):
            return target, target.root
        if isinstance(target, str):
            if ":" in target:
                bid, path = target.split(":", 1)
            else:
                bid, path = target, ""
            batch = next((b for b in self.batches if b.batch_id == bid), None)
            if batch is None:
                raise KeyError(f"unknown batch {bid!r}")
            if not path:
                return batch, batch.root
            rt = batch.element_runtime(path)
            if rt is None:
                try:
                    rt = batch.phase(path)
                except KeyError:
                    rt = None
            if rt is None:
                raise KeyError(f"unknown element {path!r} in batch {bid}")
            return batch, rt
        raise TypeError(f"cannot resolve command target {target!r}")

    # -- ticking ----------------------------------------------------------
    def tick(self, dt_ms: Optional[int] = None) -> None:
        dt = dt_ms if dt_ms is not None else self.tick_ms
        self.plant.step(dt)
        self.arbitrate()
        for batch in self.batches:
            self._tick_batch(batch)
        self._check_line_invariant()

    def _tick_batch(self, batch: BatchRuntime) -> None:
        if batch.is_done():
            return
        root = batch.root
        if root is None:
            return
        for p in batch.phases():
            p.tick()
        if root.frame is not None:
            root.frame.tick()
        self.refresh_state(batch)

    def refresh_state(self, batch: BatchRuntime) -> None:
        root = batch.root
        if root is None or batch.is_done():
            return
        if root.end_status is not None:
            batch.state = root.end_status
            batch.finished_at = self.plant.now
            # a finished or stopped batch must not sit on shared equipment:
            # everything it still holds is released in safe order
            self.release_all(batch, reason=f"batch {batch.state.value.lower()}")
            self.record(batch, "batch", "batch_end", state=batch.state.value)
            self.capture_report(batch)
        else:
            batch.state = self.aggregate_state(root)

    def _check_line_invariant(self) -> None:
        """Hard invariant: a line segment carries at most one batch's material.

        Reservation already prevents this by construction; this guard is
        independent of it and records a violation if anything bypasses the
        arbiter or if a segment still holds material after release.
        """
        for name, seg in self.plant.lines.items():
            holder = self.plant.held_by(name)
            if seg.contents is not None and seg.contents.qty_kg > 0 \
                    and holder is None:
                msg = f"line {name} carries material with no reservation"
                if msg not in self.violations:
                    self.violations.append(msg)

    # -- batch record -----------------------------------------------------
    def capture_report(self, batch: BatchRuntime) -> dict:
        phases = []
        seen = set()
        for p in batch.phase_index.values():
            if p.path in seen or p.started_at is None:
                continue
            seen.add(p.path)
            phases.append({
                "path": p.path,
                "name": p.phase.name,
                "equipment": p.phase.equipment,
                "description": p.phase.description,
                "propagation": p.phase.propagation,
                "state": p.state.value,
                "final": p.end_status.value if p.end_status else None,
                "transitions": list(p.transitions),
                "hold_reason": p.hold_reason,
                "fault_reason": p.fault_reason,
                "started_at": p.started_at,
                "finished_at": p.finished_at,
                "started": fmt_ms(p.started_at) if p.started_at is not None else "",
                "finished": fmt_ms(p.finished_at) if p.finished_at is not None else "",
            })
        held = self.plant.held_by_batch(batch.batch_id)
        report = {
            "batch_id": batch.batch_id,
            "recipe": {
                "id": batch.recipe.recipe_id,
                "name": batch.recipe.name,
                "version": batch.recipe.version,
                "product": batch.recipe.product,
                "batch_size_kg": batch.recipe.batch_size_kg * batch.scale,
            },
            "lot": batch.lot,
            "scale": batch.scale,
            "priority": batch.priority,
            "sequence": batch.seq,
            "state": batch.state.value,
            "started_at": batch.started_at,
            "finished_at": batch.finished_at,
            "started": (fmt_ms(batch.started_at)
                        if batch.started_at is not None else ""),
            "finished": (fmt_ms(batch.finished_at)
                         if batch.finished_at is not None else ""),
            "phases": phases,
            "tanks": {
                name: {
                    "kg": round(t.total_kg(), 2),
                    "temp_c": round(t.temperature(), 2),
                    "agitator": round(t.agitator_speed(), 3),
                    "contents": t.content.name if t.content else "",
                }
                for name, t in self.plant.tanks.items()
            },
            "held_resources_at_end": held,
            "line_spans": [
                {"line": name, **span}
                for name, seg in self.plant.lines.items()
                for span in seg.history if span["batch"] == batch.batch_id
            ],
            "reservations": [
                {"t": t, "time": fmt_ms(t), "batch": b, "resource": r,
                 "action": a}
                for (t, b, r, a) in self.plant.reservation_log
                if b == batch.batch_id
            ],
            "alarms": list(batch.alarms),
            "violations": list(batch.violations),
            "events": list(batch.record),
        }
        batch.final_report = report
        return report

    def report(self) -> dict:
        for b in self.batches:
            if b.final_report is None:
                if b.root is not None:
                    b.state = (b.root.end_status if b.root.end_status is not None
                               else self.aggregate_state(b.root))
                self.capture_report(b)
        return {
            "started": fmt_ms(0),
            "finished": fmt_ms(self.plant.now),
            "total_ms": self.plant.now,
            "plant": self.plant.snapshot(),
            "batches": [b.final_report for b in self.batches],
            "faults": list(self.plant.injected_faults),
            "reservation_log": [
                {"t": t, "time": fmt_ms(t), "batch": b, "resource": r,
                 "action": a}
                for (t, b, r, a) in self.plant.reservation_log
            ],
            "violations": list(self.violations),
        }
