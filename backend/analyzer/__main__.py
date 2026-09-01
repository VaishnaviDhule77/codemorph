"""CLI for the CodeMorph analyzer.

Usage::

    python -m backend.analyzer file.py [--json] [--findings] [--flow]
                                       [--dot OUT.dot]
                                       [--migrate] [--migrate-out OUT.py]
                                       [--verify]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from ..migration.deterministic import TransformationEngine
from ..verification import verify_migration
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


def _run_migration(args: argparse.Namespace, source: str) -> int:
    engine = TransformationEngine()
    result = engine.transform_source(source, filename=args.path.name)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0
    status = "applied" if result.applied else (
        "rejected" if result.transformations else "no-op"
    )
    print(
        f"Deterministic migrations: "
        f"{len(result.transformations)} transformation(s), {status}"
    )
    if result.rejected_reason:
        print(f"  rejected: {result.rejected_reason}")
    for t in result.transformations:
        print(f"  [{t.risk.value:>6}] line {t.line:>4}  {t.kind}")
        for line in t.original.splitlines() or [""]:
            print(f"      - {line}")
        for line in t.replacement.splitlines() or [""]:
            print(f"      + {line}")
        print(f"      reason: {t.reason}")
    if args.migrate_out is not None:
        args.migrate_out.write_text(result.migrated_source, encoding="utf-8")
        print(f"wrote migrated source to {args.migrate_out}")
    elif result.applied:
        print()
        print("Migrated source:")
        print(result.migrated_source, end="")
        print()
    return 0


def _run_verify(args: argparse.Namespace, source: str) -> int:
    engine = TransformationEngine()
    migration = engine.transform_source(source, filename=args.path.name)
    result = verify_migration(
        source, migration.migrated_source, filename=args.path.name
    )
    print(
        f"Deterministic migrations: {len(migration.transformations)} "
        f"transformation(s), "
        f"{'applied' if migration.applied else 'no-op'}"
    )
    if migration.rejected_reason:
        print(f"  rejected: {migration.rejected_reason}")
    print()
    print("Verification (Phase 5)")
    print(f"  Migrated syntax: {'valid' if result.syntax_check.valid else 'INVALID'}")
    if result.note:
        print(f"  Note: {result.note}")
    print(
        f"  Functions tested: {len(result.functions_tested)} "
        f"({result.total} cases)"
    )
    print(f"  PASS {result.passed} | FAIL {result.failed} | ERROR {result.errors}")
    for outcome in result.outcomes:
        detail = outcome.detail
        if len(detail) > 72:
            detail = detail[:69] + "..."
        print(
            f"    {outcome.case.function:<30} {outcome.case.description:<10} "
            f"{outcome.status:<5} {detail}"
        )
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codemorph",
        description="CodeMorph analyzer: AST structure, metrics, complexity, "
                    "findings, control & data flow, deterministic migration, "
                    "sandboxed verification.",
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
    parser.add_argument(
        "--migrate", action="store_true",
        help="apply deterministic transformations and print a traceable report",
    )
    parser.add_argument(
        "--migrate-out", type=pathlib.Path, metavar="OUT",
        help="with --migrate: write the migrated source to OUT instead of printing",
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="migrate, then run sandboxed differential verification (Phase 5)",
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

    if args.migrate:
        return _run_migration(args, source)

    if args.verify:
        return _run_verify(args, source)

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