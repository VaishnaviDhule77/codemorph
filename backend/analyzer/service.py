"""Composition layer: one call runs the whole analysis pipeline."""
from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field

from .ast_analyzer import ASTAnalyzer, ModuleInfo, parse_source
from .complexity import ComplexityAnalyzer, ComplexityReport
from .control_flow import FunctionCFG, build_cfgs
from .data_flow import DataFlowReport, build_data_flows
from .findings import Finding, FindingsConfig, FindingsEngine
from .metrics import MetricsReport, compute_metrics
from .renderer import render_structure


@dataclass
class FileAnalysis:
    """Everything CodeMorph knows about one source file after Phase 3."""

    filename: str
    module: ModuleInfo
    metrics: MetricsReport
    complexity: ComplexityReport
    structure: str
    tree: ast.Module = field(default=None, repr=False, compare=False)
    cfgs: list[FunctionCFG] = field(default_factory=list, repr=False)
    flows: list[DataFlowReport] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        """JSON-ready representation (used by the API in Phase 9).

        The AST tree and the CFG's AST references are not serialized.
        """
        return {
            "filename": self.filename,
            "module": asdict(self.module),
            "metrics": asdict(self.metrics),
            "complexity": asdict(self.complexity),
            "structure": self.structure,
            "cfgs": [cfg.to_dict() for cfg in self.cfgs],
            "data_flows": [flow.to_dict() for flow in self.flows],
        }


def analyze_source(source: str, filename: str = "<string>") -> FileAnalysis:
    """Parse → structure → metrics → complexity → CFGs → data flow.

    Raises:
        SourceParseError: if ``source`` is not valid Python.
    """
    tree = parse_source(source, filename=filename)
    module = ASTAnalyzer().analyze(tree, filename=filename)
    metrics = compute_metrics(source, module)
    complexity = ComplexityAnalyzer().analyze(tree)
    cfgs = build_cfgs(tree)
    flows = build_data_flows(cfgs, module)
    return FileAnalysis(
        filename=filename,
        module=module,
        metrics=metrics,
        complexity=complexity,
        structure=render_structure(module),
        tree=tree,
        cfgs=cfgs,
        flows=flows,
    )


def run_findings(
    analysis: FileAnalysis, config: FindingsConfig | None = None
) -> list[Finding]:
    """Run the Phase-2 static-analysis rules over one analysis."""
    return FindingsEngine(config).analyze(analysis)