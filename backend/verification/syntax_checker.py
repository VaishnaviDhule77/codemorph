"""Syntax validation (Phase 5): the first gate for generated code.

Uses ``compile()`` rather than ``ast.parse()`` deliberately: compilation
performs post-parse checks the AST builder skips (e.g. misplaced
``__future__`` imports), so generated code that parses but would fail at
import time is still caught here.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SyntaxCheckResult:
    """Structured outcome of compiling one source string."""

    valid: bool
    filename: str
    error_line: "int | None"
    error_offset: "int | None"
    error_message: "str | None"

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "filename": self.filename,
            "error_line": self.error_line,
            "error_offset": self.error_offset,
            "error_message": self.error_message,
        }


def check_syntax(source: str, filename: str = "<string>") -> SyntaxCheckResult:
    """Compile ``source``; never raises on bad code, returns a result."""
    try:
        compile(source, filename, "exec")
    except SyntaxError as exc:
        return SyntaxCheckResult(
            valid=False,
            filename=filename,
            error_line=exc.lineno,
            error_offset=exc.offset,
            error_message=exc.msg,
        )
    return SyntaxCheckResult(
        valid=True,
        filename=filename,
        error_line=None,
        error_offset=None,
        error_message=None,
    )