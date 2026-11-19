"""Discrete-step simulation of the plant.

Everything the recipe can touch lives here: materials (name, mass,
temperature, lot), valves, pumps, tanks/reactors, mills, and the shared
transfer-line segments that batches must reserve before using.

The simulation is stepped with ``Plant.step()``.  Time is integer
milliseconds so the whole run is deterministic and replayable.

Process model
-------------
* ``Tank``: mass, temperature (heater + passive cooling), agitator with a
  speed ramp, capacity.  Charging blends material; transfers move mass
  between tanks through a line.
* ``Valve``: stroke time in each direction.  A stuck valve does not move
  even though the actuator is commanded, and reports the mismatch.
* ``Pump``: spin-up/down ramp and a nominal flow; a tripped pump will not
  start.
* ``Machine``: mills and similar rotary equipment (spin-up time).
* ``LineSegment``: carries at most one batch's material at a time; every
  transfer records a span ``(start, end, batch, material, kg)`` so the
  verifier can prove no two batches ever overlapped on it.

Faults
------
``stick_valve``, ``fail_pump``, ``fail_machine``, ``fail_temp_sensor``
(either no signal or a stuck value).  Sensor faults are visible to control
logic through ``read_temperature`` - that is the point of the drill.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional

from .errors import EquipmentError, SensorError

MS = 1


def fmt_ms(ms: Optional[int]) -> str:
    if ms is None:
        return ""
    ms = int(ms)
    s, msec = divmod(ms, 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{msec:03d}"


def fmt_s(ms: Optional[int]) -> str:
    if ms is None:
        return ""
    return f"{int(ms) / 1000:.1f}s"


# --------------------------------------------------------------------------
# materials
# --------------------------------------------------------------------------
@dataclass
class Material:
    name: str
    qty_kg: float = 0.0
    temp_c: float = 20.0
    lot: str = ""

    def copy(self) -> "Material":
        return Material(self.name, self.qty_kg, self.temp_c, self.lot)

    def blend(self, other: "Material") -> None:
        total = self.qty_kg + other.qty_kg
        if total <= 0:
            self.temp_c = (self.temp_c + other.temp_c) / 2.0
        else:
            self.temp_c = (
                self.temp_c * self.qty_kg + other.temp_c * other.qty_kg
            ) / total
        self.qty_kg = total
        self.lot = "+".join(x for x in (self.lot, other.lot) if x)[:160]


# --------------------------------------------------------------------------
# equipment base
# --------------------------------------------------------------------------
class EquipState(str, Enum):
    CLOSED = "CLOSED"
    OPENING = "OPENING"
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"


class Equipment:
    name: str
    kind: str

    def step(self, dt_ms: int) -> None:  # pragma: no cover - interface
        raise NotImplementedError


def _ramp_factor(state: EquipState, left: int, up_ms: int, down_ms: int) -> float:
    if state == EquipState.RUNNING:
        return 1.0
    if state == EquipState.STARTING and up_ms > 0:
        return max(0.0, 1.0 - left / up_ms)
    if state == EquipState.STOPPING and down_ms > 0:
        return max(0.0, left / down_ms)
    return 0.0


class Valve(Equipment):
    def __init__(self, name: str, travel_ms: int = 3000):
        self.name = name
        self.kind = "valve"
        self.state = EquipState.CLOSED
        self.travel_ms = travel_ms
        self.stuck = False
        self.stuck_at: Optional[EquipState] = None
        self._target = EquipState.CLOSED
        self._remaining = 0
        self.last_command = ""

    def command(self, want_open: bool, why: str = "") -> None:
        self.last_command = ("OPEN" if want_open else "CLOSE") + (
            f" ({why})" if why else "")
        target = EquipState.OPEN if want_open else EquipState.CLOSED
        self._target = target
        if self.stuck:
            # actuator effort applied, nothing moves
            self._remaining = self.travel_ms
            return
        if self.state == target:
            self._remaining = 0
            return
        self._remaining = self.travel_ms
        self.state = (EquipState.OPENING if target == EquipState.OPEN
                      else EquipState.CLOSING)

    def step(self, dt_ms: int) -> None:
        if self._remaining <= 0:
            return
        self._remaining -= dt_ms
        if self._remaining > 0:
            return
        self._remaining = 0
        if self.stuck:
            self.state = self.stuck_at or self.state
            return
        self.state = self._target

    @property
    def open(self) -> bool:
        return self.state == EquipState.OPEN

    @property
    def commanded_open(self) -> bool:
        return self._target == EquipState.OPEN

    @property
    def mismatch(self) -> bool:
        return self.stuck and self.commanded_open != self.open


class Pump(Equipment):
    def __init__(self, name: str, flow_kg_s: float = 40.0,
                 ramp_up_ms: int = 1500, ramp_down_ms: int = 800):
        self.name = name
        self.kind = "pump"
        self.state = EquipState.STOPPED
        self.flow_kg_s = flow_kg_s
        self.ramp_up_ms = ramp_up_ms
        self.ramp_down_ms = ramp_down_ms
        self.failed = False
        self._target_on = False
        self._ramp_left = 0
        self.last_command = ""

    def command(self, on: bool, why: str = "") -> None:
        self.last_command = ("ON" if on else "OFF") + (
            f" ({why})" if why else "")
        if on and self.failed:
            self._target_on = False
            return
        if on == self._target_on and self.state in (EquipState.RUNNING,
                                                    EquipState.STARTING):
            return
        self._target_on = on
        if on:
            self.state = EquipState.STARTING
            self._ramp_left = self.ramp_up_ms
        else:
            self.state = EquipState.STOPPING
            self._ramp_left = self.ramp_down_ms

    def step(self, dt_ms: int) -> None:
        if self._ramp_left <= 0:
            return
        self._ramp_left -= dt_ms
        if self._ramp_left <= 0:
            self._ramp_left = 0
            self.state = (EquipState.RUNNING if self._target_on
                          else EquipState.STOPPED)

    def current_flow(self) -> float:
        return self.flow_kg_s * _ramp_factor(self.state, self._ramp_left,
                                             self.ramp_up_ms,
                                             self.ramp_down_ms)


class Machine(Equipment):
    """A mill or similar rotary machine (exclusive equipment)."""

    def __init__(self, name: str, spin_up_ms: int = 4000,
                 spin_down_ms: int = 3000, throughput_kg_s: float = 0.0):
        self.name = name
        self.kind = "machine"
        self.state = EquipState.STOPPED
        self.spin_up_ms = spin_up_ms
        self.spin_down_ms = spin_down_ms
        self.throughput_kg_s = throughput_kg_s
        self.failed = False
        self.run_ms = 0
        self._target_on = False
        self._ramp_left = 0
        self.last_command = ""

    def command(self, on: bool, why: str = "") -> None:
        self.last_command = ("ON" if on else "OFF") + (
            f" ({why})" if why else "")
        if on and self.failed:
            self._target_on = False
            return
        if on == self._target_on and self.state in (EquipState.RUNNING,
                                                    EquipState.STARTING):
            return
        self._target_on = on
        if on:
            self.state = EquipState.STARTING
            self._ramp_left = self.spin_up_ms
        else:
            self.state = EquipState.STOPPING
            self._ramp_left = self.spin_down_ms

    def step(self, dt_ms: int) -> None:
        if self.state == EquipState.RUNNING:
            self.run_ms += dt_ms
        if self._ramp_left <= 0:
            return
        self._ramp_left -= dt_ms
        if self._ramp_left <= 0:
            self._ramp_left = 0
            self.state = (EquipState.RUNNING if self._target_on
                          else EquipState.STOPPED)

    def speed(self) -> float:
        return _ramp_factor(self.state, self._ramp_left, self.spin_up_ms,
                            self.spin_down_ms)


class Tank(Equipment):
    def __init__(self, name: str, capacity_kg: float = 8000.0,
                 agitator_ms: int = 1500, heater_w: float = 0.0,
                 ambient_c: float = 22.0, kind: str = "tank"):
        self.name = name
        self.kind = kind
        self.capacity_kg = capacity_kg
        self.content: Optional[Material] = None
        self.agitator_on = False
        self.agitator_ms = agitator_ms
        self.agitator_ramp_left = 0
        self.heater_w = 0.0
        self.max_heater_w = heater_w
        self.ambient_c = ambient_c
        self.max_temp_c = 95.0
        self.temp_sensor_failed = False
        self.temp_sensor_stuck: Optional[float] = None
        self.mixer_ms = 0.0
        self._commanded_ag = False

    # -- material ---------------------------------------------------------
    def add_material(self, other: Material) -> None:
        if other.qty_kg <= 0:
            return
        if self.content is None:
            self.content = other.copy()
        else:
            self.content.blend(other)
        if self.content.qty_kg > self.capacity_kg + 1e-6:
            self.content.qty_kg = self.capacity_kg

    def remove(self, kg: float) -> Material:
        if self.content is None or kg <= 0:
            return Material("empty", 0.0, self.temperature())
        kg = min(kg, self.content.qty_kg)
        out = Material(self.content.name, kg, self.content.temp_c,
                       self.content.lot)
        self.content.qty_kg -= kg
        if self.content.qty_kg <= 1e-9:
            self.content = None
        return out

    def drain(self) -> Material:
        return self.remove(self.total_kg())

    # -- process quantities ----------------------------------------------
    def total_kg(self) -> float:
        return self.content.qty_kg if self.content else 0.0

    def level_pct(self) -> float:
        return 100.0 * self.total_kg() / self.capacity_kg

    def temperature(self) -> float:
        return self.content.temp_c if self.content else self.ambient_c

    def read_temperature(self) -> float:
        """What the control system *sees*."""
        if self.temp_sensor_failed:
            raise SensorError(f"temperature sensor of {self.name} failed")
        if self.temp_sensor_stuck is not None:
            return self.temp_sensor_stuck
        return self.temperature()

    def sensor_ok(self) -> bool:
        return not self.temp_sensor_failed and self.temp_sensor_stuck is None

    # -- agitator ---------------------------------------------------------
    def command_agitator(self, on: bool, why: str = "") -> None:
        self._commanded_ag = on
        if on:
            if not self.agitator_on:
                self.agitator_ramp_left = self.agitator_ms
            self.agitator_on = True
        else:
            self.agitator_on = False
            self.agitator_ramp_left = 0

    def agitator_speed(self) -> float:
        if not self.agitator_on:
            return 0.0
        if self.agitator_ramp_left > 0:
            return max(0.0, 1.0 - self.agitator_ramp_left
                       / max(1, self.agitator_ms))
        return 1.0

    # -- physics ----------------------------------------------------------
    def step(self, dt_ms: int) -> None:
        if self.agitator_ramp_left > 0:
            self.agitator_ramp_left = max(0, self.agitator_ramp_left - dt_ms)
        if self.content is None:
            return
        dt = dt_ms / 1000.0
        mass = self.content.qty_kg
        if mass <= 0:
            return
        if self.heater_w:
            self.content.temp_c += self.heater_w * dt / (mass * 1800.0)
        # passive cooling, time constant ~20000 s
        self.content.temp_c += ((self.ambient_c - self.content.temp_c)
                                * 0.00005 * dt)
        self.content.temp_c = max(self.ambient_c,
                                  min(self.content.temp_c, 150.0))
        if self.agitator_speed() > 0:
            self.mixer_ms += self.agitator_speed() * dt_ms


class LineSegment(Equipment):
    """A shared transfer line; at most one batch's material at a time."""

    def __init__(self, name: str):
        self.name = name
        self.kind = "line"
        self.holder: Optional[str] = None
        #: token of the transfer currently pushing material through the line
        self.active_token: Optional[str] = None
        self.contents: Optional[Material] = None
        #: one span per (batch, material, from, to) run:
        #: {"start","end","batch","material","kg","from","to"}
        self.history: List[dict] = []

    def step(self, dt_ms: int) -> None:
        pass

    def transfer(self, when: int, batch: str, mat: Material, from_name: str,
                 to_name: str) -> None:
        if self.holder not in (None, batch):
            raise EquipmentError(
                f"line {self.name} carries {self.holder} while {batch} "
                f"tries to transfer")
        if self.contents is None:
            self.contents = mat.copy()
        else:
            self.contents.qty_kg += mat.qty_kg
        self.holder = batch
        last = self.history[-1] if self.history else None
        if (last is not None and last["batch"] == batch
                and last["material"] == mat.name
                and last["from"] == from_name and last["to"] == to_name):
            last["end"] = when
            last["kg"] += mat.qty_kg
        else:
            self.history.append({
                "start": when, "end": when, "batch": batch,
                "material": mat.name, "kg": mat.qty_kg,
                "from": from_name, "to": to_name,
            })

    def clear(self) -> None:
        self.contents = None
        self.active_token = None


# --------------------------------------------------------------------------
# the plant
# --------------------------------------------------------------------------
class Plant:
    def __init__(self):
        self.now = 0
        self.valves: Dict[str, Valve] = {}
        self.pumps: Dict[str, Pump] = {}
        self.tanks: Dict[str, Tank] = {}
        self.machines: Dict[str, Machine] = {}
        self.lines: Dict[str, LineSegment] = {}
        self.reservations: Dict[str, str] = {}
        self.reservation_log: List[tuple] = []
        self._scenario: List[tuple] = []
        self._seq = 0
        self.injected_faults: List[dict] = []
        self.waste: Optional[Tank] = None

    # -- construction -----------------------------------------------------
    def add_valve(self, name, travel_ms=3000) -> Valve:
        v = Valve(name, travel_ms=travel_ms)
        self.valves[name] = v
        return v

    def add_pump(self, name, flow_kg_s=40.0, ramp_up_ms=1500,
                 ramp_down_ms=800) -> Pump:
        p = Pump(name, flow_kg_s=flow_kg_s, ramp_up_ms=ramp_up_ms,
                 ramp_down_ms=ramp_down_ms)
        self.pumps[name] = p
        return p

    def add_tank(self, name, capacity_kg=8000.0, agitator_ms=1500,
                 heater_w=0.0, ambient_c=22.0) -> Tank:
        t = Tank(name, capacity_kg=capacity_kg, agitator_ms=agitator_ms,
                 heater_w=heater_w, ambient_c=ambient_c)
        self.tanks[name] = t
        return t

    def add_machine(self, name, spin_up_ms=4000, spin_down_ms=3000) -> Machine:
        m = Machine(name, spin_up_ms=spin_up_ms, spin_down_ms=spin_down_ms)
        self.machines[name] = m
        return m

    def add_line(self, name) -> LineSegment:
        seg = LineSegment(name)
        self.lines[name] = seg
        return seg

    # -- material movement ------------------------------------------------
    def transfer(self, when: int, batch: str, line: str, mat: Material,
                 from_name: str, to_name: str) -> None:
        self.lines[line].transfer(when, batch, mat, from_name, to_name)

    # -- one transfer at a time per line ----------------------------------
    def begin_transfer(self, line: str, batch: str, token: str) -> None:
        """Claim the line for one transfer.  Two batches - or two branches of
        the same batch - may never push material at the same time."""
        seg = self.lines[line]
        holder = self.reservations.get(line)
        if holder != batch:
            raise EquipmentError(
                f"batch {batch} does not hold line {line} (holder={holder})")
        if seg.active_token not in (None, token):
            raise EquipmentError(
                f"line {line} is already carrying material for {seg.active_token}")
        seg.active_token = token

    def end_transfer(self, line: str, token: str) -> None:
        seg = self.lines[line]
        if seg.active_token == token:
            seg.active_token = None

    def abandon_transfers(self, batch: str) -> None:
        for seg in self.lines.values():
            if seg.active_token and seg.active_token.startswith(batch):
                seg.active_token = None

    def drain_to_waste(self, when: int, batch: str, tank_name: str,
                       line: str = "") -> float:
        tank = self.tanks[tank_name]
        mat = tank.drain()
        if mat.qty_kg <= 0:
            return 0.0
        if line:
            self.transfer(when, batch, line, mat, tank_name,
                          self.waste.name if self.waste else "waste")
        if self.waste is not None:
            self.waste.add_material(mat)
        return mat.qty_kg

    # -- shared resources -------------------------------------------------
    def reserve(self, resource: str, batch: str, when: int) -> None:
        holder = self.reservations.get(resource)
        if holder is not None and holder != batch:
            raise EquipmentError(f"{resource} already held by batch {holder}")
        self.reservations[resource] = batch
        self.reservation_log.append((when, batch, resource, "ACQUIRE"))

    def release(self, resource: str, batch: str, when: int) -> None:
        holder = self.reservations.get(resource)
        if holder != batch:
            raise EquipmentError(
                f"batch {batch} does not hold {resource} (holder={holder})")
        del self.reservations[resource]
        seg = self.lines.get(resource)
        if seg is not None:
            # a released line is empty and free for the next batch - retaining
            # the previous holder's material or token here would let a stale
            # transfer block or contaminate the next batch
            seg.holder = None
            seg.contents = None
            seg.active_token = None
        self.reservation_log.append((when, batch, resource, "RELEASE"))

    def held_by(self, resource: str) -> Optional[str]:
        return self.reservations.get(resource)

    def held_by_batch(self, batch: str) -> List[str]:
        return sorted(r for r, h in self.reservations.items() if h == batch)

    def equipment_names(self) -> List[str]:
        return (list(self.tanks) + list(self.pumps) + list(self.valves)
                + list(self.machines) + list(self.lines))

    # -- faults -----------------------------------------------------------
    def stick_valve(self, name: str) -> None:
        v = self.valves[name]
        if v.stuck:
            return
        v.stuck = True
        v.stuck_at = v.state
        self.injected_faults.append({
            "at": self.now, "time": fmt_ms(self.now), "type": "valve_stuck",
            "target": name, "detail": f"frozen in {v.state.value}"})

    def free_valve(self, name: str) -> None:
        v = self.valves[name]
        v.stuck = False
        v.stuck_at = None

    def fail_pump(self, name: str) -> None:
        p = self.pumps[name]
        p.failed = True
        p.command(False, "trip")
        self.injected_faults.append({
            "at": self.now, "time": fmt_ms(self.now), "type": "pump_failed",
            "target": name, "detail": "motor trip, will not restart"})

    def fail_machine(self, name: str) -> None:
        m = self.machines[name]
        m.failed = True
        m.command(False, "trip")
        self.injected_faults.append({
            "at": self.now, "time": fmt_ms(self.now), "type": "machine_failed",
            "target": name, "detail": "mill trip, will not restart"})

    def fail_temp_sensor(self, tank: str,
                         stuck_value: Optional[float] = None) -> None:
        t = self.tanks[tank]
        if stuck_value is not None:
            t.temp_sensor_stuck = stuck_value
            detail = f"stuck at {stuck_value:.1f} C"
        else:
            t.temp_sensor_failed = True
            detail = "no signal"
        self.injected_faults.append({
            "at": self.now, "time": fmt_ms(self.now), "type": "sensor_failed",
            "target": tank, "detail": "temperature " + detail})

    def fix_temp_sensor(self, tank: str) -> None:
        t = self.tanks[tank]
        t.temp_sensor_failed = False
        t.temp_sensor_stuck = None

    # -- scenario scheduling ---------------------------------------------
    def at(self, when_ms: int, fn: Callable[[], None], label: str = "") -> None:
        self._seq += 1
        heapq.heappush(self._scenario, (when_ms, self._seq, fn, label))

    def run_scenario(self) -> None:
        while self._scenario and self._scenario[0][0] <= self.now:
            _when, _seq, fn, _label = heapq.heappop(self._scenario)
            fn()

    # -- time -------------------------------------------------------------
    def step(self, dt_ms: int = 100) -> None:
        self.now += dt_ms
        self.run_scenario()
        for v in self.valves.values():
            v.step(dt_ms)
        for p in self.pumps.values():
            p.step(dt_ms)
        for m in self.machines.values():
            m.step(dt_ms)
        for t in self.tanks.values():
            t.step(dt_ms)

    # -- inspection -------------------------------------------------------
    def total_mass_kg(self) -> float:
        return round(sum(t.total_kg() for t in self.tanks.values()), 6)

    def snapshot(self) -> dict:
        return {
            "now": self.now,
            "tanks": {
                n: {"kg": round(t.total_kg(), 2),
                    "temp_c": round(t.temperature(), 2),
                    "ag": round(t.agitator_speed(), 2),
                    "contents": t.content.name if t.content else ""}
                for n, t in self.tanks.items()
            },
            "pumps": {
                n: {"state": p.state.value,
                    "flow_kg_s": round(p.current_flow(), 2),
                    "failed": p.failed}
                for n, p in self.pumps.items()
            },
            "valves": {
                n: {"state": v.state.value, "stuck": v.stuck,
                    "commanded_open": v.commanded_open}
                for n, v in self.valves.items()
            },
            "machines": {
                n: {"state": m.state.value, "speed": round(m.speed(), 2),
                    "run_ms": m.run_ms, "failed": m.failed}
                for n, m in self.machines.items()
            },
            "reservations": dict(self.reservations),
        }
