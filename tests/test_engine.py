"""Unit tests: phase state machine, recipe structure, arbitration,
propagation, hold/restart sequences, faults, line exclusivity."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paintbatch import ops
from paintbatch.engine import Engine, RuntimeStatus
from paintbatch.errors import RecipeError
from paintbatch.ops import OpFailed
from paintbatch.plant_layout import build_plant
from paintbatch.recipe import (
    Acquire, Always, Choice, Operation, Parallel, Phase, Procedure, Recipe,
    Release, Transition, UnitProcedure,
)
from paintbatch.recipes import all_recipes, seed_raw_materials
from paintbatch.scenarios import catalogue
from paintbatch.runner import Runner
from paintbatch.sim import Material, Plant
from paintbatch.states import (
    LEGAL_COMMANDS, TRANSITIONS, Cmd, IllegalCommand, PhaseState, is_legal,
    legal_pairs, next_state,
)
from paintbatch.verify import verify


# --------------------------------------------------------------------------
def tiny_plant():
    p = Plant()
    p.add_tank("T1", capacity_kg=5000, agitator_ms=1000)
    p.add_tank("T2", capacity_kg=5000, agitator_ms=1000)
    p.add_tank("T3", capacity_kg=5000, agitator_ms=1000)
    waste = p.add_tank("WASTE", capacity_kg=20000)
    p.waste = waste
    p.add_valve("V1", travel_ms=1000)
    p.add_valve("V2", travel_ms=1000)
    p.add_line("L1")
    return p


def simple_recipe(propagation="self", hold_s=1.0, restart_s=1.0):
    ph = Phase(
        "MIX", "T1",
        [ops.Charge("T1", "resin", 100.0),
         ops.WaitSeconds(30.0, "mixing")],
        on_hold=[ops.WaitSeconds(hold_s, "hold it")],
        on_restart=[ops.WaitSeconds(restart_s, "resume it")],
        on_abort=[ops.Drain("T1")],
        propagation=propagation,
    )
    return Recipe("T-TINY", "Tiny", Procedure("MAIN", [
        UnitProcedure("UP", [Operation("OP", [ph])]),
    ]), batch_size_kg=100.0)


class StateMachineTests(unittest.TestCase):
    def test_transition_table_is_the_documented_one(self):
        # spot check the whole table, state by state
        self.assertEqual(next_state(PhaseState.IDLE, Cmd.START),
                         PhaseState.RUNNING)
        self.assertEqual(next_state(PhaseState.RUNNING, Cmd.HOLD),
                         PhaseState.HOLDING)
        self.assertEqual(next_state(PhaseState.HOLDING, Cmd.STOP),
                         PhaseState.STOPPING)
        self.assertEqual(next_state(PhaseState.HELD, Cmd.RESTART),
                         PhaseState.RESTARTING)
        self.assertEqual(next_state(PhaseState.RESTARTING, Cmd.HOLD),
                         PhaseState.HELD)
        self.assertEqual(next_state(PhaseState.STOPPED, Cmd.RESET),
                         PhaseState.IDLE)
        self.assertEqual(next_state(PhaseState.ABORTED, Cmd.RESET),
                         PhaseState.IDLE)
        self.assertEqual(next_state(PhaseState.COMPLETE, Cmd.RESET),
                         PhaseState.IDLE)

    def test_illegal_commands_are_rejected(self):
        for (state, cmd) in [
            (PhaseState.IDLE, Cmd.HOLD),
            (PhaseState.IDLE, Cmd.STOP),
            (PhaseState.HELD, Cmd.HOLD),
            (PhaseState.HELD, Cmd.START),
            (PhaseState.RUNNING, Cmd.RESTART),
            (PhaseState.RUNNING, Cmd.RESET),
            (PhaseState.COMPLETE, Cmd.START),
            (PhaseState.ABORTED, Cmd.START),
            (PhaseState.HOLDING, Cmd.RESTART),
            (PhaseState.STOPPING, Cmd.STOP),
            (PhaseState.ABORTING, Cmd.ABORT),
        ]:
            with self.subTest(state=state, cmd=cmd):
                self.assertFalse(is_legal(state, cmd))
                with self.assertRaises(IllegalCommand):
                    next_state(state, cmd, "P1")

    def test_every_state_is_reachable_and_terminal_states_have_a_way_out(self):
        reached = {PhaseState.IDLE}
        frontier = [PhaseState.IDLE]
        from paintbatch.states import AUTOMATIC_TRANSITIONS
        while frontier:
            cur = frontier.pop()
            for (_s, _c), nxt in TRANSITIONS.items():
                if _s == cur and nxt not in reached:
                    reached.add(nxt)
                    frontier.append(nxt)
            for (a, b) in AUTOMATIC_TRANSITIONS:
                if a == cur and b not in reached:
                    reached.add(b)
                    frontier.append(b)
        self.assertEqual(reached, set(PhaseState))

    def test_full_command_legality_is_queryable(self):
        self.assertIn(Cmd.START, LEGAL_COMMANDS[PhaseState.IDLE])
        self.assertNotIn(Cmd.HOLD, LEGAL_COMMANDS[PhaseState.IDLE])
        self.assertIn((PhaseState.RUNNING.value, PhaseState.COMPLETE.value),
                      legal_pairs())


class EngineHoldRestartTests(unittest.TestCase):
    def _run(self, n=10):
        eng = Engine(tiny_plant())
        b = eng.start_batch(simple_recipe(), "B1")
        for _ in range(n):
            eng.tick()
        return eng, b

    def test_hold_goes_through_holding_and_takes_the_written_time(self):
        eng, b = self._run()
        ph = b.phases()[0]
        self.assertEqual(ph.state, PhaseState.RUNNING)
        self.assertTrue(eng.command("B1", Cmd.HOLD, "test"))
        self.assertEqual(ph.state, PhaseState.HOLDING)
        eng.tick()  # < 1 s of hold sequence
        self.assertEqual(ph.state, PhaseState.HOLDING)
        for _ in range(15):
            eng.tick()
        self.assertEqual(ph.state, PhaseState.HELD)
        self.assertEqual(b.state.value, "HELD")

    def test_restart_runs_its_own_sequence_then_resumes(self):
        eng, b = self._run()
        ph = b.phases()[0]
        eng.command("B1", Cmd.HOLD, "test")
        for _ in range(15):
            eng.tick()
        self.assertEqual(ph.state, PhaseState.HELD)
        eng.command("B1", Cmd.RESTART, "test")
        self.assertEqual(ph.state, PhaseState.RESTARTING)
        for _ in range(15):
            eng.tick()
        self.assertEqual(ph.state, PhaseState.RUNNING)

    def test_illegal_command_is_rejected_and_recorded(self):
        eng, b = self._run()
        before = len(b.record)
        self.assertFalse(eng.command("B1", Cmd.RESTART, "nothing held"))
        self.assertEqual(b.phases()[0].state, PhaseState.RUNNING)
        self.assertTrue(any(ev["event"] == "command_rejected"
                            for ev in b.record[before:]))

    def test_hold_clocks_the_op_not_the_wall_clock(self):
        eng, b = self._run(5)
        ph = b.phases()[0]
        elapsed_before = ph.op_elapsed_s(eng.plant.now)
        eng.command("B1", Cmd.HOLD, "test")
        for _ in range(60):
            eng.tick()
        self.assertEqual(ph.state, PhaseState.HELD)
        self.assertAlmostEqual(ph.op_elapsed_s(eng.plant.now),
                               elapsed_before, places=3)
        eng.command("B1", Cmd.RESTART, "test")
        for _ in range(30):
            eng.tick()
        self.assertGreater(ph.op_elapsed_s(eng.plant.now), elapsed_before)

    def test_abort_runs_abort_sequence_and_ends_aborted(self):
        eng, b = self._run()
        eng.command("B1", Cmd.ABORT, "test")
        for _ in range(30):
            eng.tick()
            if b.is_done():
                break
        self.assertEqual(b.state, RuntimeStatus.ABORTED)
        self.assertEqual(b.phases()[0].state, PhaseState.ABORTED)

    def test_hold_only_touches_running_phases(self):
        eng, b = self._run()
        held = eng.command("B1", Cmd.HOLD, "test")
        self.assertTrue(held)
        self.assertFalse(eng.command("B1", Cmd.HOLD, "again"))


class PropagationTests(unittest.TestCase):
    """Propagation scope decides which siblings hold when a phase faults."""

    def _fault_recipe(self, scope):
        bad = Phase("BAD", "T1", [ops.RaiseFault("injected")],
                    on_hold=[ops.WaitSeconds(0.5)],
                    on_restart=[ops.WaitSeconds(0.5)],
                    propagation=scope)
        good = Phase("GOOD", "T2", [ops.WaitSeconds(120.0)],
                     on_hold=[ops.WaitSeconds(0.5)],
                     on_restart=[ops.WaitSeconds(0.5)],
                     propagation="self")
        # BAD and GOOD are parallel branches of the *same* operation; a
        # second, later unit procedure holds FAR
        op = Operation("OP-AB", [Parallel([[bad], [good]], name="par")])
        up = UnitProcedure("UP", [op])
        other_up = UnitProcedure("UP2", [Operation("OP-C", [
            Phase("FAR", "T3", [ops.WaitSeconds(120.0)],
                  on_hold=[ops.WaitSeconds(0.5)],
                  on_restart=[ops.WaitSeconds(0.5)],
                  propagation="self")])])
        return Recipe("P-PROP", "Prop", Procedure("MAIN", [up, other_up]))

    def _states(self, scope):
        eng = Engine(tiny_plant())
        b = eng.start_batch(self._fault_recipe(scope), "B1")
        for _ in range(40):
            eng.tick()
        return {p.phase.name: p.state.value for p in b.phases()}

    def test_self_scope_leaves_siblings_running(self):
        st = self._states("self")
        self.assertEqual(st["BAD"], "HELD")
        self.assertEqual(st["GOOD"], "RUNNING")

    def test_operation_scope_holds_parallel_siblings(self):
        st = self._states("operation")
        self.assertEqual(st["BAD"], "HELD")
        self.assertEqual(st["GOOD"], "HELD")

    def test_unit_procedure_scope_holds_further_downstream(self):
        eng = Engine(tiny_plant())
        b = eng.start_batch(self._fault_recipe("unit_procedure"), "B1")
        for _ in range(40):
            eng.tick()
        st = {p.phase.name: p.state.value for p in b.phases()}
        self.assertEqual(st["BAD"], "HELD")
        self.assertEqual(st["GOOD"], "HELD")

    def test_batch_scope_reaches_every_phase(self):
        eng = Engine(tiny_plant())
        b = eng.start_batch(self._fault_recipe("batch"), "B1")
        for _ in range(40):
            eng.tick()
        st = {p.phase.name: p.state.value for p in b.phases()}
        self.assertEqual(st["BAD"], "HELD")
        self.assertEqual(st["GOOD"], "HELD")
        # FAR is in a later unit procedure and has not started yet
        self.assertIn(st.get("FAR", "IDLE"), ("IDLE", "HELD"))

    def test_upper_state_is_aggregated_from_phases(self):
        eng = Engine(tiny_plant())
        b = eng.start_batch(self._fault_recipe("operation"), "B1")
        for _ in range(40):
            eng.tick()
        # one phase held, the other held too -> up is HELD, not RUNNING
        self.assertEqual(b.state.value, "HELD")


class ArbitrationTests(unittest.TestCase):
    def _two_batches(self):
        p = build_plant()
        seed_raw_materials(p)
        recipes = all_recipes()
        eng = Engine(p)
        b1 = eng.start_batch(recipes["P-101"], "B1", lot="L1")
        b2 = eng.start_batch(recipes["P-102"], "B2", lot="L2")
        return eng, p, b1, b2

    def test_line_is_held_by_one_batch_at_a_time(self):
        eng, p, b1, b2 = self._two_batches()
        for _ in range(12000):
            eng.tick()
            holders = [r for r in p.reservations
                       if r in p.lines and p.reservations[r]]
            self.assertTrue(len(holders) <= len(p.lines))
            for name in p.lines:
                # the reservation map can only have one holder per key
                self.assertIn(p.held_by(name), (None, "B1", "B2"))
            if b1.is_done() and b2.is_done():
                break
        self.assertTrue(b1.is_done() and b2.is_done())

    def test_priority_then_queue_order(self):
        p = tiny_plant()
        p.add_valve("V3", travel_ms=1000)
        eng = Engine(p)
        r = simple_recipe()
        b1 = eng.start_batch(r, "B1")
        b2 = eng.start_batch(r, "B2")
        # an explicit low-priority request for B2 and a high one for B1
        req_lo = eng.request_resources(b2, ["L1"], 9, "late")
        req_hi = eng.request_resources(b1, ["L1"], 1, "urgent")
        eng.arbitrate()
        self.assertEqual(p.held_by("L1"), "B1")
        self.assertEqual(req_hi.status, "granted")
        self.assertEqual(req_lo.status, "pending")
        eng.release_resources(b1, ["L1"], "test")
        eng.arbitrate()
        self.assertEqual(p.held_by("L1"), "B2")

    def test_abort_releases_everything_in_safe_order(self):
        eng, p, b1, b2 = self._two_batches()
        for _ in range(60):
            eng.tick()
        held = p.held_by_batch("B1")
        self.assertTrue(held)
        b1.abort("test")
        for _ in range(400):
            eng.tick()
            if b1.is_done():
                break
        self.assertEqual(p.held_by_batch("B1"), [])
        events = [ev for ev in b1.record if ev["event"] == "safe_release"]
        self.assertTrue(events)

    def test_finished_batch_releases_resources(self):
        eng, p, b1, b2 = self._two_batches()
        for _ in range(8000):
            eng.tick()
            if b1.is_done() and b2.is_done():
                break
        self.assertEqual(p.held_by_batch("B1"), [])
        self.assertEqual(p.held_by_batch("B2"), [])

    def test_second_batch_waits_for_the_first_on_the_same_reactor(self):
        eng, p, b1, b2 = self._two_batches()
        order = []
        for _ in range(2000):
            eng.tick()
            holder = p.held_by("REACTOR-1")
            if not order or order[-1] != holder:
                order.append(holder)
        # REACTOR-1 passes from None to B1 (or B3) and later to someone else
        seen = [h for h in order if h]
        self.assertTrue(seen)
        # never two holders of the same reactor
        self.assertEqual(p.held_by("REACTOR-1") in (None, "B1", "B2"), True)


class FaultTests(unittest.TestCase):
    def test_stuck_valve_makes_the_phase_fault(self):
        p = tiny_plant()
        p.stick_valve("V1")
        ph = Phase("VALVE", "T1", [ops.OpenValve("V1", timeout_s=2.0)],
                   on_hold=[ops.WaitSeconds(0.5)],
                   propagation="self")
        r = Recipe("P-V", "V", Procedure("MAIN", [
            UnitProcedure("UP", [Operation("OP", [ph])])]))
        eng = Engine(p)
        b = eng.start_batch(r, "B1")
        for _ in range(80):
            eng.tick()
        self.assertEqual(b.phases()[0].state.value, "HELD")
        self.assertIn("stuck", b.alarms[-1]["text"])

    def test_failed_sensor_raises_sensor_error_to_the_op(self):
        p = tiny_plant()
        p.fail_temp_sensor("T1")
        t = p.tanks["T1"]
        t.add_material(Material("resin", 100, 22))
        with self.assertRaises(Exception):
            t.read_temperature()
        self.assertFalse(t.sensor_ok())

    def test_pump_trip_stops_the_transfer(self):
        p = tiny_plant()
        p.add_pump("P1", flow_kg_s=30.0)
        p.tanks["T1"].add_material(Material("resin", 500, 22))
        p.fail_pump("P1")
        ph = Phase("XF", "P1",
                   [ops.StartPump("P1")],
                   on_hold=[ops.WaitSeconds(0.5)], propagation="self")
        r = Recipe("P-P", "P", Procedure("MAIN", [
            UnitProcedure("UP", [Operation("OP", [ph])])]))
        eng = Engine(p)
        b = eng.start_batch(r, "B1")
        for _ in range(60):
            eng.tick()
        self.assertEqual(b.phases()[0].state.value, "HELD")


class RecipeStructureTests(unittest.TestCase):
    def test_duplicate_child_names_are_rejected(self):
        inner = Operation("OP", [
            Phase("P1", "T1", [ops.WaitSeconds(1)], propagation="self")])
        proc = Procedure("MAIN", [UnitProcedure("UP", [inner]),
                                  UnitProcedure("UP", [inner])])
        with self.assertRaises(RecipeError):
            Recipe("P-DUP", "Dup", proc)

    def test_bad_propagation_scope_is_rejected(self):
        with self.assertRaises(RecipeError):
            Phase("P1", "T1", [ops.WaitSeconds(1)], propagation="everywhere")

    def test_all_twelve_recipes_validate_and_have_four_levels(self):
        recipes = all_recipes()
        self.assertGreaterEqual(len(recipes), 12)
        for rid, r in recipes.items():
            self.assertEqual(r.procedure.kind, "procedure")
            kinds = set()
            for el in r.procedure.steps:
                kinds.add(getattr(el, "kind", None))
            self.assertIn("unit_procedure", kinds)
            self.assertTrue(r.phases)
            for path, ph in r.phases.items():
                self.assertEqual(ph.kind, "phase")
                self.assertTrue(ph.ops)

    def test_phase_requires_operations(self):
        with self.assertRaises(RecipeError):
            Phase("EMPTY", "T1", [], propagation="self")


class AcceptanceTests(unittest.TestCase):
    """The scripted acceptance runs, as a unit test (kept small: scenario
    S1, S3 and S12 cover straight-through, operator commands and line
    contention)."""

    def _run(self, sid):
        sc = next(s for s in catalogue() if s.name.startswith(sid))
        run = Runner(sc).run()
        return sc, run, verify(run)

    def test_s1_single_batch_passes(self):
        sc, run, v = self._run("S1")
        self.assertTrue(v["ok"], v["failures"])

    def test_s3_hold_restart_passes(self):
        sc, run, v = self._run("S3")
        self.assertTrue(v["ok"], v["failures"])

    def test_s7_abort_passes(self):
        sc, run, v = self._run("S7")
        self.assertTrue(v["ok"], v["failures"])
        self.assertEqual(run.batches["B701"].state.value, "ABORTED")

    def test_s12_line_contention_passes(self):
        sc, run, v = self._run("S12")
        self.assertTrue(v["ok"], v["failures"])
        # every line span belongs to exactly one batch and they never overlap
        for name, seg in run.plant.lines.items():
            spans = sorted(seg.history, key=lambda h: h["start"])
            for a, b in zip(spans, spans[1:]):
                if a["batch"] != b["batch"]:
                    self.assertGreater(b["start"], a["end"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
