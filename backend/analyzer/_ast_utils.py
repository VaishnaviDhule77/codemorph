"""Internal AST helpers shared by the analyzer modules.

These helpers implement the *scope-aware traversal* that guarantees the
structural analyzer, the complexity calculator, and (in later phases) the
control/data-flow modules all agree on how functions are discovered, named,
and scoped. One traversal contract, zero drift between analyses.
"""
from __future__ import annotations

import ast
from typing import Iterator, NamedTuple


class ScopedFunction(NamedTuple):
    """A function definition discovered during a scope-aware traversal."""

    node: ast.FunctionDef | ast.AsyncFunctionDef
    qualified_name: str
    is_method: bool
    is_nested: bool


def iter_function_defs(tree: ast.Module) -> Iterator[ScopedFunction]:
    """Yield every ``def``/``async def`` in the module, in source order.

    Qualified names use dots: ``Calculator.add`` for methods and
    ``outer.inner`` for nested functions. Lambdas are *not* yielded; their
    bodies are attributed to the enclosing function.
    """
    yield from _walk_scope(tree, prefix="", parent_kind="module")


def _walk_scope(node: ast.AST, prefix: str, parent_kind: str) -> Iterator[ScopedFunction]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualified = prefix + child.name
            yield ScopedFunction(
                node=child,
                qualified_name=qualified,
                is_method=parent_kind == "class",
                is_nested=parent_kind == "function",
            )
            yield from _walk_scope(
                child, prefix=qualified + ".", parent_kind="function"
            )
        elif isinstance(child, ast.ClassDef):
            qualified = prefix + child.name
            yield from _walk_scope(child, prefix=qualified + ".", parent_kind="class")
        else:
            yield from _walk_scope(child, prefix=prefix, parent_kind=parent_kind)


def iter_class_defs(tree: ast.Module) -> Iterator[tuple[ast.ClassDef, str]]:
    """Yield every class definition with its dotted qualified name."""
    yield from _walk_classes(tree, prefix="")


def _walk_classes(node: ast.AST, prefix: str) -> Iterator[tuple[ast.ClassDef, str]]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            qualified = prefix + child.name
            yield child, qualified
            yield from _walk_classes(child, prefix=qualified + ".")
        else:
            yield from _walk_classes(child, prefix=prefix)


# --- nesting depth -----------------------------------------------------------

_FOR = (ast.For, ast.AsyncFor)
_TRY = (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())
_WITH = (ast.With, ast.AsyncWith)
_MATCH = getattr(ast, "Match", None)  # Python 3.10+


def max_nesting_depth(node: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Maximum control-flow nesting depth inside one scope.

    Rules (documented in the README):
    * each ``if``/``for``/``while``/``try``/``with``/``match`` body adds a level
    * ``elif``/``else`` chains stay at the depth of the original ``if``
    * entering a function or class body restarts the count at 0
    """
    best, _ = _scan_blocks(node.body, depth=0, threshold=None)
    return best


def first_excessive_nesting_line(
    node: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef, threshold: int
) -> int | None:
    """Line of the first statement nested deeper than ``threshold``.

    Returns ``None`` when nothing exceeds the threshold. Uses the exact same
    traversal as :func:`max_nesting_depth`, so the two can never disagree.
    """
    _, first = _scan_blocks(node.body, depth=0, threshold=threshold)
    return first


def _scan_blocks(
    stmts: list[ast.stmt], depth: int, threshold: int | None
) -> tuple[int, int | None]:
    """Return ``(max_depth, first_line_exceeding_threshold)`` for a block."""
    best = depth
    first: int | None = None
    if threshold is not None and depth > threshold and stmts:
        first = stmts[0].lineno  # this statement sits deeper than allowed
    for stmt in stmts:
        stmt_best, stmt_first = _scan_stmt(stmt, depth, threshold)
        if stmt_best > best:
            best = stmt_best
        if first is None:
            first = stmt_first
    return best, first


def _scan_stmt(
    stmt: ast.stmt, depth: int, threshold: int | None
) -> tuple[int, int | None]:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return _scan_blocks(stmt.body, depth=0, threshold=threshold)  # new scope

    if isinstance(stmt, ast.If):
        best, first = _scan_blocks(stmt.body, depth + 1, threshold)
        if stmt.orelse:
            if _is_elif(stmt.orelse):
                # elif continues at the same depth; else adds a level
                b, f = _scan_stmt(stmt.orelse[0], depth, threshold)
            else:
                b, f = _scan_blocks(stmt.orelse, depth + 1, threshold)
            best = max(best, b)
            first = first if first is not None else f
        return best, first

    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
        best, first = _scan_blocks(stmt.body, depth + 1, threshold)
        if stmt.orelse:
            b, f = _scan_blocks(stmt.orelse, depth + 1, threshold)
            best = max(best, b)
            first = first if first is not None else f
        return best, first

    if isinstance(stmt, _TRY):
        best, first = _scan_blocks(stmt.body, depth + 1, threshold)
        for part in (stmt.orelse, stmt.finalbody):
            b, f = _scan_blocks(part, depth + 1, threshold)
            best = max(best, b)
            first = first if first is not None else f
        for handler in stmt.handlers:
            b, f = _scan_blocks(handler.body, depth + 1, threshold)
            best = max(best, b)
            first = first if first is not None else f
        return best, first

    if isinstance(stmt, _WITH):
        return _scan_blocks(stmt.body, depth + 1, threshold)

    if _MATCH is not None and isinstance(stmt, _MATCH):
        best, first = depth + 1, None
        for case in stmt.cases:
            b, f = _scan_blocks(case.body, depth + 1, threshold)
            best = max(best, b)
            first = first if first is not None else f
        return best, first

    return depth, None


def _is_elif(orelse: list[ast.stmt]) -> bool:
    """True when an ``if`` orelse branch is a single ``elif`` continuation."""
    return len(orelse) == 1 and isinstance(orelse[0], ast.If)