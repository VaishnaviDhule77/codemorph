"""Tests for backend.verification.equivalence (Phase 6).

Every expected number below was hand-derived from the formulas in the
module docstring: structural = (fn_component + class_component + var)/3,
control-flow = (nodes + edges + statements)/3 per function, data-flow =
mean of 5 Jaccards per function, aggregate = weighted mean of available
signals.
"""
from __future__ import annotations

import json

import pytest

from backend.analyzer import analyze_source
from backend.analyzer.__main__ import main
from backend.migration import TransformationEngine
from backend.verification import (
    EquivalenceWeights,
    SandboxConfig,
    compute_equivalence,
    control_flow_similarity,
    data_flow_similarity,
    render_equivalence,
    structural_similarity,
)


def signal_by_name(report, name):
    return next(s for s in report.signals if s.name == name)


# -- signal: structural similarity ---------------------------------------------


def test_structural_identical_calculator(calculator_source, calculator_analysis):
    module_b = analyze_source(calculator_source, filename="calculator.py").module
    score, detail = structural_similarity(calculator_analysis.module, module_b)
    assert score == 1.0
    assert detail["function_name_jaccard"] == 1.0
    assert detail["signature_similarity"] == 1.0
    assert detail["class_score"] == 1.0
    assert detail["variable_jaccard"] == 1.0


def test_structural_rename_function():
    original = "def f(x):\n    return x\n"
    renamed = "def g(x):\n    return x\n"
    score, detail = structural_similarity(
        analyze_source(original).module, analyze_source(renamed).module
    )
    # fn jaccard 0, no matched signatures -> 0; classes and vars both empty
    # (1.0 each) -> (0 + 1 + 1) / 3
    assert score == pytest.approx(2 / 3)
    assert detail["function_name_jaccard"] == 0.0
    assert detail["signature_similarity"] == 0.0


def test_structural_added_parameter():
    original = "def f(x):\n    return x + 1\n"
    changed = "def f(x, y):\n    return x + 1\n"
    score, detail = structural_similarity(
        analyze_source(original).module, analyze_source(changed).module
    )
    # 3 of 7 signature features match (async, decorators, method flag);
    # fn jaccard 1.0 -> fn_component = 0.5 + 0.5 * 3/7
    expected = (0.5 + 0.5 * 3 / 7 + 1.0 + 1.0) / 3
    assert score == pytest.approx(expected)
    assert detail["signature_similarity"] == pytest.approx(3 / 7)


def test_structural_added_function():
    original = "def f():\n    return 1\n"
    extended = "def f():\n    return 1\n\n\ndef helper():\n    pass\n"
    score, _ = structural_similarity(
        analyze_source(original).module, analyze_source(extended).module
    )
    # fn jaccard 1/2, matched f identical -> fn_component 0.75
    assert score == pytest.approx((0.75 + 1.0 + 1.0) / 3)


# -- signal: control-flow similarity --------------------------------------------


def test_control_flow_identical():
    source = "def f(x):\n    if x:\n        return 1\n    return 2\n"
    score, detail = control_flow_similarity(
        analyze_source(source).cfgs, analyze_source(source).cfgs
    )
    assert score == 1.0
    assert detail["functions"] == {"f": 1.0}


def test_control_flow_added_branch():
    original = "def f(x):\n    return x\n"
    changed = "def f(x):\n    if x:\n        return 1\n    return 2\n"
    score, detail = control_flow_similarity(
        analyze_source(original).cfgs, analyze_source(changed).cfgs
    )
    # nodes 0.6 ({entry,basic,exit} vs +condition, +basic), edges 0.4
    # ({normal,return} vs +true,+false,+return), statements 1/2
    assert score == pytest.approx((0.6 + 0.4 + 0.5) / 3)
    assert detail["functions"]["f"] == pytest.approx(0.5)


def test_control_flow_duplicate_collapse():
    original = (
        "def bump(total):\n"
        "    total = total + 1\n"
        "    total = total + 1\n"
        "    total = total + 1\n"
        "    return total\n"
    )
    migrated = "def bump(total):\n    total += 3\n    return total\n"
    score, detail = control_flow_similarity(
        analyze_source(original).cfgs, analyze_source(migrated).cfgs
    )
    # node and edge kinds identical; statement counts 4 vs 2 -> (1+1+0.5)/3
    assert score == pytest.approx((1 + 1 + 0.5) / 3)
    assert detail["functions"]["bump"] == pytest.approx(0.833333, abs=1e-4)


def test_control_flow_renamed_function():
    original = "def f(x):\n    return x\n"
    renamed = "def g(x):\n    return x\n"
    score, _ = control_flow_similarity(
        analyze_source(original).cfgs, analyze_source(renamed).cfgs
    )
    assert score == 0.0


# -- signal: data-flow similarity --------------------------------------------------


def test_data_flow_identical():
    source = "def f(x):\n    y = x + 1\n    return y\n"
    score, detail = data_flow_similarity(
        analyze_source(source).flows, analyze_source(source).flows
    )
    assert score == 1.0
    assert detail["functions"] == {"f": 1.0}


def test_data_flow_variable_rename():
    original = "def f(x):\n    y = x + 1\n    return y\n"
    renamed = "def f(x):\n    z = x + 1\n    return z\n"
    score, _ = data_flow_similarity(
        analyze_source(original).flows, analyze_source(renamed).flows
    )
    # params 1; defs {x,y} vs {x,z} -> 1/3 (param defs count);
    # uses 1/3; flows 0; externals 1
    assert score == pytest.approx((1 + 1 / 3 + 1 / 3 + 0 + 1) / 5)


def test_data_flow_added_parameter():
    original = "def f(x):\n    return x + 1\n"
    changed = "def f(x, y):\n    return x + 1\n"
    score, _ = data_flow_similarity(
        analyze_source(original).flows, analyze_source(changed).flows
    )
    # params 1/2, defs 1/2, uses 1, flows 1, externals 1 -> 4/5
    assert score == pytest.approx(0.8)


def test_data_flow_removed_function():
    original = "def f(x):\n    return x\n\n\ndef g(x):\n    return x\n"
    removed = "def f(x):\n    return x\n"
    score, _ = data_flow_similarity(
        analyze_source(original).flows, analyze_source(removed).flows
    )
    assert score == pytest.approx(0.5)


# -- end-to-end estimates --------------------------------------------------------------


def test_equivalence_identical_calculator_static_only(calculator_source):
    report = compute_equivalence(
        calculator_source, calculator_source,
        filename="calculator.py", run_tests=False,
    )
    assert report.score_percent == 100
    assert report.label == "very-high"
    assert [s.name for s in report.signals] == [
        "structural", "control_flow", "data_flow",
    ]
    assert all(s.score == 1.0 for s in report.signals)
    assert report.verification is None
    assert any("static signals only" in note for note in report.notes)


def test_equivalence_identical_calculator_with_tests(calculator_source):
    report = compute_equivalence(
        calculator_source, calculator_source,
        filename="calculator.py",
        sandbox_config=SandboxConfig(timeout=20),
    )
    assert report.score_percent == 100
    assert report.label == "very-high"
    assert [s.name for s in report.signals] == [
        "structural", "control_flow", "data_flow", "test_behavior",
    ]
    assert all(s.score == 1.0 for s in report.signals)
    assert report.verification is not None
    assert report.verification.total == 12


def test_static_signals_blind_to_constant_change():
    """Documented blind spot: structure, CFGs, and data flow are all
    identical when only a constant changes -- only the test signal can
    catch it. This is why the estimate must combine signals."""
    original = "def f(x):\n    return x + 1\n"
    migrated = "def f(x):\n    return x + 2\n"
    report = compute_equivalence(
        original, migrated, filename="inline.py", run_tests=False
    )
    assert report.score_percent == 100
    assert all(s.score == 1.0 for s in report.signals)


def test_tests_expose_constant_change():
    original = "def f(x):\n    return x + 1\n"
    migrated = "def f(x):\n    return x + 2\n"
    report = compute_equivalence(
        original, migrated, filename="inline.py",
        sandbox_config=SandboxConfig(timeout=20),
    )
    assert report.score_percent == 83
    assert report.label == "high"
    assert signal_by_name(report, "test_behavior").score == pytest.approx(1 / 3)
    assert report.verification is not None
    assert report.verification.total == 3
    assert any("FAILED" in note for note in report.notes)


def test_equivalence_added_parameter_static():
    original = "def f(x):\n    return x + 1\n"
    changed = "def f(x, y):\n    return x + 1\n"
    report = compute_equivalence(
        original, changed, filename="inline.py", run_tests=False
    )
    assert signal_by_name(report, "structural").score == pytest.approx(
        0.904762, abs=1e-4
    )
    assert signal_by_name(report, "control_flow").score == 1.0
    assert signal_by_name(report, "data_flow").score == pytest.approx(0.8)
    assert report.score_percent == 90
    assert report.label == "high"


def test_equivalence_renamed_function_static():
    original = "def f(x):\n    return x\n"
    renamed = "def g(x):\n    return x\n"
    report = compute_equivalence(
        original, renamed, filename="inline.py", run_tests=False
    )
    assert report.score_percent == 22
    assert report.label == "very-low"


def test_equivalence_invalid_syntax():
    report = compute_equivalence(
        "def f(x):\n    return x\n", "def broken(:\n", filename="inline.py"
    )
    assert report.score_percent == 0
    assert report.label == "invalid"
    assert report.signals == ()
    assert any("syntax validation" in note for note in report.notes)


def test_equivalence_deterministic_migration():
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
    report = compute_equivalence(
        legacy, migration.migrated_source, filename="legacy.py",
        sandbox_config=SandboxConfig(timeout=20),
    )
    # structural 1.0; cf = (1.0 + 0.8333) / 2; df 1.0; tests 7/7 = 1.0
    assert signal_by_name(report, "control_flow").score == pytest.approx(
        0.9166667, abs=1e-4
    )
    assert report.score_percent == 98
    assert report.label == "very-high"
    assert report.verification is not None
    assert report.verification.total == 7
    assert report.verification.passed == 7


def test_equivalence_python2_original_crash():
    """The mirrored blind spot: static signals are perfect, but the ORIGINAL
    cannot execute on Python 3 -- every test FAILS even though the migration
    repaired the code. FAIL outcomes must be interpreted together with the
    Phase-4 transformation registry."""
    legacy = "def has_key_check(d: dict, k: str):\n    return d.has_key(k)\n"
    migration = TransformationEngine().transform_source(legacy, filename="hk.py")
    assert migration.applied
    report = compute_equivalence(
        legacy, migration.migrated_source, filename="hk.py",
        sandbox_config=SandboxConfig(timeout=20),
    )
    assert all(
        signal_by_name(report, name).score == 1.0
        for name in ("structural", "control_flow", "data_flow")
    )
    assert report.score_percent == 75
    assert report.label == "moderate"
    assert report.verification is not None
    assert report.verification.failed == 4


def test_weights_focus_on_tests():
    original = "def f(x):\n    return x + 1\n"
    migrated = "def f(x):\n    return x + 2\n"
    report = compute_equivalence(
        original, migrated, filename="inline.py",
        sandbox_config=SandboxConfig(timeout=20),
        weights=EquivalenceWeights(
            structural=0.0, control_flow=0.0, data_flow=0.0, test_behavior=1.0
        ),
    )
    # All four signals are still reported; weights affect aggregation only.
    assert [s.name for s in report.signals] == [
        "structural", "control_flow", "data_flow", "test_behavior",
    ]
    assert report.score_percent == 33
    # 33% is below the 40% "low" band
    assert report.label == "very-low"


def test_equivalence_smelly_static(smelly_source):
    report = compute_equivalence(
        smelly_source,
        TransformationEngine().transform_source(
            smelly_source, filename="smelly.py"
        ).migrated_source,
        filename="smelly.py",
        run_tests=False,
    )
    # structural: all 9 signatures unchanged -> 1.0
    # control-flow: bump's basic block holds 5 statements in the original
    # (docstring + 3 assignments + return) and 3 after the collapse, so
    # bump = (1 + 1 + 3/5) / 3 = 13/15 and the mean is (8 + 13/15) / 9
    # = 133/135 (docstrings ARE statements and count -- consistent with
    # Phase-1 counting them as code lines).
    # data-flow: swallow_errors gains the 'Exception' name load -> 0.7 there
    assert signal_by_name(report, "structural").score == 1.0
    assert signal_by_name(report, "control_flow").score == pytest.approx(
        133 / 135, abs=1e-4
    )
    assert signal_by_name(report, "data_flow").score == pytest.approx(
        8.7 / 9, abs=1e-4
    )
    assert report.score_percent == 98
    assert report.label == "very-high"


# -- serialization & rendering ----------------------------------------------------------


def test_report_serialization(calculator_source):
    report = compute_equivalence(
        calculator_source, calculator_source,
        filename="calculator.py", run_tests=False,
    )
    data = json.loads(json.dumps(report.to_dict()))
    assert data["filename"] == "calculator.py"
    assert data["score"] == 100
    assert data["label"] == "very-high"
    assert data["estimate"] is True
    assert "NOT a formal proof" in data["disclaimer"]
    assert data["verification"] is None
    assert [s["name"] for s in data["signals"]] == [
        "structural", "control_flow", "data_flow",
    ]
    assert data["signals"][0]["detail"]["function_name_jaccard"] == 1.0


def test_render_equivalence():
    original = "def f(x):\n    return x + 1\n"
    migrated = "def f(x):\n    return x + 2\n"
    report = compute_equivalence(
        original, migrated, filename="inline.py",
        sandbox_config=SandboxConfig(timeout=20),
    )
    text = render_equivalence(report)
    assert "Semantic Equivalence Estimate: 83% (high)" in text
    assert "NOT a formal proof" in text
    assert "test_behavior" in text
    assert "(1/3 cases passed)" in text


# -- CLI ----------------------------------------------------------------------------------


def test_cli_equivalence(tmp_path, capsys):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def greet(name: str):\n    return 'Hello %s!' % (name,)\n",
        encoding="utf-8",
    )
    assert main([str(sample), "--equivalence"]) == 0
    out = capsys.readouterr().out
    assert "1 transformation(s), applied" in out
    assert "Semantic Equivalence Estimate: 100% (very-high)" in out
    assert "(4/4 cases passed)" in out


def test_cli_equivalence_noop(tmp_path, capsys):
    sample = tmp_path / "sample.py"
    sample.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    assert main([str(sample), "--equivalence"]) == 0
    out = capsys.readouterr().out
    assert "0 transformation(s), no-op" in out
    assert "Semantic Equivalence Estimate: 100% (very-high)" in out