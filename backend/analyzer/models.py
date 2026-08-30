"""Data models for CodeMorph's structural analysis (Phase 1).

These dataclasses form the serializable contract between the analyzer, the
future migration/verification layers, and the web API. They deliberately
contain no references to ``ast`` nodes so they can be converted to JSON and
compared across pipeline stages (original vs. migrated code, Phase 6).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ImportInfo:
    """A single ``import`` / ``from ... import ...`` statement."""

    is_from: bool
    module: str | None                      # None only for ``from . import x``
    level: int                              # number of leading dots (relative)
    names: tuple[tuple[str, str | None], ...]  # (imported name, alias or None)
    lineno: int

    @property
    def bound_names(self) -> tuple[str, ...]:
        """Names actually bound in the current scope by this import."""
        if self.is_from:
            return tuple(alias or name for name, alias in self.names)
        return tuple(
            alias if alias else name.split(".")[0] for name, alias in self.names
        )

    @property
    def statement(self) -> str:
        """Human-readable reconstruction of the import statement."""
        items = ", ".join(
            name if alias is None else f"{name} as {alias}"
            for name, alias in self.names
        )
        if self.is_from:
            prefix = "." * self.level + (self.module or "")
            return f"from {prefix} import {items}"
        return f"import {items}"


@dataclass(frozen=True)
class ParameterInfo:
    """One parameter of a function signature."""

    name: str
    annotation: str | None = None
    default: str | None = None              # source text of the default expression
    kind: str = "positional"                # positional | vararg | kwonly | kwarg

    @property
    def has_default(self) -> bool:
        return self.default is not None


@dataclass(frozen=True)
class CallInfo:
    """One function-call site."""

    name: str        # best-effort dotted name, e.g. "math.fsum", or "<expr>"
    lineno: int


@dataclass(frozen=True)
class ReturnInfo:
    """One explicit ``return`` statement (implicit returns are not listed)."""

    lineno: int
    value: str | None                       # source text of the returned expression


@dataclass(frozen=True)
class LoopInfo:
    kind: str                               # "for" | "while" | "async-for"
    lineno: int


@dataclass(frozen=True)
class ExceptInfo:
    """One ``except`` handler."""

    lineno: int
    exception_types: tuple[str, ...]        # () means bare ``except:``


@dataclass
class FunctionInfo:
    """Structural facts about a single function, method, or nested function."""

    name: str
    qualified_name: str
    lineno: int
    end_lineno: int
    is_async: bool
    is_method: bool
    is_nested: bool
    decorators: tuple[str, ...]
    params: tuple[ParameterInfo, ...]
    docstring: str | None
    returns: tuple[ReturnInfo, ...]
    calls: tuple[CallInfo, ...]
    variables: tuple[str, ...]              # names bound inside, first-binding order
    num_conditions: int                     # if statements + ternaries
    loops: tuple[LoopInfo, ...]
    exception_handlers: tuple[ExceptInfo, ...]
    raises: tuple[str, ...]                 # exception type names raised
    max_nesting: int

    @property
    def length(self) -> int:
        """Physical line span of the definition."""
        return self.end_lineno - self.lineno + 1

    @property
    def num_returns(self) -> int:
        return len(self.returns)


@dataclass
class ClassInfo:
    name: str
    qualified_name: str
    lineno: int
    end_lineno: int
    bases: tuple[str, ...]
    decorators: tuple[str, ...]
    docstring: str | None
    methods: tuple[str, ...]
    class_variables: tuple[str, ...]


@dataclass
class ModuleInfo:
    """Complete structural model of one parsed source file."""

    filename: str
    docstring: str | None
    imports: list[ImportInfo]
    module_variables: list[str]
    functions: list[FunctionInfo]           # every def, flattened, source order
    classes: list[ClassInfo]
    dependencies: dict[str, list[str]]      # caller -> module-internal callees
    max_nesting_depth: int