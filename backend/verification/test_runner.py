"""Differential test execution (Phase 5).

Runs the original and the migrated source on the SAME generated cases,
each in its own sandboxed subprocess, and compares:

* return values (repr equality; object reprs containing addresses are
  compared structurally),
* raised exceptions (type equality -- messages are recorded but not
  gated, documented choice),
* captured stdout (the observable side effect at this scope).

Per-case outcome: PASS / FAIL / ERROR (ERROR = infrastructure: timeout,
module crash, missing function). This is differential testing, not proof.
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..analyzer.service import analyze_source
from .sandbox import Sandbox, SandboxConfig
from .syntax_checker import SyntaxCheckResult, check_syntax
from .test_generator import GeneratedTest, generate_tests

if TYPE_CHECKING:
    pass


# --- models -------------------------------------------------------------------


@dataclass(frozen=True)
class CaseResult:
    """Execution outcome of one case on ONE version of the code."""

    index: int
    function: str
    status: str                     # ok | error | module_error | infra_error
    value_repr: "str | None"
    exception_type: "str | None"
    exception_message: "str | None"
    stdout: str
    module_error: "str | None"

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "function": self.function,
            "status": self.status,
            "value_repr": self.value_repr,
            "exception_type": self.exception_type,
            "exception_message": self.exception_message,
            "stdout": self.stdout,
            "module_error": self.module_error,
        }


@dataclass
class ExecutionRun:
    """All cases executed against one source, plus run-level status."""

    filename: str
    cases: "list[GeneratedTest]"
    results: "list[CaseResult]"
    timed_out: bool
    run_error: "str | None"
    duration: float


@dataclass(frozen=True)
class ComparisonOutcome:
    """The differential verdict for one case."""

    case: GeneratedTest
    original: CaseResult
    migrated: CaseResult
    status: str                     # PASS | FAIL | ERROR
    detail: str


@dataclass
class VerificationResult:
    """Full Phase-5 verification report for one migration."""

    filename: str
    syntax_check: SyntaxCheckResult
    functions_tested: "list[str]"
    total: int
    passed: int
    failed: int
    errors: int
    outcomes: "list[ComparisonOutcome]"
    original_duration: float = 0.0
    migrated_duration: float = 0.0
    note: "str | None" = None

    @property
    def all_passed(self) -> bool:
        return self.total > 0 and self.failed == 0 and self.errors == 0

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "syntax_check": self.syntax_check.to_dict(),
            "functions_tested": list(self.functions_tested),
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "original_duration": round(self.original_duration, 3),
            "migrated_duration": round(self.migrated_duration, 3),
            "note": self.note,
            "outcomes": [
                {
                    "status": o.status,
                    "detail": o.detail,
                    "case": o.case.to_dict(),
                    "original": o.original.to_dict(),
                    "migrated": o.migrated.to_dict(),
                }
                for o in self.outcomes
            ],
        }


# --- child-side harness ---------------------------------------------------------


_HARNESS_TEMPLATE = r'''import base64, contextlib, io, json, sys

_MARKER = "__CODEMORPH_RESULT__"
_SOURCE = base64.b64decode("@@SOURCE@@").decode("utf-8")
_FILENAME = @@FILENAME@@
_CASES = json.loads(@@PAYLOAD@@)


def _main():
    results = []
    namespace = {"__name__": "__codemorph_module__"}
    module_error = None
    try:
        exec(compile(_SOURCE, _FILENAME, "exec"), namespace)
    except BaseException as exc:
        module_error = type(exc).__name__ + ": " + str(exc)

    def _resolve(name):
        obj = namespace
        for part in name.split("."):
            obj = obj[part]
        return obj

    for case in _CASES:
        entry = {
            "index": case["index"],
            "function": case["function"],
            "status": "ok",
            "value": None,
            "exception_type": None,
            "exception_message": None,
            "stdout": "",
            "module_error": module_error,
        }
        if module_error is not None:
            entry["status"] = "module_error"
            results.append(entry)
            continue
        try:
            function = _resolve(case["function"])
        except KeyError:
            entry["status"] = "module_error"
            entry["exception_message"] = "function not found"
            results.append(entry)
            continue
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                value = function(*case["args"], **case["kwargs"])
            entry["value"] = repr(value)
        except BaseException as exc:
            entry["status"] = "error"
            entry["exception_type"] = type(exc).__name__
            entry["exception_message"] = str(exc)
        entry["stdout"] = buffer.getvalue()
        results.append(entry)

    sys.stdout.write(_MARKER + json.dumps(results) + "\n")


_main()
'''

_TOKEN_PATTERN = re.compile(r"@@(SOURCE|FILENAME|PAYLOAD)@@")


def _build_program(source: str, filename: str, payload_json: str) -> str:
    """Render the harness with the tested source embedded (base64, so no
    token-collision or quoting issues with user source)."""
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    mapping = {
        "SOURCE": encoded,
        "FILENAME": repr(filename),
        "PAYLOAD": repr(payload_json),
    }
    return _TOKEN_PATTERN.sub(lambda match: mapping[match.group(1)], _HARNESS_TEMPLATE)


# --- execution -------------------------------------------------------------------


class TestRunner:
    """Executes generated cases against one source inside the sandbox."""

    def __init__(self, sandbox_config: "SandboxConfig | None" = None) -> None:
        self.sandbox = Sandbox(
            sandbox_config if sandbox_config is not None else SandboxConfig.from_env()
        )

    def execute(
        self, source: str, cases: "list[GeneratedTest]", filename: str = "<string>"
    ) -> ExecutionRun:
        if not cases:
            return ExecutionRun(filename, [], [], False, None, 0.0)
        payload = json.dumps([
            {
                "index": index,
                "function": case.function,
                "args": list(case.args),
                "kwargs": dict(case.kwargs),
            }
            for index, case in enumerate(cases)
        ])
        program = _build_program(source, filename, payload)
        start = time.perf_counter()
        run = self.sandbox.run_program(program)
        duration = time.perf_counter() - start
        return ExecutionRun(
            filename=filename,
            cases=list(cases),
            results=self._convert(run, cases),
            timed_out=run.timed_out,
            run_error=run.error if not run.ok else None,
            duration=duration,
        )

    @staticmethod
    def _convert(run, cases: "list[GeneratedTest]") -> "list[CaseResult]":
        if (
            not run.ok
            or not isinstance(run.payload, list)
            or len(run.payload) != len(cases)
        ):
            reason = run.error or "result payload malformed"
            return [
                CaseResult(
                    index=index,
                    function=case.function,
                    status="infra_error",
                    value_repr=None,
                    exception_type=None,
                    exception_message=None,
                    stdout="",
                    module_error=reason,
                )
                for index, case in enumerate(cases)
            ]
        entries = {entry["index"]: entry for entry in run.payload}
        results: "list[CaseResult]" = []
        for index, case in enumerate(cases):
            entry = entries.get(index)
            if entry is None:
                results.append(
                    CaseResult(
                        index=index,
                        function=case.function,
                        status="infra_error",
                        value_repr=None,
                        exception_type=None,
                        exception_message=None,
                        stdout="",
                        module_error=f"missing result for case {index}",
                    )
                )
                continue
            results.append(
                CaseResult(
                    index=index,
                    function=entry["function"],
                    status=entry["status"],
                    value_repr=entry["value"],
                    exception_type=entry["exception_type"],
                    exception_message=entry["exception_message"],
                    stdout=entry["stdout"],
                    module_error=entry["module_error"],
                )
            )
        return results


# --- comparison --------------------------------------------------------------------


def _values_equivalent(left: str, right: str) -> bool:
    """repr equality, with a structural fallback for address-bearing reprs.

    ``<object object at 0x7f...>`` differs between processes even for
    equivalent values; compare everything before the address instead.
    """
    if left == right:
        return True
    if " at 0x" in left and " at 0x" in right:
        return left.split(" at 0x")[0] == right.split(" at 0x")[0]
    return False


def compare_case(
    case: GeneratedTest, original: CaseResult, migrated: CaseResult
) -> ComparisonOutcome:
    """Differential verdict for one case (PASS / FAIL / ERROR)."""
    for status_name in ("infra_error", "module_error"):
        for side, result in (("original", original), ("migrated", migrated)):
            if result.status == status_name:
                verb = "could not execute" if status_name == "infra_error" \
                    else "failed to load module"
                return ComparisonOutcome(
                    case, original, migrated, "ERROR",
                    f"{side} side {verb}: {result.module_error}",
                )
    if original.status == "ok" and migrated.status == "ok":
        if not _values_equivalent(original.value_repr, migrated.value_repr):
            return ComparisonOutcome(
                case, original, migrated, "FAIL",
                f"value mismatch: original={original.value_repr} "
                f"migrated={migrated.value_repr}",
            )
        if original.stdout != migrated.stdout:
            return ComparisonOutcome(
                case, original, migrated, "FAIL",
                f"stdout mismatch: original={original.stdout!r} "
                f"migrated={migrated.stdout!r}",
            )
        return ComparisonOutcome(
            case, original, migrated, "PASS", f"ok: {original.value_repr}"
        )
    if original.status == "error" and migrated.status == "error":
        if original.exception_type != migrated.exception_type:
            return ComparisonOutcome(
                case, original, migrated, "FAIL",
                f"exception mismatch: original raised {original.exception_type}, "
                f"migrated raised {migrated.exception_type}",
            )
        return ComparisonOutcome(
            case, original, migrated, "PASS",
            f"both raise {original.exception_type}",
        )
    if original.status == "ok":
        return ComparisonOutcome(
            case, original, migrated, "FAIL",
            f"original returned {original.value_repr}, "
            f"migrated raised {migrated.exception_type}",
        )
    return ComparisonOutcome(
        case, original, migrated, "FAIL",
        f"original raised {original.exception_type}, "
        f"migrated returned {migrated.value_repr}",
    )


# --- composition ---------------------------------------------------------------------


def verify_migration(
    original_source: str,
    migrated_source: str,
    filename: str = "<string>",
    sandbox_config: "SandboxConfig | None" = None,
) -> VerificationResult:
    """Syntax gate -> generated tests -> sandboxed differential execution.

    Raises:
        SourceParseError: if ``original_source`` is not valid Python (the
            original is assumed pre-analyzed; only the migrated source is
            validated leniently here).
    """
    migrated_check = check_syntax(migrated_source, filename=filename)
    if not migrated_check.valid:
        return VerificationResult(
            filename=filename,
            syntax_check=migrated_check,
            functions_tested=[],
            total=0,
            passed=0,
            failed=0,
            errors=0,
            outcomes=[],
            note="migrated source failed syntax validation; no tests executed",
        )

    analysis = analyze_source(original_source, filename=filename)
    cases = generate_tests(analysis)
    if not cases:
        return VerificationResult(
            filename=filename,
            syntax_check=migrated_check,
            functions_tested=[],
            total=0,
            passed=0,
            failed=0,
            errors=0,
            outcomes=[],
            note="no testable functions found; nothing to compare",
        )

    runner = TestRunner(sandbox_config)
    original_run = runner.execute(original_source, cases, filename)
    migrated_run = runner.execute(migrated_source, cases, filename)

    outcomes = [
        compare_case(case, original_result, migrated_result)
        for case, original_result, migrated_result in zip(
            cases, original_run.results, migrated_run.results
        )
    ]
    passed = sum(1 for outcome in outcomes if outcome.status == "PASS")
    failed = sum(1 for outcome in outcomes if outcome.status == "FAIL")
    errors = sum(1 for outcome in outcomes if outcome.status == "ERROR")
    functions_tested = list(dict.fromkeys(case.function for case in cases))
    return VerificationResult(
        filename=filename,
        syntax_check=migrated_check,
        functions_tested=functions_tested,
        total=len(outcomes),
        passed=passed,
        failed=failed,
        errors=errors,
        outcomes=outcomes,
        original_duration=original_run.duration,
        migrated_duration=migrated_run.duration,
    )