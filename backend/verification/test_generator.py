"""Test-input generation from function signatures (Phase 5).

For every *testable* module-level function the generator derives a small,
deterministic set of cases:

* ``normal``    -- every parameter at its typical value
* ``boundary``  -- every parameter at an edge value (0, "a", ["x"], ...)
* ``empty``     -- every parameter at its empty value (deduplicated
                   against ``boundary`` when identical)
* ``invalid``   -- the first parameter at a wrong-typed value; this tests
                   *exception parity*, not correctness
* ``default``   -- defaulted parameters omitted (only when defaults exist)
* ``no-args``   -- zero-parameter functions

Value types come from annotations when they are plain builtins
(``int/float/str/bool/list/dict``), otherwise from documented parameter
NAME heuristics; untyped unknowns fall back to ``int``. This is a
heuristic, not type inference -- documented as such.

Skipped (documented): methods (need instance construction), nested
functions, ``async`` functions (need an event loop), ``_``-prefixed
names, and ``main`` (assumed entry point with side effects).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..analyzer.models import FunctionInfo
    from ..analyzer.service import FileAnalysis


@dataclass(frozen=True)
class GeneratedTest:
    """One generated call: function, category label, literal arguments."""

    function: str
    description: str
    args: tuple = ()
    kwargs: tuple = ()   # tuple of (name, value) pairs

    def to_dict(self) -> dict:
        return {
            "function": self.function,
            "description": self.description,
            "args": list(self.args),
            "kwargs": dict(self.kwargs),
        }


_ANNOTATION_TYPES = {"int", "float", "str", "bool", "list", "dict"}

_INT_NAMES = {"n", "count", "num", "size", "index", "idx", "i", "j", "k",
              "x", "y", "total", "amount", "precision", "limit", "length"}
_FLOAT_NAMES = {"rate", "ratio", "price", "factor", "weight", "radius",
                "value", "f", "r"}
_STR_NAMES = {"name", "s", "text", "key", "path", "label", "msg",
              "message", "word"}
_BOOL_NAMES = {"flag", "verbose", "enabled", "debug"}
_LIST_NAMES = {"items", "values", "xs", "rows", "data", "seq"}

_VALUES: "dict[str, dict[str, object]]" = {
    "int": {"normal": 5, "boundary": 0, "empty": 0, "invalid": "x"},
    "float": {"normal": 2.5, "boundary": 0.0, "empty": 0.0, "invalid": "x"},
    "str": {"normal": "hello", "boundary": "a", "empty": "", "invalid": None},
    "bool": {"normal": True, "boundary": False, "empty": False, "invalid": "x"},
    "list": {"normal": ["a", "bb", "cccc"], "boundary": ["x"], "empty": [],
             "invalid": None},
    "dict": {"normal": {"k": 1}, "boundary": {}, "empty": {}, "invalid": None},
}

_CATEGORIES = ("normal", "boundary", "empty", "invalid")


def _freeze(value):
    """Hashable form of a generated argument value.

    List- and dict-typed parameters put lists and dicts into ``args`` /
    ``kwargs``; those are unhashable, so set membership on the raw
    ``(args, kwargs)`` tuple raises ``TypeError: unhashable type: 'list'``.
    Freezing converts lists and tuples element-wise into tuples and dicts
    into sorted tuples of items, preserving value equality for the
    case-deduplication below.
    """
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    return value


def _type_key(param) -> str:
    """Annotation (plain builtin) -> name heuristic -> int fallback."""
    annotation = (param.annotation or "").strip()
    if annotation in _ANNOTATION_TYPES:
        return annotation
    name = param.name.lower()
    if name in _INT_NAMES:
        return "int"
    if name in _FLOAT_NAMES:
        return "float"
    if name in _STR_NAMES:
        return "str"
    if name in _BOOL_NAMES:
        return "bool"
    if name in _LIST_NAMES:
        return "list"
    return "int"


def _testable(fn: "FunctionInfo") -> bool:
    if fn.is_method or fn.is_nested or fn.is_async:
        return False
    if fn.name.startswith("_") or fn.name == "main":
        return False
    return True


def generate_tests(analysis: "FileAnalysis") -> "list[GeneratedTest]":
    """Deterministic test cases for every testable module-level function."""
    cases: "list[GeneratedTest]" = []
    for fn in analysis.module.functions:
        if _testable(fn):
            cases.extend(_cases_for_function(fn))
    return cases


def _cases_for_function(fn: "FunctionInfo") -> "list[GeneratedTest]":
    # vararg/kwarg parameters are never filled: calling without them is valid.
    fillable = [p for p in fn.params if p.kind in ("positional", "kwonly")]
    if not fillable:
        return [GeneratedTest(fn.qualified_name, "no-args", (), ())]

    cases: "list[GeneratedTest]" = []
    for category in _CATEGORIES:
        args: list = []
        kwargs: list = []
        for position, param in enumerate(fillable):
            if category == "invalid":
                # only the FIRST parameter is invalid; the rest stay normal
                key = "invalid" if position == 0 else "normal"
            else:
                key = category
            value = _VALUES[_type_key(param)][key]
            if param.kind == "positional":
                args.append(value)
            else:
                kwargs.append((param.name, value))
        cases.append(
            GeneratedTest(fn.qualified_name, category, tuple(args), tuple(kwargs))
        )

    if any(param.has_default for param in fillable):
        args = []
        kwargs = []
        for param in fillable:
            if param.has_default:
                continue
            value = _VALUES[_type_key(param)]["normal"]
            if param.kind == "positional":
                args.append(value)
            else:
                kwargs.append((param.name, value))
        cases.append(
            GeneratedTest(fn.qualified_name, "default", tuple(args), tuple(kwargs))
        )

    seen: set = set()
    unique: "list[GeneratedTest]" = []
    for case in cases:
        # Frozen keys: raw (args, kwargs) tuples can contain lists/dicts,
        # which are unhashable and cannot participate in set membership.
        key = (_freeze(case.args), _freeze(case.kwargs))
        if key in seen:
            continue
        seen.add(key)
        unique.append(case)
    return unique