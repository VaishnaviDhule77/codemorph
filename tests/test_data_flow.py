"""Tests for backend.analyzer.data_flow (Phase-3 reaching definitions)."""
from __future__ import annotations

import json

from backend.analyzer import (
    ReturnSummary,
    analyze_source,
    build_data_flows,
    flow_findings,
    render_data_flow,
)
from backend.analyzer.__main__ import main


def flows_of(source: str, filename: str = "inline.py") -> dict:
    analysis = analyze_source(source, filename=filename)
    return {flow.qualified_name: flow for flow in analysis.flows}


def edges_of(report) -> set:
    return {(e.producer, e.consumer, e.line) for e in report.flow_edges}


def reaching_lines(report, use) -> set:
    index = {d.id: d for d in report.definitions}
    return {index[d].line for d in use.reaching}


def uses_of(report, variable, line):
    return [u for u in report.uses
            if u.variable == variable and u.line == line]


# -- chains, params, dead stores ------------------------------------------------


def test_straight_line_chains():
    report = flows_of("def f(x):\n    a = x + 1\n    b = a * 2\n    return b\n")["f"]
    assert [p.variable for p in report.parameters] == ["x"]
    assert all(p.kind == "param" for p in report.parameters)
    assert edges_of(report) == {("x", "a", 2), ("a", "b", 3), ("b", "return", 4)}
    assert report.dead_stores == ()
    assert report.possibly_undefined_uses == ()
    # ReturnSummary is a dataclass: it equals another ReturnSummary with the
    # same fields, never a plain tuple.
    assert report.returns == (
        ReturnSummary(line=4, value="b", used_variables=("b",)),
    )


def test_loop_reaching_definitions_fixpoint():
    source = (
        "def f(n):\n"
        "    i = 0\n"
        "    while i < n:\n"
        "        i = i + 1\n"
        "    return i\n"
    )
    report = flows_of(source)["f"]
    (condition_use,) = uses_of(report, "i", 3)
    # the fixpoint must flow the body definition back through the loop edge
    assert reaching_lines(report, condition_use) == {2, 4}
    assert report.dead_stores == ()


def test_branch_definitions_merge():
    source = (
        "def f(flag):\n"
        "    if flag:\n"
        "        y = 1\n"
        "    else:\n"
        "        y = 2\n"
        "    return y\n"
    )
    report = flows_of(source)["f"]
    (use,) = uses_of(report, "y", 6)
    assert reaching_lines(report, use) == {3, 5}
    assert edges_of(report) == {("y", "return", 6)}


def test_dead_store_killed_in_block():
    report = flows_of("def f(x):\n    y = x * 2\n    y = x\n    return y\n")["f"]
    assert [(d.variable, d.line) for d in report.dead_stores] == [("y", 2)]


def test_unused_parameter_is_dead_store():
    report = flows_of("def f(unused):\n    return 1\n")["f"]
    assert [(d.variable, d.kind) for d in report.dead_stores] == [("unused", "param")]


def test_use_before_definition_flagged():
    report = flows_of("def f(x):\n    return y\n")["f"]
    (use,) = uses_of(report, "y", 2)
    assert use.status == "possibly-undefined"
    assert use.reaching == ()


def test_external_inputs():
    report = flows_of("def f(x):\n    return len(x)\n")["f"]
    (len_use,) = uses_of(report, "len", 2)
    assert len_use.status == "external"
    assert report.external_inputs == ("len",)
    assert report.possibly_undefined_uses == ()


def test_delete_semantics():
    report = flows_of("def f():\n    x = 1\n    del x\n    return x\n")["f"]
    (use,) = uses_of(report, "x", 4)
    assert use.status == "possibly-undefined"  # only the delete-def reaches
    assert [(d.variable, d.line) for d in report.dead_stores] == [("x", 2)]


def test_comprehension_target_binds_before_use():
    report = flows_of("def f(xs):\n    return [v * 2 for v in xs]\n")["f"]
    (v_use,) = uses_of(report, "v", 2)
    assert v_use.status == "ok"  # the comprehension def reaches its own elt use
    assert report.possibly_undefined_uses == ()
    assert ("xs", "v", 2) in edges_of(report)


def test_closure_capture_keeps_definitions_alive():
    source = (
        "def make():\n"
        "    factor = 2\n"
        "    def scale(v):\n"
        "        return v * factor\n"
        "    return scale\n"
    )
    report = flows_of(source)["make"]
    assert report.dead_stores == ()
    (factor_use,) = uses_of(report, "factor", 3)
    assert reaching_lines(report, factor_use) == {2}


# -- calculator integration -------------------------------------------------------


def test_calculate_total_data_flow(calculator_analysis):
    report = {
        flow.qualified_name: flow for flow in calculator_analysis.flows
    }["calculate_total"]
    assert [p.variable for p in report.parameters] == ["amount", "items"]
    assert edges_of(report) == {
        ("amount", "validated", 30),
        ("validated", "tax", 31),
        ("validated", "total", 32),
        ("tax", "total", 32),
        ("items", "item", 35),
        ("total", "total", 37),
        ("total", "receipt", 39),
        ("total", "receipt", 41),
        ("receipt", "return", 42),
    }
    (receipt_use,) = uses_of(report, "receipt", 42)
    assert reaching_lines(report, receipt_use) == {39, 41}  # try + handler paths
    (total_use,) = uses_of(report, "total", 39)
    assert reaching_lines(report, total_use) == {32, 37}    # loop back edge
    (items_use,) = uses_of(report, "items", 35)
    assert reaching_lines(report, items_use) == {28, 34}    # param + reassignment
    assert report.dead_stores == ()
    assert report.possibly_undefined_uses == ()
    # ``except (TypeError, ValueError):`` loads two builtin exception names,
    # which the engine records as external inputs -- consistent with
    # validate_amount's ("ValueError",) expectation.
    assert report.external_inputs == (
        "DISCOUNT_THRESHOLD", "TypeError", "ValueError",
        "calculate_tax", "len", "math", "validate_amount",
    )
    assert report.returns == (
        ReturnSummary(line=42, value="receipt", used_variables=("receipt",)),
    )


def test_unused_self_is_dead_store(calculator_analysis):
    """Flow analysis finds what the lexical rule deliberately skips:
    ``_run_nested`` never reads ``self`` (a staticmethod candidate)."""
    report = {
        flow.qualified_name: flow for flow in calculator_analysis.flows
    }["Calculator._run_nested"]
    assert any(d.variable == "self" and d.kind == "param"
               for d in report.dead_stores)


def test_validate_amount_data_flow(calculator_analysis):
    report = {
        flow.qualified_name: flow for flow in calculator_analysis.flows
    }["validate_amount"]
    assert [p.variable for p in report.parameters] == ["amount"]
    assert edges_of(report) == {("amount", "return", 20)}
    assert report.external_inputs == ("ValueError",)
    assert report.dead_stores == ()
    assert report.possibly_undefined_uses == ()


def test_classify_initial_label_is_dead_store(smelly_source):
    flows = flows_of(smelly_source, filename="smelly.py")
    dead = [(d.variable, d.line) for d in flows["classify"].dead_stores]
    # every path through the if/elif chain reassigns label, so the initial
    # assignment is a genuine dead store
    assert ("label", 63) in dead


# -- flow findings & rendering ------------------------------------------------------


def test_flow_findings_categories_and_severity():
    analysis = analyze_source(
        "def f(x):\n    y = x\n    return z\n", filename="inline.py"
    )
    findings = flow_findings(analysis.flows, "inline.py")
    assert [(f.line, f.category, f.severity.value) for f in findings] == [
        (2, "DEAD_STORE", "LOW"),
        (3, "POSSIBLY_UNDEFINED_USE", "HIGH"),
    ]
    assert all(f.suggestion for f in findings)


def test_render_data_flow_text(calculator_analysis):
    report = {
        flow.qualified_name: flow for flow in calculator_analysis.flows
    }["calculate_total"]
    text = render_data_flow(report)
    assert "Data flow: calculate_total" in text
    assert "amount -> validated   [line 30]" in text
    assert "receipt -> return   [line 42]" in text
    assert (
        "External inputs: DISCOUNT_THRESHOLD, TypeError, ValueError, "
        "calculate_tax, len, math, validate_amount"
    ) in text
    assert "Dead stores: (none)" in text


# -- service & CLI wiring --------------------------------------------------------------


def test_service_attaches_cfgs_and_flows(calculator_analysis):
    assert len(calculator_analysis.cfgs) == 8
    assert len(calculator_analysis.flows) == 8
    assert [f.qualified_name for f in calculator_analysis.flows] == [
        c.qualified_name for c in calculator_analysis.cfgs
    ]
    payload = json.loads(json.dumps(calculator_analysis.to_dict()))
    assert len(payload["cfgs"]) == 8
    assert len(payload["data_flows"]) == 8


def test_reports_are_json_serializable(smelly_source):
    analysis = analyze_source(smelly_source, filename="smelly.py")
    data = json.loads(json.dumps([f.to_dict() for f in analysis.flows]))
    assert data[0]["qualified_name"] == "circle_area"
    assert "flow_edges" in data[0]


def test_cli_flow_flag(tmp_path, capsys):
    sample = tmp_path / "sample.py"
    sample.write_text("def f(x):\n    return x * 2\n", encoding="utf-8")
    assert main([str(sample), "--flow"]) == 0
    out = capsys.readouterr().out
    assert "CFG: f" in out
    assert "Data flow: f" in out


def test_cli_dot_flag(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text("def f(x):\n    return x * 2\n", encoding="utf-8")
    dot_path = tmp_path / "cfg.dot"
    assert main([str(sample), "--dot", str(dot_path)]) == 0
    assert "digraph codemorph {" in dot_path.read_text(encoding="utf-8")