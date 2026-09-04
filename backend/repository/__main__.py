"""CLI for CodeMorph repository-level analysis (Phase 8).

Usage::

    python -m backend.repository ./path/to/repo [--json] [--top N]
"""
from __future__ import annotations

import argparse
import json
import sys

from .analysis import RepositoryError, analyze_repository, render_repository


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codemorph-repo",
        description="CodeMorph repository-level analysis: discovery, "
                    "per-file analysis, dependencies, risk ranking.",
    )
    parser.add_argument("root", help="path to the repository root")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--top", type=int, default=5, metavar="N",
        help="number of high-risk files to list (default 5)",
    )
    args = parser.parse_args(argv)

    try:
        report = analyze_repository(args.root)
    except RepositoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    print(render_repository(report, top=args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())