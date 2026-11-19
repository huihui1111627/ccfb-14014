"""Errors used across the package."""


class RecipeError(Exception):
    """Recipe definition is structurally invalid."""


class EquipmentError(Exception):
    """The plant was asked to do something it cannot do safely."""


class SensorError(EquipmentError):
    """A sensor reading is unavailable."""


class CommandRejected(Exception):
    """A command was rejected by the equipment layer (bad value, interlock)."""


class OpFailed(Exception):
    """An equipment operation failed (fault, timeout, sensor, interlock)."""


class BatchViolation(Exception):
    """A hard invariant was broken (two batches on one line, double hold, ...)."""
