"""Recipe model (ISA-88 style, four levels).

procedure
  └─ unit procedure
       └─ operation
            └─ phase           <- the only level bound to real equipment

Every level above the phase is a *step graph* made of:

* leaf nodes - the next lower level (UnitProcedure / Operation / Phase)
* :class:`Parallel` - run several branches concurrently
* :class:`Choice`   - pick the first branch whose condition is true
* :class:`Transition` - wait for a condition before continuing
* :class:`Acquire` / :class:`Release` - shared-resource handling

A plain list of nodes is a sequence.

Phases carry the standard state machine (see ``states.py``) plus *written*
hold / restart / stop / abort sequences.  The propagation attribute decides
what happens to siblings when this phase faults into hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Sequence, Tuple

from .errors import RecipeError

PROPAGATION_SCOPES = ("self", "operation", "unit_procedure", "batch")


# --------------------------------------------------------------------------
# conditions
# --------------------------------------------------------------------------
class Condition:
    """A predicate over the runtime context, plus a human-readable label."""

    label: str = "condition"

    def ready(self, ctx) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> str:
        return self.label


@dataclass
class Always(Condition):
    label: str = "always"

    def ready(self, ctx) -> bool:
        return True


@dataclass
class After(Condition):
    """True once the containing step graph has been active for N seconds."""

    seconds: float
    label: str = ""

    def __post_init__(self):
        if not self.label:
            self.label = f"after {self.seconds:g}s"

    def ready(self, ctx) -> bool:
        return ctx.active_elapsed() >= self.seconds


@dataclass
class When(Condition):
    fn: Callable[[object], bool]
    label: str = "when"

    def ready(self, ctx) -> bool:
        return bool(self.fn(ctx))

    def describe(self) -> str:
        return self.label


@dataclass
class Param(Condition):
    key: str
    label: str = ""

    def __post_init__(self):
        if not self.label:
            self.label = f"param {self.key}"

    def ready(self, ctx) -> bool:
        return bool(ctx.params.get(self.key))


# --------------------------------------------------------------------------
# step graph nodes
# --------------------------------------------------------------------------
class Node:
    name: str = ""
    kind: str = "node"


@dataclass
class Transition(Node):
    condition: Condition
    name: str = ""
    kind: str = "transition"

    def __post_init__(self):
        if not self.name:
            self.name = self.condition.describe()


@dataclass
class Parallel(Node):
    branches: List[List[Node]]
    name: str = ""
    kind: str = "parallel"

    def __post_init__(self):
        if not self.name:
            self.name = "parallel"


@dataclass
class Choice(Node):
    options: List[Tuple[Condition, List[Node]]]
    name: str = ""
    kind: str = "choice"

    def __post_init__(self):
        if not self.name:
            self.name = "choice"


@dataclass
class Acquire(Node):
    resources: List[str]
    priority: int = 5
    name: str = ""
    kind: str = "acquire"

    def __post_init__(self):
        if not self.name:
            self.name = "acquire " + ", ".join(self.resources)


@dataclass
class Release(Node):
    resources: List[str]
    name: str = ""
    kind: str = "release"

    def __post_init__(self):
        if not self.name:
            self.name = "release " + ", ".join(self.resources)


# --------------------------------------------------------------------------
# elements
# --------------------------------------------------------------------------
class Element:
    name: str
    kind: str
    steps: List[Node]

    def path_join(self, parent: str) -> str:
        return f"{parent}.{self.name}" if parent else self.name


@dataclass
class Phase(Element):
    name: str
    equipment: str
    ops: List[object]
    on_hold: List[object] = field(default_factory=list)
    on_restart: List[object] = field(default_factory=list)
    on_stop: List[object] = field(default_factory=list)
    on_abort: List[object] = field(default_factory=list)
    propagation: str = "self"
    description: str = ""
    kind: str = "phase"
    steps: List[Node] = field(default_factory=list)

    def __post_init__(self):
        if self.propagation not in PROPAGATION_SCOPES:
            raise RecipeError(
                f"phase {self.name}: propagation must be one of "
                f"{PROPAGATION_SCOPES}, got {self.propagation!r}"
            )
        if not self.ops:
            raise RecipeError(f"phase {self.name}: no operations")


@dataclass
class Operation(Element):
    name: str
    steps: List[Node]
    description: str = ""
    kind: str = "operation"


@dataclass
class UnitProcedure(Element):
    name: str
    steps: List[Node]
    description: str = ""
    kind: str = "unit_procedure"


@dataclass
class Procedure(Element):
    name: str
    steps: List[Node]
    version: str = "1.0"
    kind: str = "procedure"


@dataclass
class Recipe:
    recipe_id: str
    name: str
    procedure: Procedure
    parameters: Dict[str, object] = field(default_factory=dict)
    batch_size_kg: float = 800.0
    product: str = ""
    version: str = "1.0"
    notes: str = ""

    # -- validation / indexing -------------------------------------------
    def __post_init__(self):
        if not self.recipe_id:
            raise RecipeError("recipe_id is required")
        #: full path -> phase (paths are unique by construction)
        self._phases: Dict[str, Phase] = {}
        #: short name -> list of full paths (names may repeat in branches)
        self._by_name: Dict[str, List[str]] = {}
        self._walk(self.procedure, "")

    def _walk(self, el, parent: str) -> None:
        path = el.path_join(parent)
        if el.kind == "phase":
            if path in self._phases:
                raise RecipeError(f"duplicate phase path {path!r} in recipe")
            self._phases[path] = el
            self._by_name.setdefault(el.name, []).append(path)
            return
        names = set()
        for node in _iter_leaves(el.steps):
            if node.kind in ("unit_procedure", "operation", "phase"):
                if node.name in names:
                    raise RecipeError(
                        f"{el.name}: duplicate child name {node.name!r}")
                names.add(node.name)
                self._walk(node, path)

    @property
    def phases(self) -> Dict[str, Phase]:
        """full path -> phase definition"""
        return dict(self._phases)

    def phase(self, name_or_path: str) -> Phase:
        if name_or_path in self._phases:
            return self._phases[name_or_path]
        paths = self._by_name.get(name_or_path, [])
        if len(paths) == 1:
            return self._phases[paths[0]]
        if not paths:
            raise KeyError(f"no phase {name_or_path!r} in recipe")
        raise KeyError(f"ambiguous phase {name_or_path!r}: {paths}")

    def resolve_path(self, name: str) -> str:
        paths = [p for p in self._phases if p == name or p.endswith("." + name)]
        if len(paths) == 1:
            return paths[0]
        raise KeyError(f"cannot resolve element {name!r}")


def _iter_leaves(steps: Sequence[Node]):
    """Yield element leaves of a step graph (ignores control nodes)."""
    for node in steps:
        if node.kind == "parallel":
            for branch in node.branches:
                yield from _iter_leaves(branch)
        elif node.kind == "choice":
            for _cond, branch in node.options:
                yield from _iter_leaves(branch)
        elif node.kind in ("unit_procedure", "operation", "phase"):
            yield node
        # transition / acquire / release are not elements


def walk_tree(recipe: Recipe) -> List[Element]:
    """All elements in document order (procedure, UP, operation, phase)."""
    out = []

    def rec(el):
        out.append(el)
        if el.kind != "phase":
            for leaf in _iter_leaves(el.steps):
                rec(leaf)

    rec(recipe.procedure)
    return out


def iter_lower(el):
    """Immediate lower-level element leaves (deduplicated in graph order)."""
    seen = set()
    out = []
    for leaf in _iter_leaves(el.steps):
        if leaf.name in seen:
            continue
        seen.add(leaf.name)
        out.append(leaf)
    return out
