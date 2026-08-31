"""Tests for backend.analyzer.findings (Phase-2 static-analysis rules)."""
from __future__ import annotations

import json

from backend.analyzer import (
    Category,
    Finding,
    FindingsConfig,
    FindingsEngine,
    Severity,
    analyze_source,
    run_findings,
    severity_counts,
)
from backend.analyzer.__main__ import main


def analyze_findings(source: str, config: FindingsConfig | None = None,
                     filename: str = "inline.py"):
    analysis = analyze_source(source, filename=filename)
    return FindingsEngine(config).analyze(analysis)


def tuples(findings: list[Finding]) -> list[tuple[int, str, str]]:
    return [(f.line, f.category, f.severity.value) for f in findings]


# -- integration: the smelly fixture fires every rule -------------------------

EXPECTED_SMELLY = [
    (7, "UNUSED_IMPORT", "MEDIUM"),          # os
    (8, "UNUSED_IMPORT", "MEDIUM"),          # sys
    (9, "UNUSED_IMPORT", "MEDIUM"),          # sqrt (pi stays used)
    (11, "UNUSED_VARIABLE", "LOW"),          # DEBUG (module level)
    (21, "MISSING_ERROR_HANDLING", "MEDIUM"),  # open, unguarded
    (22, "MISSING_ERROR_HANDLING", "LOW"),     # json.loads, unguarded
    (37, "DUPLICATED_PATTERN", "LOW"),       # 3x "total = total + 1"
    (45, "DANGEROUS_EVAL", "HIGH"),
    (46, "DANGEROUS_EXEC", "HIGH"),
    (57, "DEEP_NESTING", "MEDIUM"),          # nesting depth 4, first offender
    (61, "EXCESSIVE_BRANCHING", "MEDIUM"),   # 20 conditions
    (61, "HIGH_COMPLEXITY", "HIGH"),         # complexity 22 > 20
    (61, "LONG_FUNCTION", "MEDIUM"),         # 54 lines
    (119, "MISSING_ERROR_HANDLING", "MEDIUM"),  # open outside the try below
    (121, "UNUSED_VARIABLE", "LOW"),         # cache
    (123, "UNUSED_VARIABLE", "LOW"),         # leftover (loop target)
    (127, "UNUSED_VARIABLE", "LOW"),         # err (except-as name)
    (135, "BARE_EXCEPT", "MEDIUM"),
]


def test_fixture_fires_every_rule(smelly_source):
    findings = analyze_findings(smelly_source, filename="smelly.py")
    assert tuples(findings) == EXPECTED_SMELLY
    assert {f.category for f in findings} >= {
        Category.UNUSED_IMPORT, Category.UNUSED_VARIABLE, Category.LONG_FUNCTION,
        Category.DEEP_NESTING, Category.HIGH_COMPLEXITY,
        Category.EXCESSIVE_BRANCHING, Category.DUPLICATED_PATTERN,
        Category.MISSING_ERROR_HANDLING, Category.BARE_EXCEPT,
        Category.DANGEROUS_EVAL, Category.DANGEROUS_EXEC,
    }


def test_fixture_severity_counts(smelly_source):
    findings = analyze_findings(smelly_source, filename="smelly.py")
    assert severity_counts(findings) == {"HIGH": 3, "MEDIUM": 9, "LOW": 6}


def test_fixture_messages_are_actionable(smelly_source):
    findings = analyze_findings(smelly_source, filename="smelly.py")
    by_key = {(f.category, f.line): f for f in findings}
    assert "'sqrt'" in by_key[("UNUSED_IMPORT", 9)].message
    assert "'math'" in by_key[("UNUSED_IMPORT", 9)].message
    assert "Module-level variable 'DEBUG'" in by_key[("UNUSED_VARIABLE", 11)].message
    assert "'risky_cleanup'" in by_key[("UNUSED_VARIABLE", 121)].message
    assert "(22)" in by_key[("HIGH_COMPLEXITY", 61)].message
    assert "54 lines" in by_key[("LONG_FUNCTION", 61)].message
    assert "4 levels deep" in by_key[("DEEP_NESTING", 57)].message
    assert "3 consecutive" in by_key[("DUPLICATED_PATTERN", 37)].message
    assert all(f.suggestion for f in findings)  # every finding is actionable


def test_calculator_fixture_has_no_findings(calculator_analysis):
    """The clean Phase-1 fixture must produce zero findings."""
    assert FindingsEngine().analyze(calculator_analysis) == []


# -- rule: unused imports ------------------------------------------------------


def test_unused_import_alias_and_messages():
    source = "import numpy as np\n"
    findings = analyze_findings(source)
    assert tuples(findings) == [(1, "UNUSED_IMPORT", "MEDIUM")]
    assert "'np'" in findings[0].message


def test_shadowed_import_flagged_correctly():
    # The local `os` shadows the import: only the import is truly unused.
    source = "import os\n\ndef f():\n    os = 1\n    return os\n"
    assert tuples(analyze_findings(source)) == [(1, "UNUSED_IMPORT", "MEDIUM")]


def test_future_and_star_imports_exempt():
    future = "from __future__ import annotations\nimport os\n"
    assert tuples(analyze_findings(future)) == [(2, "UNUSED_IMPORT", "MEDIUM")]
    star = "from os.path import *\n"
    assert analyze_findings(star) == []


def test_reexport_via_all_exempt():
    source = 'from math import sqrt\n\n__all__ = ["sqrt"]\n'
    assert analyze_findings(source) == []


# -- rule: unused variables ----------------------------------------------------


def test_unused_variables_module_and_local():
    source = "DEBUG = True\n\n\ndef f():\n    cache = {}\n"
    findings = analyze_findings(source)
    assert tuples(findings) == [(1, "UNUSED_VARIABLE", "LOW"),
                                (5, "UNUSED_VARIABLE", "LOW")]
    assert "Module-level" in findings[0].message
    assert "in function 'f'" in findings[1].message


def test_underscore_params_and_dunder_exempt():
    assert analyze_findings("def f():\n    _ignored = 1\n    return 2\n") == []
    assert analyze_findings("def callback(unused_a, unused_b):\n    return 1\n") == []


def test_closure_reads_count():
    source = (
        "def make():\n"
        "    factor = 2\n"
        "    def scale(x):\n"
        "        return x * factor\n"
        "    return scale\n"
    )
    assert analyze_findings(source) == []


def test_augassign_and_del_count_as_use():
    # Conservative by design: x += 1 and del x read the binding.
    assert analyze_findings("def f():\n    x = 0\n    x += 1\n    return None\n") == []
    assert analyze_findings("def f():\n    x = 1\n    del x\n") == []


def test_global_declaration_marks_used():
    source = "COUNT = 0\n\n\ndef bump_count():\n    global COUNT\n    COUNT = COUNT + 1\n"
    assert analyze_findings(source) == []


def test_comprehension_target_can_be_unused():
    source = "def f():\n    return [1 for x in range(3)]\n"
    assert tuples(analyze_findings(source)) == [(2, "UNUSED_VARIABLE", "LOW")]


def test_comprehension_element_reading_target_is_not_unused():
    """Regression: ``[clamp(v) for v in values]`` must not flag ``v``.

    AST field order visits the element expression before the generator
    target; without explicit handling the read of ``v`` finds no binder
    and ``v`` is falsely reported as unused.
    """
    list_comp = "def f(values):\n    return [v * 2 for v in values]\n"
    assert analyze_findings(list_comp) == []

    dict_comp = "def f(pairs):\n    return {k: v for k, v in pairs}\n"
    assert analyze_findings(dict_comp) == []

    nested = "def f(rows):\n    return [y for row in rows for y in row]\n"
    assert analyze_findings(nested) == []

    gen_exp = "def f(values):\n    return any(v > 0 for v in values)\n"
    assert analyze_findings(gen_exp) == []


# -- rules: size & complexity (thresholds tuned via config) ----------------------


def test_long_function_threshold():
    source = "def f():\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    return a + b + c + d\n"
    findings = analyze_findings(source, FindingsConfig(long_function_lines=5))
    assert tuples(findings) == [(1, "LONG_FUNCTION", "MEDIUM")]


def test_deep_nesting_location_points_at_offending_line():
    source = (
        "def f(xs):\n"
        "    for x in xs:\n"
        "        if x:\n"
        "            if x > 0:\n"
        "                return x\n"
        "    return None\n"
    )
    findings = analyze_findings(source, FindingsConfig(deep_nesting_depth=2))
    assert tuples(findings) == [(5, "DEEP_NESTING", "MEDIUM")]


def test_complexity_severity_boundaries():
    def chain(n: int) -> str:
        lines = ["def f(x):"]
        for i in range(n):
            lines.append(f"    if x == {i}:")
            lines.append("        x += 1")
        lines.append("    return x")
        return "\n".join(lines) + "\n"

    medium = [f for f in analyze_findings(chain(10))
              if f.category == Category.HIGH_COMPLEXITY]
    assert len(medium) == 1 and medium[0].severity.value == "MEDIUM"

    high = [f for f in analyze_findings(chain(20))
            if f.category == Category.HIGH_COMPLEXITY]
    assert len(high) == 1 and high[0].severity.value == "HIGH"
    assert "(21)" in high[0].message


def test_excessive_branching_threshold():
    source = (
        "def route(code):\n"
        "    if code == 1:\n"
        "        return 'a'\n"
        "    if code == 2:\n"
        "        return 'b'\n"
        "    if code == 3:\n"
        "        return 'c'\n"
        "    return None\n"
    )
    findings = analyze_findings(source, FindingsConfig(excessive_branching=2))
    assert tuples(findings) == [(1, "EXCESSIVE_BRANCHING", "MEDIUM")]


# -- rule: duplicated patterns ----------------------------------------------------


def test_duplicate_runs():
    two = "def f():\n    x = 1\n    x = 1\n    return x\n"
    assert analyze_findings(two) == []

    three = "x = 0\nx = x + 1\nx = x + 1\nx = x + 1\nprint(x)\n"
    findings = analyze_findings(three)
    assert tuples(findings) == [(2, "DUPLICATED_PATTERN", "LOW")]
    assert "3 consecutive" in findings[0].message

    four = "x = 0\nx = x + 1\nx = x + 1\nx = x + 1\nx = x + 1\nprint(x)\n"
    findings = analyze_findings(four)
    assert tuples(findings) == [(2, "DUPLICATED_PATTERN", "LOW")]
    assert "4 consecutive" in findings[0].message

    interleaved = "a = 1\nb = 2\na = 1\nb = 2\na = 1\nb = 2\nprint(a, b)\n"
    assert analyze_findings(interleaved) == []


# -- rule: missing error handling ----------------------------------------------------


def test_missing_error_handling_guarded_vs_not():
    source = (
        "import json\n"
        "\n"
        "def safe(raw):\n"
        "    try:\n"
        "        return int(raw)\n"
        "    except ValueError:\n"
        "        return 0\n"
        "\n"
        "\n"
        "def unsafe(path):\n"
        "    handle = open(path)\n"
        "    data = json.loads(handle.read())\n"
        "    handle.close()\n"
        "    return data\n"
    )
    findings = [f for f in analyze_findings(source)
                if f.category == Category.MISSING_ERROR_HANDLING]
    assert tuples(findings) == [
        (11, "MISSING_ERROR_HANDLING", "MEDIUM"),   # open
        (12, "MISSING_ERROR_HANDLING", "LOW"),      # json.loads
    ]


def test_else_and_handler_clauses_are_not_guards():
    # Real Python semantics: exceptions raised in a try's else clause or in
    # an except handler are NOT caught by that same try.
    else_case = (
        "def f(p):\n"
        "    try:\n"
        "        pass\n"
        "    except OSError:\n"
        "        pass\n"
        "    else:\n"
        "        open(p)\n"
        "    return None\n"
    )
    findings = [f for f in analyze_findings(else_case)
                if f.category == Category.MISSING_ERROR_HANDLING]
    assert tuples(findings) == [(7, "MISSING_ERROR_HANDLING", "MEDIUM")]

    handler_case = (
        "def f(p):\n"
        "    try:\n"
        "        pass\n"
        "    except OSError:\n"
        "        open(p)\n"
        "    return None\n"
    )
    findings = [f for f in analyze_findings(handler_case)
                if f.category == Category.MISSING_ERROR_HANDLING]
    assert tuples(findings) == [(5, "MISSING_ERROR_HANDLING", "MEDIUM")]


def test_guard_resets_at_function_boundary():
    # Guards are intraprocedural (documented limitation): the def inside the
    # try only binds there; inner's body is analyzed as unguarded.
    source = (
        "def outer(p):\n"
        "    try:\n"
        "        def inner():\n"
        "            open(p)\n"
        "        inner()\n"
        "    except OSError:\n"
        "        pass\n"
    )
    findings = [f for f in analyze_findings(source)
                if f.category == Category.MISSING_ERROR_HANDLING]
    assert tuples(findings) == [(4, "MISSING_ERROR_HANDLING", "MEDIUM")]


# -- rules: dangerous constructs & bare except ------------------------------------------


def test_dangerous_calls_flag_builtins_only():
    # s.eval() is a method call (cf. torch's model.eval()), NOT the builtin.
    source = (
        "def f(s):\n"
        "    eval(s)\n"
        "    exec(s)\n"
        "    s.eval()\n"
        "    return s\n"
    )
    assert tuples(analyze_findings(source)) == [
        (2, "DANGEROUS_EVAL", "HIGH"),
        (3, "DANGEROUS_EXEC", "HIGH"),
    ]


def test_bare_vs_typed_except():
    source = (
        "def f(x):\n"
        "    try:\n"
        "        return x\n"
        "    except:\n"
        "        return None\n"
        "\n"
        "def g(x):\n"
        "    try:\n"
        "        return x\n"
        "    except ValueError:\n"
        "        return None\n"
    )
    assert tuples(analyze_findings(source)) == [(4, "BARE_EXCEPT", "MEDIUM")]


# -- serialization & service wiring -------------------------------------------------------


def test_finding_json_serialization():
    finding = Finding(
        file="x.py", line=3, category=Category.BARE_EXCEPT,
        severity=Severity.MEDIUM, message="m", suggestion="s",
    )
    data = json.loads(json.dumps(finding.to_dict()))
    assert data == {
        "file": "x.py", "line": 3, "category": "BARE_EXCEPT",
        "severity": "MEDIUM", "message": "m", "suggestion": "s",
    }


def test_run_findings_service(smelly_source, calculator_analysis):
    assert run_findings(calculator_analysis) == []
    smelly = analyze_source(smelly_source, filename="smelly.py")
    assert len(run_findings(smelly)) == 18


# -- CLI ------------------------------------------------------------------------------------


def test_cli_findings_text(tmp_path, capsys):
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n", encoding="utf-8")
    assert main([str(sample), "--findings"]) == 0
    out = capsys.readouterr().out
    assert "Static-analysis findings (1)" in out
    assert "UNUSED_IMPORT" in out
    assert "Severity: 0 high, 1 medium, 0 low" in out


def test_cli_findings_json(tmp_path, capsys):
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n", encoding="utf-8")
    assert main([str(sample), "--findings", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["findings"][0]["category"] == "UNUSED_IMPORT"
    assert data["findings"][0]["severity"] == "MEDIUM"
    assert data["findings"][0]["suggestion"]