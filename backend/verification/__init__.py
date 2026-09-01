"""CodeMorph verification layer (Phase 5): syntax gate, sandbox, tests."""
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
    "Sandbox", "SandboxConfig", "SandboxRun",
    "SyntaxCheckResult", "check_syntax",
    "GeneratedTest", "generate_tests",
    "CaseResult", "ComparisonOutcome", "ExecutionRun", "TestRunner",
    "VerificationResult", "compare_case", "verify_migration",
]