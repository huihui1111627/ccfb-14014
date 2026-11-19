"""ISA-88 style state machine for a *phase* (the bottom level of the recipe).

The phase is the only level that touches real equipment, so it is the only
level with the full state set.  Upper levels (operation, unit procedure,
procedure) are *derived* from their children (see ``engine.py``).

States
------
IDLE          -> RUNNING            start
RUNNING       -> COMPLETE           done (phase finished its work)
              -> HELD               operator/upper level hold
              -> STOPPING           operator/upper level stop
              -> ABORTING           abort
RUNNING       -> HOLDING            (hold command accepted, phase is
                                     physically ramping down)
HOLDING       -> HELD               physical hold finished
HOLDING       -> STOPPING/ABORTING  if stop/abort arrives while holding
HELD          -> RESTARTING         restart command
              -> STOPPING           stop
              -> ABORTING           abort
RESTARTING    -> RUNNING            physical restart finished
              -> HELD               if a hold arrives while restarting
STOPPING      -> STOPPED
STOPPED       -> ABORTING
              -> IDLE               reset
ABORTING      -> ABORTED
ABORTED       -> IDLE               reset
COMPLETE      -> IDLE               reset

Commands
--------
START, HOLD, RESTART, STOP, ABORT, RESET.  A command is illegal in every
state not listed above; the engine raises ``IllegalCommand`` and the
attempt is recorded so it shows up in the batch record.

Hold/restart are *not* pause/resume: the phase author supplies
``on_hold`` and ``on_restart`` sequences that act on the equipment
(stop pump, close valve, restart agitator, ...).  The state machine
waits for those sequences to finish before it reports HELD/RUNNING.
"""

from __future__ import annotations

from enum import Enum


class PhaseState(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    HOLDING = "HOLDING"
    HELD = "HELD"
    RESTARTING = "RESTARTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ABORTING = "ABORTING"
    ABORTED = "ABORTED"
    COMPLETE = "COMPLETE"


#: all states, in the order used by the transition table in the UI / report
PHASE_STATES = list(PhaseState)


class Cmd(str, Enum):
    START = "START"
    HOLD = "HOLD"
    RESTART = "RESTART"
    STOP = "STOP"
    ABORT = "ABORT"
    RESET = "RESET"


class IllegalCommand(Exception):
    """Raised when a command is not legal for the current state."""

    def __init__(self, phase: str, state: str, command: str):
        super().__init__(
            f"command {command} is illegal in state {state} for phase {phase}"
        )
        self.phase = phase
        self.state = state
        self.command = command


# transition table: (state, command) -> next state
TRANSITIONS = {
    (PhaseState.IDLE, Cmd.START): PhaseState.RUNNING,
    (PhaseState.IDLE, Cmd.RESET): PhaseState.IDLE,
    (PhaseState.RUNNING, Cmd.HOLD): PhaseState.HOLDING,
    (PhaseState.RUNNING, Cmd.STOP): PhaseState.STOPPING,
    (PhaseState.RUNNING, Cmd.ABORT): PhaseState.ABORTING,
    (PhaseState.HOLDING, Cmd.STOP): PhaseState.STOPPING,
    (PhaseState.HOLDING, Cmd.ABORT): PhaseState.ABORTING,
    (PhaseState.HELD, Cmd.RESTART): PhaseState.RESTARTING,
    (PhaseState.HELD, Cmd.STOP): PhaseState.STOPPING,
    (PhaseState.HELD, Cmd.ABORT): PhaseState.ABORTING,
    (PhaseState.RESTARTING, Cmd.HOLD): PhaseState.HELD,
    (PhaseState.RESTARTING, Cmd.STOP): PhaseState.STOPPING,
    (PhaseState.RESTARTING, Cmd.ABORT): PhaseState.ABORTING,
    (PhaseState.STOPPING, Cmd.ABORT): PhaseState.ABORTING,
    (PhaseState.STOPPED, Cmd.ABORT): PhaseState.ABORTING,
    (PhaseState.STOPPED, Cmd.RESET): PhaseState.IDLE,
    (PhaseState.ABORTING, Cmd.RESET): PhaseState.ABORTING,
    (PhaseState.ABORTED, Cmd.RESET): PhaseState.IDLE,
    (PhaseState.COMPLETE, Cmd.RESET): PhaseState.IDLE,
}

#: legal commands per state, derived once for validation / documentation
LEGAL_COMMANDS = {}
for (_st, _cmd) in TRANSITIONS:
    LEGAL_COMMANDS.setdefault(_st, set()).add(_cmd)

#: commands legal in *any* non-terminal state (nothing extra today, but the
#: hook makes the intent explicit and gives the engine one place to look)
UNIVERSAL_COMMANDS = set()

#: states in which holding is active or in progress
HOLD_STATES = {PhaseState.HOLDING, PhaseState.HELD}
#: states from which an abort is legal
ABORTABLE_STATES = {s for (s, c) in TRANSITIONS if c is Cmd.ABORT}
#: terminal-ish states that the upper levels report as "finished"
DONE_STATES = {PhaseState.COMPLETE, PhaseState.STOPPED, PhaseState.ABORTED}


def is_legal(state: PhaseState, command: Cmd) -> bool:
    return (state, command) in TRANSITIONS


#: transitions the runtime performs on its own when a sequence finishes
#: (a command opened the "ing" state; the written sequence then completes)
AUTOMATIC_TRANSITIONS = {
    # the main operation list finished
    (PhaseState.RUNNING, PhaseState.COMPLETE),
    # a written hold / restart / stop / abort sequence finished
    (PhaseState.HOLDING, PhaseState.HELD),
    (PhaseState.RESTARTING, PhaseState.RUNNING),
    (PhaseState.STOPPING, PhaseState.STOPPED),
    (PhaseState.ABORTING, PhaseState.ABORTED),
}


def legal_pairs():
    """Every (from, to) the phase state machine may ever produce."""
    pairs = {(s.value, n.value) for (s, _c), n in TRANSITIONS.items()}
    pairs |= {(a.value, b.value) for a, b in AUTOMATIC_TRANSITIONS}
    return pairs


def next_state(state: PhaseState, command: Cmd, phase_name: str = "") -> PhaseState:
    try:
        return TRANSITIONS[(state, command)]
    except KeyError:
        raise IllegalCommand(phase_name or "?", state.value, command.value) from None
