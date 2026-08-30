"""Cyclomatic (McCabe) complexity for the supported Python subset.

Definition used by CodeMorph (documented in the README):

    complexity = 1
        + 1 per if / elif (each ``ast.If`` node)
        + 1 per ternary conditional expression
        + 1 per for / async for / while loop
        + 1 per except handler
        + 1 per assert
        + (n - 1) per boolean operator chain with n operands
        + 1 per comprehension ``for`` clause and per comprehension ``if`` filter
        + 1 per match ``case`` (Python 3.10+)

Nested ``def`` statements are measured as their own functions; module-level
code is reported separately.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass

from ._ast_utils import iter_function_defs


def rank_of(complexity: int) -> str:
    """Map a complexity value to a letter rank (A = lowest risk, F = highest)."""
    if complexity <= 5:
        return "A"
    if complexity <= 10:
        return "B"
    if complexity <= 20:
        return "C"
    if complexity <= 30:
        return "D"
    if complexity <= 40:
        return "E"
    return "F"


@dataclass(frozen=True)
class FunctionComplexity:
    name: str
    qualified_name: str
    lineno: int
    complexity: int
    rank: str
    is_method: bool


@dataclass
class ComplexityReport:
    functions: list[FunctionComplexity]
    module_level: int

    @property
    def total(self) -> int:
        return self.module_level + sum(fn.complexity for fn in self.functions)

    @property
    def max_function(self) -> FunctionComplexity | None:
        return max(self.functions, key=lambda fn: fn.complexity, default=None)

    @property
    def average(self) -> float:
        if not self.functions:
            return 0.0
        return round(
            sum(fn.complexity for fn in self.functions) / len(self.functions), 2
        )


class _ComplexityVisitor(ast.NodeVisitor):
    """Counts decision points inside one scope, ignoring nested ``def``s."""

    def __init__(self) -> None:
        self.decision_points = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return None  # nested functions are measured separately

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_If(self, node: ast.If) -> None:
        self.decision_points += 1
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.decision_points += 1
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self.decision_points += 1
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_While(self, node: ast.While) -> None:
        self.decision_points += 1
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.decision_points += 1
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self.decision_points += 1
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.decision_points += len(node.values) - 1
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.decision_points += 1 + len(node.ifs)
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        self.decision_points += len(node.cases)
        self.generic_visit(node)


def _decision_points(stmts: list[ast.stmt]) -> int:
    visitor = _ComplexityVisitor()
    for stmt in stmts:
        visitor.visit(stmt)
    return visitor.decision_points


class ComplexityAnalyzer:
    """Computes per-function and module-level cyclomatic complexity."""

    def analyze(self, tree: ast.Module) -> ComplexityReport:
        functions: list[FunctionComplexity] = []
        for scoped in iter_function_defs(tree):
            complexity = 1 + _decision_points(scoped.node.body)
            functions.append(
                FunctionComplexity(
                    name=scoped.node.name,
                    qualified_name=scoped.qualified_name,
                    lineno=scoped.node.lineno,
                    complexity=complexity,
                    rank=rank_of(complexity),
                    is_method=scoped.is_method,
                )
            )
        functions.sort(key=lambda fn: (fn.lineno, fn.qualified_name))
        return ComplexityReport(
            functions=functions,
            module_level=1 + _decision_points(tree.body),
        )