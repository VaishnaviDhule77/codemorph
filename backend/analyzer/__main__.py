"""CLI for the CodeMorph analyzer.

Usage::

    python -m backend.analyzer file.py [--json] [--findings] [--flow]
                                       [--dot OUT.dot]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from .ast_analyzer import SourceParseError
from .control_flow import cfgs_to_dot, render_cfg
from .data_flow import flow_findings, render_data_flow
from .findings import severity_counts
from .service import FileAnalysis, analyze_source, run_findings


def _print_text_report(result: FileAnalysis) -> None:
    print(result.structure)
    print()
    m = result.metrics
    print("Metrics")
    print(
        f"  Lines: {m.total_lines} total | {m.code_lines} code | "
        f"{m.blank_lines} blank | {m.comment_lines} comment"
    )
    print(
        f"  Functions: {m.num_functions} ({m.num_methods} methods) | "
        f"Classes: {m.num_classes} | Imports: {m.num_imports}"
    )
    print(
        f"  Max nesting depth: {m.max_nesting_depth} | "
        f"Function length: max {m.max_function_length}, avg {m.average_function_length}"
    )
    c = result.complexity
    print()
    print("Cyclomatic complexity (McCabe)")
    print(
        f"  Module level: {c.module_level} | Total: {c.total} | "
        f"Functions: {len(c.functions)}"
    )
    for fn in sorted(c.functions, key=lambda f: f.complexity, reverse=True)[:5]:
        print(f"  {fn.qualified_name}: {fn.complexity} (rank {fn.rank})")


def _print_findings(findings, title: str = "Static-analysis findings") -> None:
    print()
    print(f"{title} ({len(findings)})")
    if not findings:
        print("  No findings.")
        return
    for finding in findings:
        print(
            f"  [{finding.severity.value:>6}] line {finding.line:>4}  "
            f"{finding.category:<24} {finding.message}"
        )
        print(f"          suggestion: {finding.suggestion}")
    counts = severity_counts(findings)
    print(
        f"  Severity: {counts['HIGH']} high, "
        f"{counts['MEDIUM']} medium, {counts['LOW']} low"
    )


def _print_flow(result: FileAnalysis, flow_findings_list) -> None:
    for cfg in result.cfgs:
        print()
        print(render_cfg(cfg))
    for flow in result.flows:
        print()
        print(render_data_flow(flow))
    if flow_findings_list:
        _print_findings(flow_findings_list, "Flow-sensitive findings")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codemorph",
        description="CodeMorph analyzer: AST structure, metrics, complexity, "
                    "findings, control & data flow.",
    )
    parser.add_argument("path", type=pathlib.Path, help="Python source file")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--findings", action="store_true",
        help="run the Phase-2 static-analysis rules",
    )
    parser.add_argument(
        "--flow", action="store_true",
        help="print per-function CFGs and data-flow reports",
    )
    parser.add_argument(
        "--dot", type=pathlib.Path, metavar="OUT",
        help="write a Graphviz DOT file of all CFGs",
    )
    args = parser.parse_args(argv)

    try:
        source = args.path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {args.path}: {exc}", file=sys.stderr)
        return 2

    try:
        result = analyze_source(source, filename=args.path.name)
    except SourceParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.dot is not None:
        args.dot.write_text(cfgs_to_dot(result.cfgs), encoding="utf-8")
        print(f"wrote Graphviz DOT to {args.dot}")
        return 0

    findings = run_findings(result) if args.findings else []
    flow_findings_list = flow_findings(result.flows, result.filename) if args.flow else []

    if args.json:
        payload = result.to_dict()
        if args.findings:
            payload["findings"] = [f.to_dict() for f in findings]
        if args.flow:
            payload["flow_findings"] = [f.to_dict() for f in flow_findings_list]
        print(json.dumps(payload, indent=2))
        return 0

    _print_text_report(result)
    if args.findings:
        _print_findings(findings)
    if args.flow:
        _print_flow(result, flow_findings_list)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())