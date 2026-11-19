"""Equipment operations available to phase authors.

Every operation is a small generator driven by :class:`PhaseContext`.  An op
yields :class:`Tick` when it wants to be resumed after the next plant step,
or :class:`Wait` with a predicate when it is waiting for a condition.  The
engine steps the plant, then resumes the generator.

This is deliberately not a scripting language: an op either succeeds or
raises :class:`OpFailed`, which the phase runtime turns into a hold (the
recipe's propagation rule then decides who else holds).  Ops are the same
building blocks in the main sequence and in the written hold / restart /
stop / abort sequences.

Ops that change material are *idempotent by checkpoint*: the phase context
keeps a small dict keyed by the op's position in its sequence, so a phase
that is restarted after a fault resumes at the failed op instead of doing
earlier work twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .errors import OpFailed
from .sim import Material


@dataclass
class Tick:
    pass


@dataclass
class Wait:
    predicate: Callable[[], bool]
    label: str = "wait"

    def ready(self) -> bool:
        return bool(self.predicate())


class Op:
    label = "op"

    def __call__(self, ctx):  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> str:
        return self.label


# --------------------------------------------------------------------------
# materials / tanks
# --------------------------------------------------------------------------
class Charge(Op):
    """Add ``kg`` of ``material`` at ``temp_c`` to a tank."""

    def __init__(self, tank: str, material: str, kg: float = 0.0,
                 temp_c: float = 22.0, lot: str = ""):
        self.tank = tank
        self.material = material
        self.kg = kg
        self.temp_c = temp_c
        self.lot = lot

    def describe(self):
        return f"charge {self.material} {self.kg:g}kg -> {self.tank}"

    def __call__(self, ctx):
        kg = ctx.qty(self.kg)
        key = ctx.op_key + ".charged"
        done = float(ctx.state.get(key, 0.0))
        if done >= kg - 1e-9:
            ctx.log(f"{self.material} already charged, skipping")
            yield Tick()
            return
        tank = ctx.plant.tanks[self.tank]
        if tank.total_kg() + kg > tank.capacity_kg + 1e-6:
            raise OpFailed(
                f"{self.tank}: charge of {kg:.1f}kg would overflow "
                f"({tank.total_kg():.1f}/{tank.capacity_kg:.1f}kg)"
            )
        tank.add_material(
            Material(self.material, kg, ctx.temp(self.temp_c), self.lot or ctx.lot)
        )
        ctx.state[key] = kg
        ctx.log(f"charged {kg:g} kg {self.material} into {self.tank}")
        ctx.event("charge", tank=self.tank, material=self.material,
                  kg=round(kg, 3), temp_c=round(tank.temperature(), 2),
                  lot=self.lot or ctx.lot)
        yield Tick()
        yield Wait(lambda: True, "charge settled")


class Agitate(Op):
    def __init__(self, tank: str, seconds: float = 0.0, label: str = ""):
        self.tank = tank
        self.seconds = seconds
        self._label = label

    def describe(self):
        return self._label or f"agitate {self.tank} {self.seconds:g}s"

    def __call__(self, ctx):
        tank = ctx.plant.tanks[self.tank]
        if not tank.agitator_on:
            tank.command_agitator(True, "agitate")
            ctx.event("agitator", tank=self.tank, on=True)
            ctx.log(f"agitator {self.tank} on")
        if self.seconds > 0:
            base = ctx.op_elapsed_s()
            start = ctx.state.setdefault(ctx.op_key + ".ag_start", base)
            yield Wait(
                lambda: ctx.op_elapsed_s() - start >= self.seconds,
                f"mixing {self.tank}")
        ctx.log(f"agitated {self.tank}")
        yield Tick()


class StopAgitator(Op):
    def __init__(self, tank: str):
        self.tank = tank

    def describe(self):
        return f"stop agitator {self.tank}"

    def __call__(self, ctx):
        ctx.plant.tanks[self.tank].command_agitator(False, "stop")
        ctx.event("agitator", tank=self.tank, on=False)
        ctx.log(f"agitator {self.tank} off")
        yield Tick()


class Heater(Op):
    """Direct heater control, used by hold/restart sequences."""

    def __init__(self, tank: str, watts: float):
        self.tank = tank
        self.watts = watts

    def describe(self):
        return f"heater {self.tank} {'on' if self.watts else 'off'}"

    def __call__(self, ctx):
        ctx.plant.tanks[self.tank].heater_w = self.watts
        ctx.event("heater", tank=self.tank, on=bool(self.watts), watts=self.watts)
        yield Tick()


class HeatTo(Op):
    """Heat a tank to a set point using the tank heater.

    Reads the *control* temperature, so a failed sensor is a real fault: the
    op fails, the phase goes to hold, and the propagation rule decides what
    else holds.
    """

    def __init__(self, tank: str, setpoint_c: float, heater_w: float = 60000.0,
                 timeout_s: float = 3600.0):
        self.tank = tank
        self.setpoint_c = setpoint_c
        self.heater_w = heater_w
        self.timeout_s = timeout_s

    def describe(self):
        return f"heat {self.tank} to {self.setpoint_c:g}C"

    def __call__(self, ctx):
        tank = ctx.plant.tanks[self.tank]
        ctx.event("heater", tank=self.tank, on=True, setpoint_c=self.setpoint_c)
        while True:
            # re-assert every step: a hold sequence switches the heater off,
            # and the restart sequence must not have to know this op exists
            tank.heater_w = self.heater_w
            try:
                seen = tank.read_temperature()
            except Exception as exc:  # SensorError
                tank.heater_w = 0.0
                raise OpFailed(f"temperature sensor fault on {self.tank}: {exc}")
            if seen >= self.setpoint_c:
                break
            if ctx.op_elapsed_s() > self.timeout_s:
                tank.heater_w = 0.0
                raise OpFailed(
                    f"{self.tank} did not reach {self.setpoint_c:g}C within "
                    f"{self.timeout_s:g}s (seen {seen:.1f}C)")
            yield Tick()
        tank.heater_w = 0.0
        ctx.event("heater", tank=self.tank, on=False,
                  temp_c=round(tank.temperature(), 2))
        ctx.log(f"{self.tank} reached {tank.temperature():.1f}C")
        yield Tick()


class CoolTo(Op):
    def __init__(self, tank: str, setpoint_c: float, timeout_s: float = 7200.0):
        self.tank = tank
        self.setpoint_c = setpoint_c
        self.timeout_s = timeout_s

    def describe(self):
        return f"cool {self.tank} to {self.setpoint_c:g}C"

    def __call__(self, ctx):
        tank = ctx.plant.tanks[self.tank]
        tank.heater_w = 0.0
        while True:
            try:
                seen = tank.read_temperature()
            except Exception as exc:
                raise OpFailed(f"temperature sensor fault on {self.tank}: {exc}")
            if seen <= self.setpoint_c:
                break
            if ctx.op_elapsed_s() > self.timeout_s:
                raise OpFailed(f"{self.tank} did not cool to "
                               f"{self.setpoint_c:g}C in time")
            yield Tick()
        ctx.log(f"{self.tank} cooled to {tank.temperature():.1f}C")
        yield Tick()


# --------------------------------------------------------------------------
# valves / pumps / transfer
# --------------------------------------------------------------------------
class OpenValve(Op):
    def __init__(self, valve: str, timeout_s: float = 20.0):
        self.valve = valve
        self.timeout_s = timeout_s

    def describe(self):
        return f"open valve {self.valve}"

    def __call__(self, ctx):
        v = ctx.plant.valves[self.valve]
        v.command(True, ctx.label)
        ctx.event("valve", valve=self.valve, command="OPEN")
        yield Wait(lambda: v.open or ctx.op_elapsed_s() > self.timeout_s,
                   f"valve {self.valve} opening")
        if not v.open:
            raise OpFailed(
                f"valve {self.valve} did not open within {self.timeout_s:g}s"
                f"{' (stuck)' if v.stuck else ''}")
        ctx.log(f"valve {self.valve} open")
        yield Tick()


class CloseValve(Op):
    def __init__(self, valve: str, timeout_s: float = 20.0):
        self.valve = valve
        self.timeout_s = timeout_s

    def describe(self):
        return f"close valve {self.valve}"

    def __call__(self, ctx):
        v = ctx.plant.valves[self.valve]
        v.command(False, ctx.label)
        ctx.event("valve", valve=self.valve, command="CLOSE")
        yield Wait(lambda: not v.open or ctx.op_elapsed_s() > self.timeout_s,
                   f"valve {self.valve} closing")
        if v.open:
            raise OpFailed(
                f"valve {self.valve} did not close within {self.timeout_s:g}s"
                f"{' (stuck open)' if v.stuck else ''}")
        ctx.log(f"valve {self.valve} closed")
        yield Tick()


class StartPump(Op):
    def __init__(self, pump: str, timeout_s: float = 20.0):
        self.pump = pump
        self.timeout_s = timeout_s

    def describe(self):
        return f"start pump {self.pump}"

    def __call__(self, ctx):
        p = ctx.plant.pumps[self.pump]
        p.command(True, ctx.label)
        ctx.event("pump", pump=self.pump, command="ON")
        if p.failed:
            raise OpFailed(f"pump {self.pump} will not start (motor trip)")
        yield Wait(lambda: p.state.value == "RUNNING"
                   or ctx.op_elapsed_s() > self.timeout_s,
                   f"pump {self.pump} starting")
        if p.state.value != "RUNNING":
            raise OpFailed(f"pump {self.pump} did not reach running")
        ctx.log(f"pump {self.pump} running")
        yield Tick()


class StopPump(Op):
    def __init__(self, pump: str, timeout_s: float = 20.0):
        self.pump = pump
        self.timeout_s = timeout_s

    def describe(self):
        return f"stop pump {self.pump}"

    def __call__(self, ctx):
        p = ctx.plant.pumps[self.pump]
        p.command(False, ctx.label)
        ctx.event("pump", pump=self.pump, command="OFF")
        yield Wait(lambda: p.state.value == "STOPPED"
                   or ctx.op_elapsed_s() > self.timeout_s,
                   f"pump {self.pump} stopping")
        ctx.log(f"pump {self.pump} stopped")
        yield Tick()


class StartMachine(Op):
    def __init__(self, machine: str, timeout_s: float = 60.0):
        self.machine = machine
        self.timeout_s = timeout_s

    def describe(self):
        return f"start {self.machine}"

    def __call__(self, ctx):
        m = ctx.plant.machines[self.machine]
        m.command(True, ctx.label)
        ctx.event("machine", machine=self.machine, command="ON")
        if m.failed:
            raise OpFailed(f"{self.machine} will not start (trip)")
        yield Wait(lambda: m.state.value == "RUNNING"
                   or ctx.op_elapsed_s() > self.timeout_s,
                   f"{self.machine} spinning up")
        if m.state.value != "RUNNING":
            raise OpFailed(f"{self.machine} did not reach running speed")
        ctx.log(f"{self.machine} running")
        yield Tick()


class StopMachine(Op):
    def __init__(self, machine: str, timeout_s: float = 60.0):
        self.machine = machine
        self.timeout_s = timeout_s

    def describe(self):
        return f"stop {self.machine}"

    def __call__(self, ctx):
        m = ctx.plant.machines[self.machine]
        m.command(False, ctx.label)
        ctx.event("machine", machine=self.machine, command="OFF")
        yield Wait(lambda: m.state.value == "STOPPED"
                   or ctx.op_elapsed_s() > self.timeout_s,
                   f"{self.machine} spinning down")
        ctx.log(f"{self.machine} stopped")
        yield Tick()


class Mill(Op):
    """Run a mill for a fixed time at speed."""

    def __init__(self, machine: str, seconds: float = 0.0):
        self.machine = machine
        self.seconds = seconds

    def describe(self):
        return f"mill {self.machine} {self.seconds:g}s"

    def __call__(self, ctx):
        yield from ctx.run_op(StartMachine(self.machine))
        m = ctx.plant.machines[self.machine]
        start_run = m.run_ms
        if self.seconds > 0:
            yield Wait(lambda: (m.run_ms - start_run) / 1000.0 >= self.seconds,
                       f"milling {self.machine}")
        yield from ctx.run_op(StopMachine(self.machine))
        ctx.log(f"milled {self.machine}")
        yield Tick()


class Drain(Op):
    """Empty a tank to the waste vessel through an optional reserved line."""

    def __init__(self, tank: str, line: str = ""):
        self.tank = tank
        self.line = line

    def describe(self):
        return f"drain {self.tank}"

    def __call__(self, ctx):
        kg = ctx.plant.tanks[self.tank].total_kg()
        if kg <= 0:
            yield Tick()
            return
        if self.line and ctx.plant.held_by(self.line) != ctx.batch_id:
            raise OpFailed(f"cannot drain {self.tank}: line {self.line} not held")
        moved = ctx.plant.drain_to_waste(ctx.now_ms(), ctx.batch_id,
                                         self.tank, self.line)
        ctx.event("drain", tank=self.tank, kg=round(moved, 3))
        if self.line:
            # a drain is a transfer and must appear in the mass balance
            ctx.event("transfer_done", line=self.line, kg=round(moved, 3))
        ctx.log(f"drained {moved:.1f}kg from {self.tank}")
        yield Tick()


class LiveMass:
    """Resolve a transfer amount from the source tank at the moment the
    transfer starts (used for "empty this vessel" steps)."""

    def __init__(self, tank: str):
        self.tank = tank

    def resolve(self, ctx) -> float:
        return ctx.plant.tanks[self.tank].total_kg()


class Transfer(Op):
    """Move ``kg`` of the source tank's contents through a shared line.

    The batch must already hold the line (the step graph acquires it).  The
    op refuses to move material if any other batch holds the line - that is
    the hard interlock behind "two batches never share a line".
    """

    def __init__(self, line: str, source: str, target: str, kg: float = 0.0,
                 pump: Optional[str] = None, rate_kg_s: float = 40.0,
                 source_valve: Optional[str] = None,
                 target_valve: Optional[str] = None):
        self.line = line
        self.source = source
        self.target = target
        self.kg = kg
        self.pump = pump
        self.rate_kg_s = rate_kg_s
        self.source_valve = source_valve
        self.target_valve = target_valve

    def describe(self):
        return f"transfer {self.kg:g}kg {self.source}->{self.target} via {self.line}"

    def __call__(self, ctx):
        plant = ctx.plant
        if isinstance(self.kg, LiveMass):
            kg = self.kg.resolve(ctx)
        else:
            kg = ctx.qty(self.kg)
        key = ctx.op_key + ".moved"
        moved = float(ctx.state.get(key, 0.0))
        if moved >= kg - 1e-9:
            ctx.log(f"transfer already complete ({moved:.1f}kg), skipping")
            yield Tick()
            return
        holder = plant.held_by(self.line)
        if holder != ctx.batch_id:
            raise OpFailed(
                f"batch {ctx.batch_id} does not hold line {self.line} "
                f"(holder={holder})")
        src = plant.tanks[self.source]
        dst = plant.tanks[self.target]
        need = kg - moved
        if src.total_kg() + 1e-6 < need:
            raise OpFailed(f"{self.source} has {src.total_kg():.1f}kg, "
                           f"needs {need:.1f}kg")
        # one physical transfer at a time on a line, even between two
        # branches of the same batch
        token = f"{ctx.batch_id}:{ctx.phase_path}:{ctx.op_key}"
        try:
            plant.begin_transfer(self.line, ctx.batch_id, token)
        except Exception as exc:
            raise OpFailed(str(exc))
        try:
            if self.source_valve:
                yield from ctx.run_op(OpenValve(self.source_valve))
            if self.target_valve:
                yield from ctx.run_op(OpenValve(self.target_valve))
            if self.pump:
                yield from ctx.run_op(StartPump(self.pump))
            pump = plant.pumps.get(self.pump) if self.pump else None
            last = ctx.now_ms()
            ctx.event("transfer_start", line=self.line, source=self.source,
                      target=self.target, kg=round(need, 3))
            while moved < kg - 1e-9:
                holder = plant.held_by(self.line)
                if holder != ctx.batch_id:
                    raise OpFailed(
                        f"line {self.line} lost to {holder} during transfer")
                if dst.total_kg() >= dst.capacity_kg - 1e-6:
                    raise OpFailed(f"{self.target} full during transfer")
                now = ctx.now_ms()
                dt = (now - last) / 1000.0
                last = now
                rate = self.rate_kg_s
                if pump is not None:
                    if pump.failed:
                        raise OpFailed(
                            f"pump {self.pump} tripped during transfer")
                    rate = min(rate, pump.current_flow())
                step = min(rate * dt, kg - moved, src.total_kg())
                if step > 0:
                    chunk = src.remove(step)
                    dst.add_material(chunk)
                    plant.transfer(now, ctx.batch_id, self.line, chunk,
                                   self.source, self.target)
                    moved += step
                    ctx.state[key] = moved
                yield Tick()
            if self.pump:
                yield from ctx.run_op(StopPump(self.pump))
            if self.source_valve:
                yield from ctx.run_op(CloseValve(self.source_valve))
            if self.target_valve:
                yield from ctx.run_op(CloseValve(self.target_valve))
        finally:
            plant.end_transfer(self.line, token)
        ctx.event("transfer_done", line=self.line, kg=round(moved, 3))
        ctx.log(f"transferred {moved:.1f}kg via {self.line}")
        yield Tick()


# --------------------------------------------------------------------------
# generic
# --------------------------------------------------------------------------
class WaitSeconds(Op):
    def __init__(self, seconds: float, label: str = ""):
        self.seconds = seconds
        self._label = label

    def describe(self):
        return self._label or f"wait {self.seconds:g}s"

    def __call__(self, ctx):
        yield Wait(lambda: ctx.op_elapsed_s() >= self.seconds, self.describe())
        yield Tick()


class HoldUntil(Op):
    def __init__(self, label: str, predicate: Callable[[], bool],
                 timeout_s: float = 3600.0):
        self._label = label
        self.predicate = predicate
        self.timeout_s = timeout_s

    def describe(self):
        return self._label

    def __call__(self, ctx):
        while not self.predicate():
            if ctx.op_elapsed_s() > self.timeout_s:
                raise OpFailed(f"{self._label}: timeout after {self.timeout_s:g}s")
            yield Tick()
        yield Tick()


class CheckTemperature(Op):
    def __init__(self, tank: str, low: float, high: float):
        self.tank = tank
        self.low = low
        self.high = high

    def describe(self):
        return f"check {self.tank} temp in [{self.low:g},{self.high:g}]"

    def __call__(self, ctx):
        tank = ctx.plant.tanks[self.tank]
        try:
            seen = tank.read_temperature()
        except Exception as exc:
            raise OpFailed(f"temperature sensor fault on {self.tank}: {exc}")
        if not (self.low <= seen <= self.high):
            raise OpFailed(
                f"{self.tank} temperature {seen:.1f}C outside "
                f"[{self.low:g},{self.high:g}]C")
        ctx.log(f"temperature check {self.tank}: {seen:.1f}C ok")
        yield Tick()


class SetParam(Op):
    def __init__(self, key: str, value):
        self.key = key
        self.value = value

    def describe(self):
        return f"set {self.key}={self.value}"

    def __call__(self, ctx):
        ctx.params[self.key] = self.value
        ctx.event("parameter", key=self.key, value=str(self.value))
        yield Tick()


class RecordQuality(Op):
    def __init__(self, label: str, passed: bool = True, value: str = ""):
        self._label = label
        self.passed = passed
        self.value = value or ("PASS" if passed else "FAIL")

    def describe(self):
        return f"QC {self._label}"

    def __call__(self, ctx):
        ctx.params[f"qc.{self._label}"] = self.passed
        ctx.event("quality", test=self._label, result=self.value)
        ctx.log(f"QC {self._label}: {self.value}")
        yield Tick()


class RaiseFault(Op):
    def __init__(self, reason: str):
        self.reason = reason

    def describe(self):
        return f"raise fault: {self.reason}"

    def __call__(self, ctx):
        raise OpFailed(self.reason)
        yield Tick()  # pragma: no cover


class CheckLevel(Op):
    def __init__(self, tank: str, low_kg: float, high_kg: float):
        self.tank = tank
        self.low_kg = low_kg
        self.high_kg = high_kg

    def describe(self):
        return f"check {self.tank} level"

    def __call__(self, ctx):
        kg = ctx.plant.tanks[self.tank].total_kg()
        if not (self.low_kg <= kg <= self.high_kg):
            raise OpFailed(
                f"{self.tank} level {kg:.1f}kg outside "
                f"[{self.low_kg:g},{self.high_kg:g}]kg")
        yield Tick()
