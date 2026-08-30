"""Source-level code metrics: lines, counts, function lengths, nesting."""
from __future__ import annotations

from dataclasses import dataclass

from .models import ModuleInfo


@dataclass
class MetricsReport:
    """Quantitative facts about one source file."""

    total_lines: int
    code_lines: int
    blank_lines: int
    comment_lines: int
    num_functions: int
    num_methods: int
    num_classes: int
    num_imports: int
    max_nesting_depth: int
    max_function_length: int
    average_function_length: float
    longest_function: str | None


def compute_metrics(source: str, module: ModuleInfo) -> MetricsReport:
    """Compute line metrics from ``source`` and counts from ``module``.

    Line categories are physical and intentionally simple:
    * blank    -- whitespace-only lines
    * comment  -- lines whose first non-whitespace character is ``#``
    * code     -- everything else. Docstrings count as code: they are runtime
      expressions (Phase 2 treats them separately).
    """
    lines = source.splitlines()
    total = len(lines)
    blank = sum(1 for line in lines if not line.strip())
    comment = sum(1 for line in lines if line.strip().startswith("#"))

    lengths = [fn.length for fn in module.functions]
    longest = max(module.functions, key=lambda fn: fn.length, default=None)

    return MetricsReport(
        total_lines=total,
        code_lines=total - blank - comment,
        blank_lines=blank,
        comment_lines=comment,
        num_functions=len(module.functions),
        num_methods=sum(1 for fn in module.functions if fn.is_method),
        num_classes=len(module.classes),
        num_imports=len(module.imports),
        max_nesting_depth=module.max_nesting_depth,
        max_function_length=max(lengths, default=0),
        average_function_length=(
            round(sum(lengths) / len(lengths), 2) if lengths else 0.0
        ),
        longest_function=longest.qualified_name if longest is not None else None,
    )