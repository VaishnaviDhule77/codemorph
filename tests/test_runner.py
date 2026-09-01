"""Tests for backend.verification.test_runner (execution + comparison)."""
from __future__ import annotations

import json

from backend.analyzer.__main__ import main
from backend.migration import TransformationEngine
from backend.verification import (
    GeneratedTest,
    SandboxConfig,
    verify_migration,
)

# Aliased so pytest does not try to collect the imported class as a test
# class (names matching Test* trigger collection attempts and a warning).
from backend.verification.test_runner import TestRunner as Runner


def case(function: str, description: str, args: list) -> GeneratedTest:
    return GeneratedTest(
        function=function, description=description, args=tuple(args)
    )


def run_one(source: str, cases: list, timeout: float = 20.0):
    return Runner(SandboxConfig(timeout=timeout)).execute(
        source, cases, filename="inline.py"
    )


# -- single-side execution ------------------------------------------------------


def test_execute_simple_function():
    run = run_one("def add(a, b):\n    return a + b\n", [case("add", "normal", [5, 3])])
    assert run.timed_out is False and run.run_error is None
    (result,) = run.results
    assert result.status == "ok"
    assert result.value_repr == "8"
    assert result.function == "add"
    assert result.stdout == ""


def test_execute_captures_exceptions():
    run = run_one(
        "def boom(x):\n    raise ValueError('bad ' + str(x))\n",
        [case("boom", "normal", [1])],
    )
    (result,) = run.results
    assert result.status == "error"
    assert result.exception_type == "ValueError"
    assert result.exception_message == "bad 1"


def test_execute_captures_stdout():
    run = run_one(
        "def show(x):\n    print('got', x)\n    return x\n",
        [case("show", "normal", [5])],
    )
    (result,) = run.results
    assert result.status == "ok"
    assert result.value_repr == "5"
    assert result.stdout == "got 5\n"


def test_module_level_failure_marks_all_cases():
    source = 'raise RuntimeError("boom")\n\n\ndef f(x):\n    return x\n'
    run = run_one(source, [case("f", "normal", [5])])
    (result,) = run.results
    assert result.status == "module_error"
    assert result.module_error == "RuntimeError: boom"


def test_missing_function_is_module_error():
    run = run_one("def g(x):\n    return x\n", [case("missing", "normal", [1])])
    (result,) = run.results
    assert result.status == "module_error"
    assert result.exception_message == "function not found"


def test_timeout_produces_infra_error():
    source = "def spin(x):\n    while True:\n        pass\n    return x\n"
    run = run_one(source, [case("spin", "normal", [5])], timeout=2)
    assert run.timed_out is True
    assert run.run_error and "timeout" in run.run_error.lower()
    (result,) = run.results
    assert result.status == "infra_error"
    assert result.module_error and "timeout" in result.module_error.lower()


# -- differential verification -------------------------------------------------------


def test_verification_identical_source_all_pass(calculator_source):
    result = verify_migration(
        calculator_source, calculator_source,
        filename="calculator.py",
        sandbox_config=SandboxConfig(timeout=20),
    )
    assert result.syntax_check.valid
    assert result.functions_tested == [
        "validate_amount", "calculate_tax", "calculate_total",
    ]
    assert result.total == 12
    assert result.passed == 12 and result.failed == 0 and result.errors == 0
    assert result.all_passed
    by_key = {(o.case.function, o.case.description): o for o in result.outcomes}
    assert by_key[("calculate_total", "normal")].original.value_repr == "4.0"
    assert by_key[("calculate_tax", "default")].original.value_repr == "0.5"
    assert "both raise TypeError" in by_key[("validate_amount", "invalid")].detail


def test_verification_of_deterministic_migration():
    legacy = (
        "def greet(name: str):\n"
        "    return 'Hello %s!' % (name,)\n"
        "\n"
        "\n"
        "def bump(x):\n"
        "    x = x + 1\n"
        "    x = x + 1\n"
        "    x = x + 1\n"
        "    return x\n"
    )
    migration = TransformationEngine().transform_source(legacy, filename="legacy.py")
    assert migration.applied
    assert 'f"Hello {name}!"' in migration.migrated_source
    assert "x += 3" in migration.migrated_source

    result = verify_migration(
        legacy, migration.migrated_source,
        filename="legacy.py",
        sandbox_config=SandboxConfig(timeout=20),
    )
    # greet: normal/boundary/empty/invalid (invalid None -> "Hello None!"
    # via str() AND via __format__ -> equal). bump: 3 cases. All 7 pass.
    assert result.total == 7
    assert result.passed == 7 and result.failed == 0 and result.errors == 0


def test_python2_ism_migration_flags_original_crash():
    """Differential testing cannot "pass" a repair of non-runnable code.

    ``d.has_key(k)`` is Python 2: the ORIGINAL crashes with AttributeError
    on Python 3 while the migration returns a value. Every case FAILS --
    correctly reporting a divergence, even though the migration *fixed*
    the code. Interpreting FAIL outcomes therefore requires the Phase-4
    transformation registry alongside the test results; this is exactly
    the static-analysis-plus-testing combination CodeMorph researches.
    """
    legacy = "def has_key_check(d: dict, k: str):\n    return d.has_key(k)\n"
    migration = TransformationEngine().transform_source(legacy, filename="hk.py")
    assert migration.applied

    result = verify_migration(
        legacy, migration.migrated_source,
        filename="hk.py",
        sandbox_config=SandboxConfig(timeout=20),
    )
    # All four categories survive deduplication: 'k' is str-typed, so
    # boundary ({}, "a") and empty ({}, "") DIFFER -- dedup only collapses
    # cases when every parameter's boundary and empty values coincide.
    assert result.total == 4
    assert result.passed == 0 and result.failed == 4 and result.errors == 0
    normal = next(
        o for o in result.outcomes if o.case.description == "normal"
    )
    assert normal.status == "FAIL"
    assert "AttributeError" in normal.detail
    assert "migrated returned False" in normal.detail
    # invalid case (d=None): original raises AttributeError, the migrated
    # 'k in None' raises TypeError -- exception parity catches it too.
    invalid = next(
        o for o in result.outcomes if o.case.description == "invalid"
    )
    assert invalid.status == "FAIL"
    assert "exception mismatch" in invalid.detail


def test_value_mismatch_fails():
    original = "def f(x):\n    return x + 1\n"
    migrated = "def f(x):\n    return x + 2\n"
    result = verify_migration(original, migrated, filename="inline.py")
    assert result.total == 3
    # invalid case: both raise TypeError -> PASS; the other two FAIL
    assert result.passed == 1 and result.failed == 2
    outcome = next(
        o for o in result.outcomes if o.case.description == "normal"
    )
    assert outcome.status == "FAIL"
    assert "6" in outcome.detail and "7" in outcome.detail


def test_exception_parity_and_mismatch():
    original = 'def f(x):\n    raise ValueError("nope")\n'
    same = verify_migration(original, original, filename="inline.py")
    assert same.total == 3 and same.passed == 3 and same.failed == 0
    assert all("both raise ValueError" in o.detail for o in same.outcomes)

    changed = 'def f(x):\n    raise TypeError("nope")\n'
    diff = verify_migration(original, changed, filename="inline.py")
    assert diff.passed == 0 and diff.failed == 3
    assert "exception mismatch" in diff.outcomes[0].detail
    assert "ValueError" in diff.outcomes[0].detail
    assert "TypeError" in diff.outcomes[0].detail


def test_return_vs_raise():
    original = "def f(x):\n    return x + 1\n"
    migrated = 'def f(x):\n    raise RuntimeError("boom")\n'
    result = verify_migration(original, migrated, filename="inline.py")
    assert result.passed == 0 and result.failed == 3
    outcome = result.outcomes[0]
    assert outcome.status == "FAIL"
    assert "returned 6" in outcome.detail
    assert "RuntimeError" in outcome.detail


def test_stdout_divergence_fails():
    original = 'def show(x):\n    print("v1")\n    return x\n'
    migrated = 'def show(x):\n    print("v2")\n    return x\n'
    result = verify_migration(original, migrated, filename="inline.py")
    # values match; only the observable side effect differs
    assert result.passed == 0 and result.failed == 3
    assert all("stdout" in o.detail for o in result.outcomes)


def test_unstable_reprs_compared_by_structure():
    # repr(object()) embeds a memory address that differs across processes;
    # the comparator falls back to comparing everything before " at 0x".
    source = "def make(x):\n    return object()\n"
    result = verify_migration(source, source, filename="inline.py")
    assert result.passed == 3
    outcome = result.outcomes[0]
    assert " at 0x" in outcome.original.value_repr
    assert outcome.status == "PASS"


def test_invalid_migrated_syntax_short_circuits():
    result = verify_migration(
        "def f(x):\n    return x\n", "def broken(:\n", filename="inline.py"
    )
    assert result.syntax_check.valid is False
    assert result.syntax_check.error_line == 1
    assert result.total == 0
    assert result.outcomes == []
    assert result.note and "syntax" in result.note.lower()


def test_no_testable_functions():
    source = "def main():\n    return 1\n"
    result = verify_migration(source, source, filename="inline.py")
    assert result.total == 0
    assert result.functions_tested == []
    assert result.note and "no testable functions" in result.note


def test_verification_result_serialization():
    source = "def f(x):\n    return x\n"
    result = verify_migration(source, source, filename="inline.py")
    data = json.loads(json.dumps(result.to_dict()))
    assert data["total"] == 3
    assert data["passed"] == 3
    assert data["syntax_check"]["valid"] is True
    assert data["outcomes"][0]["case"]["function"] == "f"
    assert data["outcomes"][0]["original"]["status"] == "ok"
    assert data["outcomes"][0]["original"]["value_repr"] == "5"


# -- CLI ------------------------------------------------------------------------------------


def test_cli_verify(tmp_path, capsys):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def greet(name: str):\n    return 'Hello %s!' % (name,)\n",
        encoding="utf-8",
    )
    assert main([str(sample), "--verify"]) == 0
    out = capsys.readouterr().out
    assert "1 transformation(s), applied" in out
    assert "Migrated syntax: valid" in out
    assert "PASS 4 | FAIL 0 | ERROR 0" in out