"""CLI demo for the Phase-1 analyzer.

Usage::

    python -m backend.analyzer path/to/file.py [--json]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from .ast_analyzer import SourceParseError
from .service import FileAnalysis, analyze_source


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codemorph",
        description="CodeMorph Phase-1 analyzer: AST structure, metrics, complexity.",
    )
    parser.add_argument("path", type=pathlib.Path, help="Python source file to analyze")
    parser.add_argument("--json", action="store_true", help="emit the full report as JSON")
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

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        _print_text_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())