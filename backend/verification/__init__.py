"""CodeMorph verification layer (Phases 5-6)."""
from .equivalence import (
    EquivalenceReport,
    EquivalenceWeights,
    SignalScore,
    compute_equivalence,
    control_flow_similarity,
    data_flow_similarity,
    render_equivalence,
    structural_similarity,
)
from .sandbox import Sandbox, SandboxConfig, SandboxRun
from .syntax_checker import SyntaxCheckResult, check_syntax
from .test_generator import GeneratedTest, generate_tests
from .test_runner import (
    CaseResult,
    ComparisonOutcome,
    ExecutionRun,
    TestRunner,
    VerificationResult,
    compare_case,
    verify_migration,
)

__all__ = [
    "EquivalenceReport", "EquivalenceWeights", "SignalScore",
    "compute_equivalence", "control_flow_similarity",
    "data_flow_similarity", "render_equivalence", "structural_similarity",
    "Sandbox", "SandboxConfig", "SandboxRun",
    "SyntaxCheckResult", "check_syntax",
    "GeneratedTest", "generate_tests",
    "CaseResult", "ComparisonOutcome", "ExecutionRun", "TestRunner",
    "VerificationResult", "compare_case", "verify_migration",
]