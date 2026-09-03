"""CLI for CodeMorph's LLM-assisted migration (Phase 7).

Usage::

    python -m backend.migration file.py                # print the prompt only
    python -m backend.migration file.py --llm          # run the gated pipeline
    python -m backend.migration file.py --llm --json   # machine-readable result
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from ..analyzer.ast_analyzer import SourceParseError
from ..analyzer.service import analyze_source
from ..verification.equivalence import render_equivalence
from .llm_migrator import (
    LLMMigrator,
    LLMMigrationStatus,
    build_migration_prompt,
    collect_all_findings,
)


def _print_result(result) -> None:
    print(f"LLM migration: {result.status}")
    suffix = f" (model: {result.model})" if result.model else ""
    print(f"  provider: {result.provider}{suffix}")
    if result.rejection_reason:
        print(f"  reason: {result.rejection_reason}")
    if result.status == LLMMigrationStatus.NOT_CONFIGURED:
        print(
            "  to enable: set CODEMORPH_LLM_PROVIDER=openai and "
            "CODEMORPH_LLM_API_KEY (see .env.example)"
        )
    if result.findings_before is not None:
        print(f"  findings: {result.findings_before} -> {result.findings_after}")
    if result.equivalence is not None:
        print()
        print(render_equivalence(result.equivalence))
    for warning in result.warnings:
        print(f"  WARNING: {warning}")
    if result.accepted:
        print()
        print("Migrated source:")
        print(result.migrated_source, end="")
        print()


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codemorph-llm",
        description="CodeMorph LLM-assisted migration: analysis-context "
                    "prompt, gated generation, verification, equivalence.",
    )
    parser.add_argument("path", type=pathlib.Path, help="Python source file")
    parser.add_argument(
        "--llm", action="store_true",
        help="run the LLM migration pipeline (requires provider configuration)",
    )
    parser.add_argument(
        "--prompt-only", action="store_true",
        help="print the migration prompt without calling any provider (default)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    try:
        source = args.path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {args.path}: {exc}", file=sys.stderr)
        return 2

    try:
        analysis = analyze_source(source, filename=args.path.name)
    except SourceParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not args.llm:
        prompt = build_migration_prompt(
            source, analysis, collect_all_findings(analysis, args.path.name)
        )
        if args.json:
            print(json.dumps(
                {"filename": args.path.name, "prompt": prompt,
                 "provider_called": False},
                indent=2,
            ))
        else:
            print(
                f"Migration prompt for {args.path.name} "
                f"(no provider will be called):"
            )
            print()
            print(prompt, end="")
        return 0

    result = LLMMigrator().migrate(source, filename=args.path.name)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0
    _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())