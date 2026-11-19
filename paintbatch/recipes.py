"""Recipe library: twelve paint recipes in the four-level structure.

Every recipe follows the same physical flow - raw-material header -> reactor
-> premix -> mill -> finish -> filler - while the *control structure*
deliberately differs so the acceptance run exercises every graph feature:

===========  ==============================================================
P-101        sequential; single reactor, single finish tank
P-102        parallel cook + in-line QC; two tint methods (choice)
P-103        reactor choice by availability, tint choice by parameter
P-104        two parallel fill branches, separate finish tanks
P-105        long cook with temperature supervision and written hold
P-106        mill choice by parameter; fault-drill recipe
P-107        short cycle; self-scope fault propagation
P-108        batch-scope fault propagation drill
P-109        operation-scope propagation drill
P-110        abort drill with drain to waste
P-111        unit-procedure-scope propagation drill
P-112        rework batch blending two recovered lots (parallel charges)
===========  ==============================================================

Propagation scope is written per phase: ``propagation="self" | "operation" |
"unit_procedure" | "batch"``.  Hold / restart / stop / abort sequences are
written per phase as well - they are real equipment sequences, not flags.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

from . import ops
from .recipe import (
    Acquire,
    Always,
    Choice,
    Operation,
    Parallel,
    Phase,
    Procedure,
    Recipe,
    Release,
    Transition,
    UnitProcedure,
    When,
)

REACTOR_HEATER_W = 600000.0
TICK = 0.1

#: valve between a raw-material tank and the LETDOWN header
RM_VALVE = {"RM-1": "V-RM-1", "RM-2": "V-RM-2",
            "RM-3": "V-RM-3", "RM-4": "V-RM-4"}
#: inlet valve from the LETDOWN header into a vessel
VESSEL_IN_VALVE = {
    "REACTOR-1": "V-LD-R1", "REACTOR-2": "V-LD-R2",
    "PREMIX-1": "V-PM1-IN", "PREMIX-2": "V-PM2-IN",
    "FINISH-1": "V-F1-IN", "FINISH-2": "V-F2-IN",
}
#: inlet valve used when a feed arrives through the additive header
ADD_IN_VALVE = {"REACTOR-1": "V-ADD-R1", "REACTOR-2": "V-ADD-R2"}
VESSEL_OUT_VALVE = {
    "REACTOR-1": "V-R1-OUT", "REACTOR-2": "V-R2-OUT",
    "PREMIX-1": "V-PM1-OUT", "PREMIX-2": "V-PM2-OUT",
    "FINISH-1": "V-F1-OUT", "FINISH-2": "V-F2-OUT",
}


# --------------------------------------------------------------------------
# phase factories
# --------------------------------------------------------------------------
def feed_phase(name: str, vessel: str, src: str, kg: float, temp_c: float,
               propagation: str = "operation", line: str = "LETDOWN",
               pump: str = "P-LD", priority: int = 2) -> Phase:
    """Charge one raw material into ``vessel`` through a raw header.

    The header must already be held; the enclosing operation acquires it.
    ``line="ADD-LINE"`` routes through the additive header, which is what
    makes two feeds into the same vessel run in parallel legitimately.
    """
    src_valve = RM_VALVE[src]
    if line == "ADD-LINE":
        dst_valve = ADD_IN_VALVE[vessel]
    else:
        dst_valve = VESSEL_IN_VALVE[vessel]
    main = [
        ops.OpenValve(src_valve),
        ops.OpenValve(dst_valve),
        ops.StartPump(pump),
        ops.Transfer(line, src, vessel, kg, pump=pump,
                     rate_kg_s=45.0),
        ops.StopPump(pump),
        ops.CloseValve(src_valve),
        ops.CloseValve(dst_valve),
        ops.WaitSeconds(1.0, "level settle"),
    ]
    return Phase(
        name, line, main,
        on_hold=_hold_seq([src_valve, dst_valve], pump, ""),
        on_restart=_restart_seq([src_valve, dst_valve], pump, ""),
        on_stop=[ops.StopPump(pump), ops.CloseValve(src_valve),
                 ops.CloseValve(dst_valve)],
        on_abort=[ops.StopPump(pump), ops.CloseValve(src_valve),
                  ops.CloseValve(dst_valve)],
        propagation=propagation,
        description=f"feed {kg:g}kg {src} -> {vessel}",
    )


def _hold_seq(valves: Sequence[str], pump: str, tank: str,
              heater: bool = False) -> List:
    seq: List = []
    if pump:
        seq.append(ops.StopPump(pump))
    for v in valves:
        seq.append(ops.CloseValve(v))
    if tank:
        seq.append(ops.StopAgitator(tank))
        if heater:
            seq.append(ops.Heater(tank, 0.0))
    seq.append(ops.WaitSeconds(1.0, "hold settled"))
    return seq


def _restart_seq(valves: Sequence[str], pump: str, tank: str,
                 heater_w: float = 0.0) -> List:
    seq: List = []
    if tank:
        seq.append(ops.Agitate(tank, 0.0))
        if heater_w:
            seq.append(ops.Heater(tank, heater_w))
    for v in valves:
        seq.append(ops.OpenValve(v))
    if pump:
        seq.append(ops.StartPump(pump))
    seq.append(ops.WaitSeconds(1.0, "restart settled"))
    return seq


def cook_phase(name: str, reactor: str, setpoint_c: float, mix_s: float,
               propagation: str = "operation", supervise: bool = True
               ) -> Phase:
    main = [
        ops.Agitate(reactor, 0.0),
        ops.HeatTo(reactor, setpoint_c, heater_w=REACTOR_HEATER_W),
        ops.Agitate(reactor, mix_s, f"cook {reactor}"),
    ]
    if supervise:
        # only check the vessel still has material and did not run away hot;
        # HeatTo below already supervises the ramp itself
        main += [
            ops.CheckLevel(reactor, 10.0, 6000.0),
            ops.CheckTemperature(reactor, 15.0, setpoint_c + 25.0),
        ]
    return Phase(
        name, reactor, main,
        on_hold=_hold_seq([], "", reactor, heater=True),
        # the restart sequence does not re-arm the heater: HeatTo re-asserts
        # it on its next step.  Re-arming it here would leave the heater on
        # through the mixing step that follows the ramp.
        on_restart=_restart_seq([], "", reactor),
        on_stop=[ops.Heater(reactor, 0.0), ops.StopAgitator(reactor)],
        on_abort=[ops.Heater(reactor, 0.0), ops.StopAgitator(reactor)],
        propagation=propagation,
        description=f"cook {reactor} at {setpoint_c:g}C for {mix_s:g}s",
    )


def transfer_phase(name: str, line: str, src: str, dst: str, kg: float,
                   pump: str, propagation: str = "operation",
                   rate: float = 35.0) -> Phase:
    src_valve = VESSEL_OUT_VALVE[src]
    dst_valve = VESSEL_IN_VALVE[dst]
    main = [
        ops.OpenValve(src_valve),
        ops.OpenValve(dst_valve),
        ops.StartPump(pump),
        ops.Transfer(line, src, dst, kg, pump=pump, rate_kg_s=rate),
        ops.StopPump(pump),
        ops.CloseValve(src_valve),
        ops.CloseValve(dst_valve),
        ops.WaitSeconds(0.5, "settle"),
    ]
    return Phase(
        name, line, main,
        on_hold=_hold_seq([src_valve, dst_valve], pump, ""),
        on_restart=_restart_seq([src_valve, dst_valve], pump, ""),
        on_stop=[ops.StopPump(pump), ops.CloseValve(src_valve),
                 ops.CloseValve(dst_valve)],
        on_abort=[ops.StopPump(pump), ops.CloseValve(src_valve),
                  ops.CloseValve(dst_valve)],
        propagation=propagation,
        description=f"transfer {kg:g}kg {src}->{dst} via {line}",
    )


def mill_phase(name: str, machine: str, seconds: float,
               propagation: str = "operation") -> Phase:
    return Phase(
        name, machine, [ops.Mill(machine, seconds)],
        on_hold=[ops.StopMachine(machine), ops.WaitSeconds(1.0, "mill held")],
        on_restart=[ops.StartMachine(machine), ops.WaitSeconds(1.0, "mill up")],
        on_stop=[ops.StopMachine(machine)],
        on_abort=[ops.StopMachine(machine)],
        propagation=propagation,
        description=f"mill {machine} for {seconds:g}s",
    )


def qc_phase(name: str, tank: str, label: str,
             propagation: str = "unit_procedure") -> Phase:
    return Phase(
        name, tank,
        [ops.CheckTemperature(tank, 15.0, 100.0),
         ops.CheckLevel(tank, 10.0, 8000.0),
         ops.RecordQuality(label, passed=True)],
        on_hold=[ops.WaitSeconds(1.0, "qc hold")],
        on_restart=[ops.WaitSeconds(1.0, "qc restart")],
        on_abort=[ops.WaitSeconds(1.0, "qc abort")],
        propagation=propagation,
        description=f"quality release check ({label})",
    )


def tint_phase(tank: str, kg: float, propagation: str = "operation") -> Phase:
    return Phase(
        "TINT", tank,
        [ops.Charge(tank, "tint", kg, 24.0),
         ops.Agitate(tank, 8.0, "tint dispersion")],
        on_hold=_hold_seq([], "", tank),
        on_restart=_restart_seq([], "", tank),
        on_abort=_hold_seq([], "", tank),
        propagation=propagation,
        description="add tint paste and disperse",
    )


def fill_phase(name: str, tank: str, kg,
               propagation: str = "operation") -> Phase:
    out_valve = VESSEL_OUT_VALVE[tank]
    amount = (ops.LiveMass(tank) if kg == "all"
              else kg)
    main = [
        ops.OpenValve(out_valve),
        ops.OpenValve("V-FILL"),
        ops.StartPump("P-FILL"),
        ops.Transfer("FILL-LINE", tank, "PRODUCT", amount, pump="P-FILL",
                     rate_kg_s=30.0),
        ops.StopPump("P-FILL"),
        ops.CloseValve("V-FILL"),
        ops.CloseValve(out_valve),
        ops.RecordQuality(f"fill-{tank}", passed=True),
    ]
    return Phase(
        name, "FILL-LINE", main,
        on_hold=_hold_seq([out_valve, "V-FILL"], "P-FILL", ""),
        on_restart=_restart_seq([out_valve, "V-FILL"], "P-FILL", ""),
        on_stop=[ops.StopPump("P-FILL"), ops.CloseValve("V-FILL")],
        on_abort=[ops.StopPump("P-FILL"), ops.CloseValve("V-FILL"),
                  ops.CloseValve(out_valve)],
        propagation=propagation,
        description=f"fill from {tank}",
    )


def charge_operation(name: str, vessel: str, feeds: List[Tuple[str, float, float]],
                     propagation: str = "operation",
                     priority: int = 2) -> Operation:
    """Operation: acquire the header, run one or more feed phases."""
    main_feeds = [f for f in feeds if not f[0].startswith("RM-3")
                  and not f[0].startswith("RM-4")] or []
    add_feeds = [f for f in feeds if f not in main_feeds]
    steps: List = []
    if main_feeds:
        steps.append(Acquire(["LETDOWN"], priority=priority))
        for idx, (src, kg, temp) in enumerate(main_feeds):
            steps.append(feed_phase(f"FEED-{src}-{idx + 1}", vessel, src, kg,
                                    temp, propagation=propagation))
        steps.append(Release(["LETDOWN"]))
    if add_feeds:
        steps.append(Acquire(["ADD-LINE"], priority=priority + 1))
        for idx, (src, kg, temp) in enumerate(add_feeds):
            steps.append(feed_phase(f"FEED-{src}-{idx + 1}", vessel, src, kg,
                                    temp, propagation=propagation,
                                    line="ADD-LINE", pump="P-ADD"))
        steps.append(Release(["ADD-LINE"]))
    return Operation(name, steps, description=f"charge {vessel}")


def charge_into_phase(name: str, src: str, dst: str, line: str, pump: str,
                      src_valve: str, dst_valve: str,
                      propagation: str = "operation") -> Phase:
    """Move the *entire* contents of ``src`` into ``dst`` (inline equipment).

    Used for passes through a mill or a transfer header where the amount is
    whatever is in the vessel.  Reads the live level, so the caller does not
    have to know the exact mass.
    """
    ph = Phase(
        name, line,
        [ops.OpenValve(src_valve), ops.OpenValve(dst_valve),
         ops.StartPump(pump),
         _LiveMassTransfer(line, src, dst, pump),
         ops.StopPump(pump),
         ops.CloseValve(src_valve), ops.CloseValve(dst_valve)],
        on_hold=_hold_seq([src_valve, dst_valve], pump, ""),
        on_restart=_restart_seq([src_valve, dst_valve], pump, ""),
        on_stop=[ops.StopPump(pump), ops.CloseValve(src_valve),
                 ops.CloseValve(dst_valve)],
        on_abort=[ops.StopPump(pump), ops.CloseValve(src_valve),
                  ops.CloseValve(dst_valve)],
        propagation=propagation,
        description=f"move {src} -> {dst} via {line}",
    )
    return ph


def split_phase(name: str, src: str, dst: str, kg: float) -> Phase:
    """Move ``kg`` (not everything) from a reactor to a premix."""
    ph = Phase(
        name, "REACT-OUT",
        [ops.OpenValve("V-R1-OUT" if src == "REACTOR-1" else "V-R2-OUT"),
         ops.OpenValve(VESSEL_IN_VALVE[dst]),
         ops.StartPump("P-RO"),
         ops.Transfer("REACT-OUT", src, dst, kg, pump="P-RO", rate_kg_s=30.0),
         ops.StopPump("P-RO"),
         ops.CloseValve(VESSEL_IN_VALVE[dst]),
         ops.CloseValve("V-R1-OUT" if src == "REACTOR-1" else "V-R2-OUT")],
        on_hold=_hold_seq([], "P-RO", ""),
        on_restart=_restart_seq([], "P-RO", ""),
        on_stop=[ops.StopPump("P-RO")],
        on_abort=[ops.StopPump("P-RO")],
        propagation="operation",
        description=f"split {kg:g}kg {src} -> {dst}",
    )
    return ph


def letdown_section(reactor: str, premix: str, priority: int = 3
                    ) -> UnitProcedure:
    return UnitProcedure("LETDOWN", [
        Acquire(["REACT-OUT"], priority=priority),
        Operation("TRANSFER", [
            charge_into_phase("XFER", reactor, premix, "REACT-OUT", "P-RO",
                              VESSEL_OUT_VALVE[reactor],
                              VESSEL_IN_VALVE[premix]),
        ]),
        Release(["REACT-OUT"]),
        # the reactor is empty now: hand it back before the next unit procedure
        Release([reactor]),
    ], description=f"let down {reactor} -> {premix}")


def reaction_operation(name: str, reactor: str, setpoint: float, mix_s: float,
                       propagation: str = "operation",
                       qc: bool = True) -> Operation:
    steps: List = [cook_phase("COOK", reactor, setpoint, mix_s,
                              propagation=propagation)]
    if qc:
        steps.append(qc_phase("QC", reactor, f"cook-{reactor}",
                              propagation=propagation))
    return Operation(name, steps, description=f"react in {reactor}")


# --------------------------------------------------------------------------
# building blocks per unit procedure
# --------------------------------------------------------------------------
def reactor_section(reactor: str, feeds, setpoint: float, mix_s: float,
                    priority: int = 3, prop: str = "operation",
                    parallel_qc: bool = False) -> UnitProcedure:
    reactor_step = Acquire([reactor], priority=priority)
    if parallel_qc:
        react = Parallel([
            [reaction_operation("COOK-OP", reactor, setpoint, mix_s,
                                propagation="unit_procedure", qc=False)],
            [Operation("QC-OP", [qc_phase("QC-INLINE", reactor,
                                          "in-line-viscosity",
                                          propagation="unit_procedure")])],
        ], name="cook-and-qc")
    else:
        react = reaction_operation("REACT-OP", reactor, setpoint, mix_s,
                                   propagation=prop)
    return UnitProcedure("REACTION", [
        reactor_step,
        charge_operation("CHARGE", reactor, feeds),
        Transition(Always()),
        react,
    ], description=f"reaction section on {reactor}")


def mill_section(premix: str, finish: str, machine: str, seconds: float,
                 priority: int = 5) -> UnitProcedure:
    """Run the premix through the mill line into the finish tank, then grind.

    Note the mill line is *not* the premix outlet: material reaches it via
    the premix outlet valve and the mill inlet valve, which is why the
    mill section needs its own transfer phase.
    """
    feed = Phase(
        "FEED-MILL", "MILL-LINE",
        [ops.OpenValve(VESSEL_OUT_VALVE[premix]),
         ops.OpenValve("V-MILL-IN"),
         ops.StartPump("P-MILL"),
         _LiveMassTransfer("MILL-LINE", premix, finish, "P-MILL"),
         ops.StopPump("P-MILL"),
         ops.CloseValve("V-MILL-IN"),
         ops.CloseValve(VESSEL_OUT_VALVE[premix]),
         ops.WaitSeconds(0.5, "mill feed settle")],
        on_hold=_hold_seq([VESSEL_OUT_VALVE[premix], "V-MILL-IN"], "P-MILL", ""),
        on_restart=_restart_seq([VESSEL_OUT_VALVE[premix], "V-MILL-IN"],
                                "P-MILL", ""),
        on_stop=[ops.StopPump("P-MILL"), ops.CloseValve("V-MILL-IN")],
        on_abort=[ops.StopPump("P-MILL"), ops.CloseValve("V-MILL-IN")],
        propagation="operation",
        description=f"feed {premix} through mill line into {finish}",
    )
    return UnitProcedure("MILL", [
        Acquire(["MILL-LINE", machine], priority=priority),
        Operation("MILL", [feed, mill_phase("GRIND", machine, seconds)]),
        Release(["MILL-LINE", machine]),
    ], description=f"mill via {machine} into {finish}")


class _LiveMassTransfer(ops.Op):
    """Transfer whatever is in the source tank right now.

    The mill feed's precision is the level control's job, not the recipe's,
    so the recipe does not know the exact mass at this point.
    """

    label = "live mass transfer"

    def __init__(self, line, src, dst, pump):
        self.line = line
        self.src = src
        self.dst = dst
        self.pump = pump

    def describe(self):
        return f"move all of {self.src} -> {self.dst} via {self.line}"

    def __call__(self, ctx):
        kg = ctx.plant.tanks[self.src].total_kg()
        yield from ops.Transfer(self.line, self.src, self.dst, kg,
                                pump=self.pump, rate_kg_s=30.0)(ctx)


def _mill_steps(name: str, machine: str, premix: str, finish: str,
                seconds: float) -> list:
    """Acquire/mill/release as a plain step list (used inside a branch)."""
    feed = Phase(
        f"FEED-{name}", "MILL-LINE",
        [ops.OpenValve(VESSEL_OUT_VALVE[premix]),
         ops.OpenValve("V-MILL-IN"),
         ops.StartPump("P-MILL"),
         _LiveMassTransfer("MILL-LINE", premix, finish, "P-MILL"),
         ops.StopPump("P-MILL"),
         ops.CloseValve("V-MILL-IN"),
         ops.CloseValve(VESSEL_OUT_VALVE[premix])],
        on_hold=_hold_seq([VESSEL_OUT_VALVE[premix], "V-MILL-IN"], "P-MILL", ""),
        on_restart=_restart_seq([VESSEL_OUT_VALVE[premix], "V-MILL-IN"],
                                "P-MILL", ""),
        propagation="operation",
        description=f"feed {premix} through the mill line into {finish}",
    )
    return [Acquire(["MILL-LINE", machine], priority=5), feed,
            mill_phase(f"GRIND-{name}", machine, seconds),
            Release(["MILL-LINE", machine])]


def fill_section(tank: str, kg, priority: int = 8) -> UnitProcedure:
    """``kg="all"`` fills whatever the batch actually produced.

    A batch's yield differs from its nominal size by the transfer and
    hold-up losses; the fill phase must not demand more than is in the tank.
    """
    return UnitProcedure("FILL", [
        Acquire(["FILL-LINE"], priority=priority),
        Operation("FILL", [fill_phase("FILL", tank, kg,
                                      propagation="operation")]),
        Release(["FILL-LINE"]),
    ], description=f"fill from {tank}")


# --------------------------------------------------------------------------
# recipes
# --------------------------------------------------------------------------
def recipe_white_base() -> Recipe:
    proc = Procedure("P-101", [
        reactor_section("REACTOR-1",
                        [("RM-1", 900.0, 22.0), ("RM-2", 500.0, 22.0)],
                        58.0, 45.0),
        letdown_section("REACTOR-1", "PREMIX-1"),
        UnitProcedure("TINT", [
            Operation("TINT", [tint_phase("PREMIX-1", 40.0)]),
        ], description="tint"),
        mill_section("PREMIX-1", "FINISH-1", "MILL-1", 25.0),
        fill_section("FINISH-1", "all"),
    ])
    return Recipe("P-101", "White Base", proc,
                  parameters={"tint": True}, batch_size_kg=1500.0,
                  product="WHITE-BASE", version="1.4")


def recipe_tint_blue() -> Recipe:
    proc = Procedure("P-102", [
        reactor_section("REACTOR-2",
                        [("RM-1", 700.0, 22.0), ("RM-3", 180.0, 22.0)],
                        64.0, 60.0, parallel_qc=True),
        letdown_section("REACTOR-2", "PREMIX-2"),
        UnitProcedure("TINT", [
            Operation("TINT", [
                Choice([
                    (When(lambda ctx: ctx.params.get("tint") is not False,
                          "tint recipe"), [tint_phase("PREMIX-2", 120.0)]),
                    (Always("no tint"), [
                        Phase("NO-TINT", "PREMIX-2",
                              [ops.WaitSeconds(1.0, "clear")],
                              on_hold=[ops.WaitSeconds(0.5)],
                              on_restart=[ops.WaitSeconds(0.5)],
                              propagation="self",
                              description="no tint"),
                    ]),
                ], name="TINT-CHOICE"),
            ]),
        ], description="tint choice"),
        mill_section("PREMIX-2", "FINISH-2", "MILL-2", 30.0),
        fill_section("FINISH-2", "all"),
    ])
    return Recipe("P-102", "Tint Blue", proc,
                  parameters={"tint": True}, batch_size_kg=900.0,
                  product="TINT-BLUE", version="2.0")


def recipe_fast_gray() -> Recipe:
    """Choose whichever reactor is free when the step is reached."""
    reactor = "REACTOR-1"
    proc = Procedure("P-103", [
        UnitProcedure("REACTION", [
            Choice([
                (When(lambda ctx: ctx.plant.held_by("REACTOR-1") is None,
                      "reactor 1 free"), [
                    Acquire(["REACTOR-1"], priority=4),
                    charge_operation("CHARGE-R1", "REACTOR-1",
                                     [("RM-1", 650.0, 22.0),
                                      ("RM-2", 350.0, 22.0)]),
                    reaction_operation("REACT-OP-R1", "REACTOR-1", 60.0, 40.0,
                                       propagation="operation"),
                    Release(["REACTOR-1"]),
                ]),
                (Always("fall back to reactor 2"), [
                    Acquire(["REACTOR-2"], priority=4),
                    charge_operation("CHARGE-R2", "REACTOR-2",
                                     [("RM-1", 650.0, 22.0),
                                      ("RM-2", 350.0, 22.0)]),
                    reaction_operation("REACT-OP-R2", "REACTOR-2", 60.0, 40.0,
                                       propagation="operation"),
                    Release(["REACTOR-2"]),
                ]),
            ], name="REACTOR-CHOICE"),
        ], description="reaction on the free reactor"),
        UnitProcedure("TRANSFER", [
            Acquire(["REACT-OUT"], priority=4),
            Operation("TRANSFER-OP", [
                Choice([
                    (When(lambda ctx: ctx.plant.tanks["REACTOR-1"].total_kg()
                          > 10.0, "from reactor 1"), [
                        charge_into_phase("XFER-R1", "REACTOR-1", "PREMIX-1",
                                          "REACT-OUT", "P-RO", "V-R1-OUT",
                                          "V-PM1-IN")]),
                    (Always("from reactor 2"), [
                        charge_into_phase("XFER-R2", "REACTOR-2", "PREMIX-1",
                                          "REACT-OUT", "P-RO", "V-R2-OUT",
                                          "V-PM1-IN")]),
                ], name="SOURCE-CHOICE"),
            ]),
            Release(["REACT-OUT"]),
        ]),
        mill_section("PREMIX-1", "FINISH-1", "MILL-1", 20.0),
        fill_section("FINISH-1", "all"),
    ])
    return Recipe("P-103", "Fast Gray", proc,
                  parameters={"tint": False}, batch_size_kg=850.0,
                  product="FAST-GRAY", version="1.1")


def recipe_primer() -> Recipe:
    """Two finish tanks filled in parallel; both contend for FILL-LINE."""
    proc = Procedure("P-104", [
        reactor_section("REACTOR-1",
                        [("RM-1", 800.0, 22.0), ("RM-2", 700.0, 22.0)],
                        52.0, 40.0),
        UnitProcedure("SPLIT", [
            Acquire(["REACT-OUT"], priority=3),
            Operation("TO-PREMIX-1", [
                split_phase("XFER-1", "REACTOR-1", "PREMIX-1", 750.0)]),
            Operation("TO-PREMIX-2", [
                charge_into_phase("XFER-2", "REACTOR-1", "PREMIX-2",
                                  "REACT-OUT", "P-RO", "V-R1-OUT",
                                  "V-PM2-IN")]),
            Release(["REACT-OUT"]),
            Release(["REACTOR-1"]),
        ]),
        UnitProcedure("MILL-AND-FILL", [
            *_mill_steps("MILL-1", "MILL-1", "PREMIX-1", "FINISH-1", 15.0),
            *_mill_steps("MILL-2", "MILL-2", "PREMIX-2", "FINISH-2", 15.0),
            Parallel([
                [Acquire(["FILL-LINE"], priority=8),
                 Operation("FILL-1", [fill_phase("FILL-1", "FINISH-1", "all")]),
                 Release(["FILL-LINE"])],
                [Acquire(["FILL-LINE"], priority=9),
                 Operation("FILL-2", [fill_phase("FILL-2", "FINISH-2", "all")]),
                 Release(["FILL-LINE"])],
            ], name="fills"),
        ]),
    ])
    return Recipe("P-104", "Primer", proc,
                  parameters={}, batch_size_kg=1500.0, product="PRIMER",
                  version="3.2")


def recipe_gloss_red() -> Recipe:
    proc = Procedure("P-105", [
        reactor_section("REACTOR-2",
                        [("RM-1", 850.0, 22.0), ("RM-3", 120.0, 22.0)],
                        70.0, 120.0),
        letdown_section("REACTOR-2", "PREMIX-2"),
        UnitProcedure("TINT-MILL", [
            Operation("TINT", [tint_phase("PREMIX-2", 150.0)]),
        ]),
        mill_section("PREMIX-2", "FINISH-2", "MILL-2", 60.0, priority=5),
        fill_section("FINISH-2", "all", priority=7),
    ])
    return Recipe("P-105", "Gloss Red", proc,
                  parameters={"tint": True}, batch_size_kg=1000.0,
                  product="GLOSS-RED", version="2.3")


def recipe_matte_olive() -> Recipe:
    mill = "MILL-1"
    proc = Procedure("P-106", [
        reactor_section("REACTOR-1",
                        [("RM-1", 600.0, 22.0), ("RM-2", 250.0, 22.0),
                         ("RM-4", 120.0, 22.0)],
                        48.0, 35.0),
        letdown_section("REACTOR-1", "PREMIX-1"),
        UnitProcedure("GRIND", [
            Choice([
                (When(lambda ctx: ctx.params.get("mill") == 2, "mill 2"), [
                    *_mill_steps("MILL-2", "MILL-2", "PREMIX-1", "FINISH-1",
                                 20.0),
                ]),
                (Always("mill 1"), [
                    *_mill_steps("MILL-1", "MILL-1", "PREMIX-1", "FINISH-1",
                                 20.0),
                ]),
            ], name="MILL-CHOICE"),
        ]),
        fill_section("FINISH-1", "all"),
    ])
    return Recipe("P-106", "Matte Olive", proc,
                  parameters={"mill": 1}, batch_size_kg=900.0,
                  product="MATTE-OLIVE", version="1.0")


def recipe_clearcoat() -> Recipe:
    proc = Procedure("P-107", [
        reactor_section("REACTOR-2",
                        [("RM-1", 500.0, 22.0), ("RM-4", 80.0, 22.0)],
                        45.0, 20.0),
        letdown_section("REACTOR-2", "PREMIX-2"),
        UnitProcedure("POLISH", [
            *_mill_steps("MILL-2", "MILL-2", "PREMIX-2", "FINISH-2", 10.0),
        ]),
        fill_section("FINISH-2", "all", priority=7),
    ])
    return Recipe("P-107", "Clearcoat", proc,
                  parameters={}, batch_size_kg=600.0, product="CLEARCOAT",
                  version="1.2")


def recipe_filler() -> Recipe:
    proc = Procedure("P-108", [
        reactor_section("REACTOR-1",
                        [("RM-2", 900.0, 22.0), ("RM-1", 400.0, 22.0)],
                        40.0, 30.0, prop="batch"),
        letdown_section("REACTOR-1", "PREMIX-1"),
        mill_section("PREMIX-1", "FINISH-1", "MILL-1", 15.0),
        fill_section("FINISH-1", "all", priority=7),
    ])
    return Recipe("P-108", "Filler", proc,
                  parameters={}, batch_size_kg=1300.0, product="FILLER",
                  version="1.0")


def recipe_deep_base() -> Recipe:
    proc = Procedure("P-109", [
        reactor_section("REACTOR-2",
                        [("RM-1", 750.0, 22.0), ("RM-3", 220.0, 22.0)],
                        62.0, 50.0, prop="operation"),
        letdown_section("REACTOR-2", "PREMIX-2"),
        UnitProcedure("TINT", [Operation("TINT", [tint_phase("PREMIX-2", 90.0)])]),
        mill_section("PREMIX-2", "FINISH-2", "MILL-2", 25.0),
        fill_section("FINISH-2", "all"),
    ])
    return Recipe("P-109", "Deep Base", proc,
                  parameters={}, batch_size_kg=1050.0, product="DEEP-BASE",
                  version="2.1")


def recipe_safety_abort() -> Recipe:
    drain = Phase("DRAIN", "REACTOR-1",
                  [ops.Drain("REACTOR-1", "WASTE-LINE")],
                  on_hold=[ops.WaitSeconds(0.5)],
                  on_restart=[ops.WaitSeconds(0.5)],
                  on_abort=_hold_seq(["V-WASTE"], "", "REACTOR-1"),
                  propagation="self",
                  description="drain reactor to waste")
    proc = Procedure("P-110", [
        UnitProcedure("REACTION", [
            Acquire(["REACTOR-1"], priority=3),
            charge_operation("CHARGE", "REACTOR-1",
                             [("RM-1", 700.0, 22.0), ("RM-2", 300.0, 22.0)]),
            reaction_operation("REACT-OP", "REACTOR-1", 55.0, 40.0,
                               propagation="unit_procedure", qc=False),
            Acquire(["WASTE-LINE"], priority=9),
            Operation("SAFE", [drain]),
            Release(["WASTE-LINE"]),
            Release(["REACTOR-1"]),
        ], description="reactor with safe drain"),
        UnitProcedure("RECOVER", [
            Acquire(["REACTOR-1"], priority=3),
            charge_operation("RECHARGE", "REACTOR-1",
                             [("RM-1", 600.0, 22.0)]),
            reaction_operation("RECOOK", "REACTOR-1", 40.0, 15.0),
        ]),
        letdown_section("REACTOR-1", "PREMIX-1"),
        mill_section("PREMIX-1", "FINISH-1", "MILL-1", 10.0),
        fill_section("FINISH-1", "all"),
    ])
    return Recipe("P-110", "Safety Abort", proc,
                  parameters={}, batch_size_kg=600.0, product="SAFETY",
                  version="0.9",
                  notes="drill recipe: after the safe drain only the "
                        "recharge remains, so the nominal size is 600 kg")


def recipe_unit_test() -> Recipe:
    proc = Procedure("P-111", [
        reactor_section("REACTOR-1",
                        [("RM-1", 500.0, 22.0), ("RM-2", 300.0, 22.0)],
                        50.0, 45.0, prop="unit_procedure"),
        letdown_section("REACTOR-1", "PREMIX-1"),
        mill_section("PREMIX-1", "FINISH-1", "MILL-1", 12.0),
        fill_section("FINISH-1", "all"),
    ])
    return Recipe("P-111", "Unit Test", proc,
                  parameters={}, batch_size_kg=800.0, product="UNIT-TEST",
                  version="1.0")


def recipe_rework() -> Recipe:
    """Rework batch: two recovered lots blended in parallel feeds."""
    proc = Procedure("P-112", [
        UnitProcedure("BLEND", [
            Operation("FEED-BOTH", [
                Parallel([
                    [Acquire(["LETDOWN"], priority=2),
                     feed_phase("FEED-A", "REACTOR-2", "RM-1", 600.0, 22.0),
                     Release(["LETDOWN"])],
                    [Acquire(["ADD-LINE"], priority=3),
                     feed_phase("FEED-B", "REACTOR-2", "RM-3", 300.0, 22.0,
                                line="ADD-LINE", pump="P-ADD"),
                     Release(["ADD-LINE"])],
                ], name="parallel feed"),
                Phase("BLEND", "REACTOR-2",
                      [ops.Agitate("REACTOR-2", 15.0, "blend rework"),
                       ops.RecordQuality("rework-blend", passed=True)],
                      on_hold=_hold_seq([], "", "REACTOR-2"),
                      on_restart=_restart_seq([], "", "REACTOR-2"),
                      propagation="operation",
                      description="blend rework lots"),
            ]),
        ], description="blend rework lots"),
        letdown_section("REACTOR-2", "PREMIX-2"),
        mill_section("PREMIX-2", "FINISH-2", "MILL-2", 10.0),
        fill_section("FINISH-2", "all", priority=9),
    ])
    return Recipe("P-112", "Rework", proc,
                  parameters={}, batch_size_kg=850.0, product="REWORK",
                  version="1.0")


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
RECIPE_BUILDERS = [
    recipe_white_base, recipe_tint_blue, recipe_fast_gray, recipe_primer,
    recipe_gloss_red, recipe_matte_olive, recipe_clearcoat, recipe_filler,
    recipe_deep_base, recipe_safety_abort, recipe_unit_test, recipe_rework,
]


def all_recipes() -> Dict[str, Recipe]:
    out = {}
    for build in RECIPE_BUILDERS:
        r = build()
        out[r.recipe_id] = r
    return out


def seed_raw_materials(plant) -> None:
    """Fill the raw-material tanks so recipes can draw from them."""
    from .sim import Material
    seed = {
        "RM-1": ("resin", 60000.0, 22.0),
        "RM-2": ("filler", 50000.0, 22.0),
        "RM-3": ("additive-A", 20000.0, 22.0),
        "RM-4": ("additive-B", 20000.0, 22.0),
    }
    for name, (mat, kg, temp) in seed.items():
        tank = plant.tanks[name]
        tank.capacity_kg = kg
        tank.add_material(Material(mat, kg, temp, f"LOT-{name}-001"))
