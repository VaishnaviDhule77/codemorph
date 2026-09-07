"""CLI for the CodeMorph evaluation framework (Phase 10).

Usage::

    python -m backend.evaluation                              # scripted calibration
    python -m backend.evaluation --provider openai            # real experiment
    python -m backend.evaluation --tasks percent_format,bare_except
    python -m backend.evaluation --provider scripted --json

Results are written to benchmark/results/ (experiments.json is what the
Research panel reads) -- measured values only, never fabricated.
"""
from __future__ import annotations

import argparse
import json
import sys

from ..migration.llm_migrator import NullProvider, create_provider
from ..verification.sandbox import SandboxConfig
from .benchmark import BENCHMARK_TASKS, get_task
from .experiment import ExperimentRunner, ScriptedProvider

SCRIPTED_NOTE = (
    "SCRIPTED CALIBRATION: deterministic identity responses; this run "
    "validates the measurement harness, NOT model behavior."
)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codemorph-eval",
        description="CodeMorph research evaluation: LLM-only vs "
                    "static-analysis+LLM on a benchmark of migration tasks.",
    )
    parser.add_argument(
        "--provider", default="scripted", choices=["scripted", "openai"],
        help="scripted = deterministic calibration (default); openai = real "
             "experiment (requires CODEMORPH_LLM_* env configuration)",
    )
    parser.add_argument(
        "--tasks", default="",
        help="comma-separated benchmark task names (default: all 8)",
    )
    parser.add_argument(
        "--results-dir", default=None,
        help="output directory (default: benchmark/results)",
    )
    parser.add_argument("--label", default="", help="run label")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    if args.provider == "scripted":
        provider = ScriptedProvider()
        note = SCRIPTED_NOTE
    else:
        provider = create_provider()
        if isinstance(provider, NullProvider):
            print(
                "error: provider 'openai' requested but the environment is "
                "not configured (set CODEMORPH_LLM_PROVIDER=openai and "
                "CODEMORPH_LLM_API_KEY)",
                file=sys.stderr,
            )
            return 1
        note = "live provider run"

    if args.tasks.strip():
        try:
            tasks = [get_task(name.strip()) for name in args.tasks.split(",")]
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    else:
        tasks = list(BENCHMARK_TASKS)

    runner = ExperimentRunner(
        provider=provider,
        sandbox_config=SandboxConfig.from_env(),
        results_dir=args.results_dir,
    )
    result = runner.run_experiment(tasks=tasks, label=args.label, note=note)
    written = result.save()

    if args.json:
        payload = result.to_dict()
        payload["written_files"] = [str(p) for p in written]
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Experiment: {args.label or '(unlabeled)'}")
    print(f"Tasks: {', '.join(result.tasks)}")
    print()
    print(result.comparison_table())
    print()
    print("wrote:")
    for path in written:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())