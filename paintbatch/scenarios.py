"""Acceptance scenarios.

The catalogue covers the acceptance brief:

* all twelve recipes run, several at a time on one plant;
* operator holds / restarts / aborts are issued mid-batch by the script;
* faults are injected: stuck valve, tripped pump, mill trip, failed
  temperature sensor (no signal and stuck reading);
* every scenario states its expected per-phase transition table and the
  expected final batch states.

Scenarios are plain data; ``acceptance.py`` runs and verifies them.
"""

from __future__ import annotations

from typing import List

from .runner import Hook, Scenario, StepLog
from .states import Cmd

# --------------------------------------------------------------------------
# handy transition tables
# --------------------------------------------------------------------------
IDLE_RUN_COMPLETE = [("IDLE", "RUNNING"), ("RUNNING", "COMPLETE")]
IDLE_RUN_HOLD_HELD_RESTART_RUN_COMPLETE = [
    ("IDLE", "RUNNING"), ("RUNNING", "HOLDING"), ("HOLDING", "HELD"),
    ("HELD", "RESTARTING"), ("RESTARTING", "RUNNING"), ("RUNNING", "COMPLETE"),
]
IDLE_RUN_HOLD_HELD_ABORT_ABORTED = [
    ("IDLE", "RUNNING"), ("RUNNING", "HOLDING"), ("HOLDING", "HELD"),
    ("HELD", "ABORTING"), ("ABORTING", "ABORTED"),
]
IDLE_RUN_ABORT_ABORTED = [
    ("IDLE", "RUNNING"), ("RUNNING", "ABORTING"), ("ABORTING", "ABORTED"),
]
IDLE_RUN_STOP_STOPPED = [
    ("IDLE", "RUNNING"), ("RUNNING", "STOPPING"), ("STOPPING", "STOPPED"),
]


def at(seconds: float) -> int:
    return int(seconds * 1000)


# --------------------------------------------------------------------------
# S1: single batch, straight through
# --------------------------------------------------------------------------
def s1_single_batch() -> Scenario:
    return Scenario(
        name="S1 single batch P-101",
        description="one white-base batch end to end; no interruption",
        batches=[dict(recipe="P-101", batch_id="B101", lot="LOT-101",
                      priority=5)],
        max_ms=at(1200),
        expected_states={"B101": "COMPLETE"},
        expectations=[
            StepLog("FEED-RM-1-1", IDLE_RUN_COMPLETE),
            StepLog("COOK", IDLE_RUN_COMPLETE),
            StepLog("FILL", IDLE_RUN_COMPLETE),
        ],
    )


# --------------------------------------------------------------------------
# S2: three recipes at once, contending for the shared headers
# --------------------------------------------------------------------------
def s2_parallel_batches() -> Scenario:
    return Scenario(
        name="S2 three batches in parallel",
        description="P-101 + P-102 + P-104 on one plant; all share the "
                    "headers and the filler line",
        batches=[
            dict(recipe="P-101", batch_id="B201", lot="LOT-201", priority=5),
            dict(recipe="P-102", batch_id="B202", lot="LOT-202", priority=5),
            dict(recipe="P-104", batch_id="B203", lot="LOT-203", priority=4),
        ],
        max_ms=at(2400),
        expected_states={"B201": "COMPLETE", "B202": "COMPLETE",
                         "B203": "COMPLETE"},
        expectations=[
            StepLog("COOK", IDLE_RUN_COMPLETE),
            StepLog("FILL", IDLE_RUN_COMPLETE),
        ],
    )


# --------------------------------------------------------------------------
# S3: operator hold / restart during the cook
# --------------------------------------------------------------------------
def s3_hold_restart() -> Scenario:
    def hold(run):
        run.cmd("B301", Cmd.HOLD, "operator: viscosity check")

    def restart(run):
        run.cmd("B301", Cmd.RESTART, "operator: release after check")

    def hold_again(run):
        run.cmd("B301", Cmd.HOLD, "operator: second check")

    def restart_again(run):
        run.cmd("B301", Cmd.RESTART, "operator: release")

    return Scenario(
        name="S3 hold and restart",
        description="two operator holds during the cook; written hold and "
                    "restart sequences must run and the cook resumes",
        batches=[dict(recipe="P-105", batch_id="B301", lot="LOT-301",
                      priority=5)],
        script=[
            Hook(at(180), "hold during cook", hold),
            Hook(at(240), "restart", restart),
            Hook(at(260), "hold again", hold_again),
            Hook(at(300), "restart again", restart_again),
        ],
        max_ms=at(1400),
        expected_states={"B301": "COMPLETE"},
        expectations=[
            StepLog("COOK",
                    IDLE_RUN_HOLD_HELD_RESTART_RUN_COMPLETE + [
                        ("RUNNING", "HOLDING"), ("HOLDING", "HELD"),
                        ("HELD", "RESTARTING"), ("RESTARTING", "RUNNING")]),
        ],
    )


# --------------------------------------------------------------------------
# S4: stuck valve during a transfer
# --------------------------------------------------------------------------
def s4_stuck_valve() -> Scenario:
    def stick(run):
        run.inject("V-LD-R1 stuck closed", lambda: run.plant.stick_valve(
            "V-LD-R1"))

    def free(run):
        run.inject("V-LD-R1 freed", lambda: run.plant.free_valve("V-LD-R1"))

    def restart(run):
        run.cmd("B401", Cmd.RESTART, "valve repaired")

    return Scenario(
        name="S4 stuck valve -> hold -> restart",
        description="charge valve sticks closed; the feed phase faults into "
                    "hold, the operator frees the valve and restarts",
        batches=[dict(recipe="P-108", batch_id="B401", lot="LOT-401",
                      priority=5)],
        script=[
            Hook(at(4.5), "stick V-LD-R1", stick),
            Hook(at(26), "free valve", free),
            Hook(at(28), "restart", restart),
        ],
        max_ms=at(2400),
        expected_states={"B401": "COMPLETE"},
        expectations=[
            StepLog("FEED-RM-2-1",
                    IDLE_RUN_HOLD_HELD_RESTART_RUN_COMPLETE, contains=True),
        ],
    )


# --------------------------------------------------------------------------
# S5: pump trip and sensor failure with propagation to the unit procedure
# --------------------------------------------------------------------------
def s5_pump_trip() -> Scenario:
    def trip(run):
        run.inject("P-LD motor trip", lambda: run.plant.fail_pump("P-LD"))

    def repair(run):
        run.inject("P-LD motor replaced",
                   lambda: setattr(run.plant.pumps["P-LD"], "failed", False))

    def restart(run):
        run.cmd("B501", Cmd.RESTART, "pump repaired")

    return Scenario(
        name="S5 pump trip with unit-procedure propagation",
        description="the header pump trips mid-charge; the feed phase holds, "
                    "propagation=unit_procedure must hold its siblings; the "
                    "operator replaces the motor then restarts",
        batches=[dict(recipe="P-111", batch_id="B501", lot="LOT-501",
                      priority=5)],
        script=[
            Hook(at(9), "trip P-LD", trip),
            Hook(at(24), "replace motor", repair),
            Hook(at(26), "restart", restart),
        ],
        max_ms=at(1500),
        expected_states={"B501": "COMPLETE"},
        expectations=[
            StepLog("FEED-RM-1-1",
                    [("IDLE", "RUNNING"), ("RUNNING", "HOLDING"),
                     ("HOLDING", "HELD"), ("HELD", "RESTARTING"),
                     ("RESTARTING", "RUNNING")], contains=True),
        ],
    )


# --------------------------------------------------------------------------
# S6: failed temperature sensor -> hold -> operator restart
# --------------------------------------------------------------------------
def s6_sensor_failure() -> Scenario:
    def fail(run):
        run.inject("REACTOR-2 temperature sensor open circuit",
                   lambda: run.plant.fail_temp_sensor("REACTOR-2", None))

    def fix(run):
        run.inject("sensor replaced",
                   lambda: run.plant.fix_temp_sensor("REACTOR-2"))

    def restart(run):
        run.cmd("B601", Cmd.RESTART, "sensor replaced")

    return Scenario(
        name="S6 temperature sensor failure",
        description="a failed RTD makes HeatTo raise a fault; the phase holds "
                    "and the cook resumes after the sensor is replaced",
        batches=[dict(recipe="P-109", batch_id="B601", lot="LOT-601",
                      priority=5)],
        script=[
            Hook(at(35), "fail sensor", fail),
            Hook(at(60), "replace sensor", fix),
            Hook(at(62), "restart", restart),
        ],
        max_ms=at(1600),
        expected_states={"B601": "COMPLETE"},
        expectations=[
            StepLog("COOK", IDLE_RUN_HOLD_HELD_RESTART_RUN_COMPLETE,
                    contains=True),
        ],
    )


# --------------------------------------------------------------------------
# S7: abort mid batch, drain to waste, resources released
# --------------------------------------------------------------------------
def s7_abort() -> Scenario:
    def abort(run):
        run.cmd("B701", Cmd.ABORT, "operator: wrong additive charged")

    return Scenario(
        name="S7 abort mid cook",
        description="batch is aborted during the cook; the abort sequence "
                    "runs, the reactor drains to waste and every resource is "
                    "released in safe order",
        batches=[dict(recipe="P-105", batch_id="B701", lot="LOT-701",
                      priority=5)],
        script=[Hook(at(220), "abort", abort)],
        max_ms=at(900),
        expected_states={"B701": "ABORTED"},
        expectations=[
            StepLog("COOK", IDLE_RUN_ABORT_ABORTED, contains=True),
        ],
    )


# --------------------------------------------------------------------------
# S8: hold one batch while a second keeps running (no cross-batch coupling)
# --------------------------------------------------------------------------
def s8_hold_one_of_two() -> Scenario:
    def hold(run):
        run.cmd("B801", Cmd.HOLD, "operator hold on B801 only")

    def restart(run):
        run.cmd("B801", Cmd.RESTART, "release B801")

    return Scenario(
        name="S8 hold one of two batches",
        description="holding B801 must not touch B802; B802 keeps running and "
                    "the shared header passes from one to the other cleanly",
        batches=[
            dict(recipe="P-101", batch_id="B801", lot="LOT-801", priority=5),
            dict(recipe="P-102", batch_id="B802", lot="LOT-802", priority=5),
        ],
        script=[
            Hook(at(60), "hold B801", hold),
            Hook(at(150), "restart B801", restart),
        ],
        max_ms=at(2200),
        expected_states={"B801": "COMPLETE", "B802": "COMPLETE"},
        expectations=[
            StepLog("COOK", IDLE_RUN_HOLD_HELD_RESTART_RUN_COMPLETE,
                    contains=True, batch="B801"),
        ],
    )


# --------------------------------------------------------------------------
# S9: mill trip (exclusive equipment)
# --------------------------------------------------------------------------
def s9_mill_trip() -> Scenario:
    def trip(run):
        run.inject("MILL-1 bearing trip",
                   lambda: run.plant.fail_machine("MILL-1"))

    return Scenario(
        name="S9 mill trip during grinding",
        description="the mill trips while grinding; the GRIND phase faults "
                    "into hold and the batch stays held for maintenance",
        batches=[dict(recipe="P-106", batch_id="B901", lot="LOT-901",
                      priority=5)],
        script=[Hook(at(100), "trip mill", trip)],
        max_ms=at(900),
        expected_states={"B901": "HELD"},
        expectations=[
            StepLog("GRIND-MILL-1", [("IDLE", "RUNNING"),
                                     ("RUNNING", "HOLDING"),
                                     ("HOLDING", "HELD")], contains=False),
        ],
    )


# --------------------------------------------------------------------------
# S10: stop then reset; a second batch takes the equipment
# --------------------------------------------------------------------------
def s10_stop_and_takeover() -> Scenario:
    def stop(run):
        run.cmd("BA01", Cmd.STOP, "operator stop")

    return Scenario(
        name="S10 stop one batch, next batch takes the plant",
        description="BA01 is stopped mid cook; BA02 is queued on the same "
                    "reactor and must be granted it only after BA01's phases "
                    "reach STOPPED and its resources are released",
        batches=[
            dict(recipe="P-101", batch_id="BA01", lot="LOT-A01", priority=3),
            dict(recipe="P-103", batch_id="BA02", lot="LOT-A02", priority=3),
        ],
        script=[Hook(at(120), "stop BA01", stop)],
        max_ms=at(2400),
        expected_states={"BA01": "STOPPED", "BA02": "COMPLETE"},
        expectations=[
            StepLog("COOK", IDLE_RUN_STOP_STOPPED, batch="BA01"),
        ],
    )


# --------------------------------------------------------------------------
# S11: a wrong (illegal) command must be rejected, not executed
# --------------------------------------------------------------------------
def s11_illegal_commands() -> Scenario:
    def bad_restart(run):
        # nothing is held here; restart must be rejected
        run.cmd("BB01", Cmd.RESTART, "spurious restart from the HMI")

    def hold(run):
        run.cmd("BB01", Cmd.HOLD, "operator hold")

    def double_hold(run):
        run.cmd("BB01", Cmd.HOLD, "operator presses hold twice")

    def restart(run):
        run.cmd("BB01", Cmd.RESTART, "restart")

    def reset_done(run):
        run.cmd("BB01", Cmd.RESET, "reset while running (illegal)")

    return Scenario(
        name="S11 illegal commands rejected",
        description="spurious restart / double hold / reset must be rejected "
                    "and must not disturb the batch",
        batches=[dict(recipe="P-107", batch_id="BB01", lot="LOT-B01",
                      priority=5)],
        script=[
            Hook(at(20), "spurious restart", bad_restart),
            Hook(at(40), "hold", hold),
            Hook(at(45), "hold again", double_hold),
            Hook(at(70), "restart", restart),
            Hook(at(90), "reset while running", reset_done),
        ],
        max_ms=at(900),
        expected_states={"BB01": "COMPLETE"},
        expectations=[StepLog("FILL", IDLE_RUN_COMPLETE)],
    )


# --------------------------------------------------------------------------
# S12: four batches, staggered, shared filler line - the incident scenario
# --------------------------------------------------------------------------
def s12_fill_line_contention() -> Scenario:
    def hold(run):
        run.cmd("BC02", Cmd.HOLD, "line changeover on the filler")

    def restart(run):
        run.cmd("BC02", Cmd.RESTART, "line ready")

    return Scenario(
        name="S12 filler line contention",
        description="four batches staggered so that two reach the filler at "
                    "once; the line is a shared resource and only one batch "
                    "may carry material on it at any instant",
        batches=[
            dict(recipe="P-101", batch_id="BC01", lot="LOT-C01", priority=6),
            dict(recipe="P-107", batch_id="BC02", lot="LOT-C02", priority=6),
            dict(recipe="P-107", batch_id="BC03", lot="LOT-C03", priority=6),
            dict(recipe="P-106", batch_id="BC04", lot="LOT-C04", priority=7),
        ],
        script=[
            Hook(at(120), "hold BC02", hold),
            Hook(at(200), "restart BC02", restart),
        ],
        max_ms=at(3000),
        expected_states={"BC01": "COMPLETE", "BC02": "COMPLETE",
                         "BC03": "COMPLETE", "BC04": "COMPLETE"},
        expectations=[
            StepLog("FILL", IDLE_RUN_COMPLETE),
        ],
    )


def catalogue() -> List[Scenario]:
    return [
        s1_single_batch(),
        s2_parallel_batches(),
        s3_hold_restart(),
        s4_stuck_valve(),
        s5_pump_trip(),
        s6_sensor_failure(),
        s7_abort(),
        s8_hold_one_of_two(),
        s9_mill_trip(),
        s10_stop_and_takeover(),
        s11_illegal_commands(),
        s12_fill_line_contention(),
    ]
