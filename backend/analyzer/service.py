"""Composition layer: one call runs the whole Phase-1 analysis pipeline."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .ast_analyzer import ASTAnalyzer, ModuleInfo, parse_source
from .complexity import ComplexityAnalyzer, ComplexityReport
from .metrics import MetricsReport, compute_metrics
from .renderer import render_structure


@dataclass
class FileAnalysis:
    """Everything CodeMorph knows about one source file after Phase 1."""

    filename: str
    module: ModuleInfo
    metrics: MetricsReport
    complexity: ComplexityReport
    structure: str

    def to_dict(self) -> dict:
        """JSON-ready representation (used by the API in Phase 9)."""
        return asdict(self)


def analyze_source(source: str, filename: str = "<string>") -> FileAnalysis:
    """Parse → structural model → metrics → complexity → rendered tree.

    Raises:
        SourceParseError: if ``source`` is not valid Python.
    """
    tree = parse_source(source, filename=filename)
    module = ASTAnalyzer().analyze(tree, filename=filename)
    metrics = compute_metrics(source, module)
    complexity = ComplexityAnalyzer().analyze(tree)
    return FileAnalysis(
        filename=filename,
        module=module,
        metrics=metrics,
        complexity=complexity,
        structure=render_structure(module),
    )