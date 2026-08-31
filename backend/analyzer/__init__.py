"""CodeMorph analysis engine — Phases 1-3."""
from .ast_analyzer import ASTAnalyzer, SourceParseError, parse_source
from .complexity import ComplexityAnalyzer, ComplexityReport, FunctionComplexity, rank_of
from .control_flow import (
    CFGEdge,
    CFGNode,
    FunctionCFG,
    build_cfgs,
    cfgs_to_dot,
    render_cfg,
)
from .data_flow import (
    DataFlowReport,
    Definition,
    FlowEdge,
    ReturnSummary,
    Use,
    build_data_flows,
    flow_findings,
    render_data_flow,
)
from .findings import (
    Category,
    Finding,
    FindingsConfig,
    FindingsEngine,
    Severity,
    severity_counts,
)
from .metrics import MetricsReport, compute_metrics
from .models import (
    CallInfo,
    ClassInfo,
    ExceptInfo,
    FunctionInfo,
    ImportInfo,
    LoopInfo,
    ModuleInfo,
    ParameterInfo,
    ReturnInfo,
)
from .renderer import render_structure
from .service import FileAnalysis, analyze_source, run_findings

__all__ = [
    "ASTAnalyzer", "SourceParseError", "parse_source",
    "ComplexityAnalyzer", "ComplexityReport", "FunctionComplexity", "rank_of",
    "CFGEdge", "CFGNode", "FunctionCFG", "build_cfgs", "cfgs_to_dot",
    "render_cfg",
    "DataFlowReport", "Definition", "FlowEdge", "ReturnSummary", "Use",
    "build_data_flows", "flow_findings", "render_data_flow",
    "Category", "Finding", "FindingsConfig", "FindingsEngine", "Severity",
    "severity_counts",
    "MetricsReport", "compute_metrics",
    "CallInfo", "ClassInfo", "ExceptInfo", "FunctionInfo", "ImportInfo",
    "LoopInfo", "ModuleInfo", "ParameterInfo", "ReturnInfo",
    "render_structure", "FileAnalysis", "analyze_source", "run_findings",
]