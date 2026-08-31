"""Tests for backend.analyzer.control_flow (Phase-3 CFG builder)."""
from __future__ import annotations

import json
import sys

import pytest

from backend.analyzer import analyze_source, build_cfgs, cfgs_to_dot, render_cfg


def cfgs_of(source: str) -> dict:
    return {cfg.qualified_name: cfg for cfg in analyze_source(source).cfgs}


def edge_tuples(cfg) -> set:
    return {(e.source, e.target, e.kind) for e in cfg.edges}


def assert_valid(cfg) -> None:
    ids = [n.id for n in cfg.nodes]
    assert len(ids) == len(set(ids)), "duplicate node ids"
    id_set = set(ids)
    for e in cfg.edges:
        assert e.source in id_set and e.target in id_set
    assert cfg.predecessor_edges("entry") == []
    assert cfg.successor_edges("exit") == []


# -- core shapes --------------------------------------------------------------


def test_straight_line_graph():
    cfg = cfgs_of("def f(x):\n    a = x + 1\n    return a\n")["f"]
    assert [n.kind for n in cfg.nodes] == ["entry", "basic", "exit"]
    block = cfg.nodes[1]
    assert block.statements == ("a = x + 1", "return a")
    assert edge_tuples(cfg) == {("entry", "n1", "normal"), ("n1", "exit", "return")}


def test_if_else_graph():
    source = (
        "def f(x):\n"
        "    if x:\n"
        "        r = 1\n"
        "    else:\n"
        "        r = 2\n"
        "    return r\n"
    )
    cfg = cfgs_of(source)["f"]
    assert len(cfg.nodes) == 6  # entry, cond, 2 bodies, join, exit
    cond = cfg.nodes_of_kind("condition")[0]
    assert cond.condition == "x"
    then_block = cfg.node_with_statement("r = 1")
    else_block = cfg.node_with_statement("r = 2")
    join = cfg.node_with_statement("return r")
    assert edge_tuples(cfg) == {
        ("entry", cond.id, "normal"),
        (cond.id, then_block.id, "true"),
        (cond.id, else_block.id, "false"),
        (then_block.id, join.id, "normal"),
        (else_block.id, join.id, "normal"),
        (join.id, "exit", "return"),
    }


def test_while_loop_back_edge():
    source = (
        "def f(n):\n"
        "    i = 0\n"
        "    while i < n:\n"
        "        i += 1\n"
        "    return i\n"
    )
    cfg = cfgs_of(source)["f"]
    loop = cfg.nodes_of_kind("loop")[0]
    assert loop.condition == "i < n"
    body = cfg.node_with_statement("i += 1")
    after = cfg.node_with_statement("return i")
    assert (body.id, loop.id, "loop_back") in edge_tuples(cfg)
    assert (loop.id, body.id, "true") in edge_tuples(cfg)
    assert (loop.id, after.id, "false") in edge_tuples(cfg)


def test_for_break_skips_else():
    source = (
        "def f(items):\n"
        "    for x in items:\n"
        "        if x:\n"
        "            break\n"
        "    else:\n"
        "        result = 'done'\n"
        "    return result\n"
    )
    cfg = cfgs_of(source)["f"]
    loop = cfg.nodes_of_kind("loop")[0]
    assert loop.description == "for x in items"
    break_block = cfg.node_with_statement("break")
    else_block = cfg.node_with_statement("result = 'done'")
    join = cfg.node_with_statement("return result")
    # break jumps past the else clause, directly to the join
    assert [(e.target, e.kind) for e in cfg.successor_edges(break_block.id)] == [
        (join.id, "break")
    ]
    assert (loop.id, else_block.id, "false") in edge_tuples(cfg)
    assert (else_block.id, join.id, "normal") in edge_tuples(cfg)
    assert cfg.predecessor_edges(else_block.id)[0].source == loop.id


def test_continue_edge_targets_loop_header():
    source = (
        "def f(items):\n"
        "    for x in items:\n"
        "        if x:\n"
        "            continue\n"
        "        total = x\n"
        "    return total\n"
    )
    cfg = cfgs_of(source)["f"]
    loop = cfg.nodes_of_kind("loop")[0]
    continue_block = cfg.node_with_statement("continue")
    assert (continue_block.id, loop.id, "continue") in edge_tuples(cfg)


def test_try_except_shape():
    source = (
        "def f(v):\n"
        "    try:\n"
        "        x = int(v)\n"
        "    except ValueError:\n"
        "        x = 0\n"
        "    return x\n"
    )
    cfg = cfgs_of(source)["f"]
    body = cfg.node_with_statement("x = int(v)")
    handler = cfg.nodes_of_kind("handler")[0]
    assert handler.description == "except ValueError:"
    handler_body = cfg.node_with_statement("x = 0")
    join = cfg.node_with_statement("return x")
    assert edge_tuples(cfg) == {
        ("entry", body.id, "normal"),
        (body.id, handler.id, "exception"),
        (handler.id, handler_body.id, "normal"),
        (body.id, join.id, "normal"),
        (handler_body.id, join.id, "normal"),
        (join.id, "exit", "return"),
    }


def test_raise_uncaught_flows_to_exit():
    source = (
        "def f(x):\n"
        "    if x < 0:\n"
        "        raise ValueError('neg')\n"
        "    return x\n"
    )
    cfg = cfgs_of(source)["f"]
    raise_block = cfg.node_with_statement("raise ValueError('neg')")
    assert (raise_block.id, "exit", "exception") in edge_tuples(cfg)


def test_raise_inside_try_has_single_exception_edge():
    source = (
        "def f(v):\n"
        "    try:\n"
        "        raise ValueError(v)\n"
        "    except ValueError:\n"
        "        return 0\n"
    )
    cfg = cfgs_of(source)["f"]
    raise_block = cfg.node_with_statement("raise ValueError(v)")
    handler = cfg.nodes_of_kind("handler")[0]
    edges = [e for e in cfg.edges
             if e.source == raise_block.id and e.target == handler.id]
    assert len(edges) == 1  # deduplicated: explicit raise + generic edge
    assert edges[0].kind == "exception" and edges[0].label == "raise"


def test_dead_code_is_unreachable():
    cfg = cfgs_of("def f():\n    return 1\n    print('dead')\n")["f"]
    assert cfg.dead_code_ids() == ["n2"]
    assert cfg.node("n2").statements == ("print('dead')",)


def test_with_statement_is_transparent():
    source = (
        "def f(p):\n"
        "    with open(p) as h:\n"
        "        data = h.read()\n"
        "    return data\n"
    )
    cfg = cfgs_of(source)["f"]
    assert [n.kind for n in cfg.nodes] == ["entry", "basic", "exit"]
    block = cfg.nodes[1]
    assert block.statements == (
        "with open(p) as h:", "data = h.read()", "return data",
    )


# -- integration fixtures --------------------------------------------------------


def test_every_function_gets_a_cfg(calculator_analysis):
    assert [cfg.qualified_name for cfg in calculator_analysis.cfgs] == [
        "validate_amount", "calculate_tax", "calculate_total",
        "Calculator.__init__", "Calculator.add",
        "Calculator._run_nested", "Calculator._run_nested.clamp", "main",
    ]


def test_calculate_total_graph_shape(calculator_analysis):
    cfg = {c.qualified_name: c for c in calculator_analysis.cfgs}["calculate_total"]
    assert len(cfg.nodes) == 12
    assert len(cfg.edges) == 15
    assert_valid(cfg)


def test_calculate_total_semantic_edges(calculator_analysis):
    cfg = {c.qualified_name: c for c in calculator_analysis.cfgs}["calculate_total"]
    loop = cfg.nodes_of_kind("loop")[0]
    assert loop.description == "for item in items"
    assert cfg.nodes_of_kind("condition")[0].condition == "items is None"
    assert cfg.nodes_of_kind("condition")[1].condition == "len(item) > 3"
    handler = cfg.nodes_of_kind("handler")[0]
    assert handler.description == "except (TypeError, ValueError):"
    try_body = cfg.node_with_statement("receipt = math.fsum([total, 0.0])")
    assert (try_body.id, handler.id, "exception") in edge_tuples(cfg)
    assert sum(1 for e in cfg.edges if e.kind == "loop_back") == 2
    return_block = cfg.node_with_statement("return receipt")
    assert (return_block.id, "exit", "return") in edge_tuples(cfg)


def test_cfg_invariants_hold_for_calculator(calculator_analysis):
    for cfg in calculator_analysis.cfgs:
        assert_valid(cfg)


def test_cfg_invariants_hold_for_smelly(smelly_source):
    analysis = analyze_source(smelly_source, filename="smelly.py")
    assert len(analysis.cfgs) == 9  # one per function, incl. nested logic
    for cfg in analysis.cfgs:
        assert_valid(cfg)


def test_async_function_cfg():
    source = (
        "async def fetch(xs):\n"
        "    async for x in xs:\n"
        "        await x\n"
        "    return None\n"
    )
    cfg = build_cfgs(__import__("ast").parse(source))[0]
    assert cfg.is_async is True
    loop = cfg.nodes_of_kind("loop")[0]
    assert loop.description == "for x in xs"
    body = cfg.node_with_statement("await x")
    assert (body.id, loop.id, "loop_back") in edge_tuples(cfg)


@pytest.mark.skipif(sys.version_info < (3, 10), reason="match requires 3.10+")
def test_match_statement_branches():
    source = (
        "def f(x):\n"
        "    match x:\n"
        "        case 1:\n"
        "            return 'one'\n"
        "        case 2:\n"
        "            return 'two'\n"
        "    return 'many'\n"
    )
    cfg = cfgs_of(source)["f"]
    match_node = cfg.nodes_of_kind("match")[0]
    case_edges = [e for e in cfg.edges if e.kind == "case"]
    assert len(case_edges) == 2
    assert all(e.source == match_node.id for e in case_edges)
    assert (match_node.id, cfg.node_with_statement("return 'many'").id, "false") \
        in edge_tuples(cfg)


# -- serialization & rendering ------------------------------------------------------


def test_cfg_to_dict_is_serializable(calculator_analysis):
    data = json.loads(json.dumps(calculator_analysis.cfgs[0].to_dict()))
    assert data["qualified_name"] == "validate_amount"
    assert {"id", "kind", "statements"} <= set(data["nodes"][0])
    assert {"source", "target", "kind"} <= set(data["edges"][0])


def test_render_cfg_text(calculator_analysis):
    cfg = {c.qualified_name: c for c in calculator_analysis.cfgs}["calculate_total"]
    text = render_cfg(cfg)
    assert "CFG: calculate_total (12 nodes, 15 edges)" in text
    assert "for item in items" in text
    assert "loop_back" in text
    assert "except (TypeError, ValueError):" in text


def test_dot_export(calculator_analysis):
    dot = cfgs_to_dot(calculator_analysis.cfgs)
    assert dot.startswith("digraph codemorph {")
    assert 'label="calculate_total";' in dot
    assert ' [label="loop_back"];' in dot