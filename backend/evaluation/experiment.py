"""Research evaluation framework (Phase 10): benchmark + comparison runner.

Implements the experiment mode required by the project spec: run two
migration approaches on a benchmark of small Python modernization tasks
and measure the dependent variables from actual executions -- numbers are
never fabricated.

Conditions (the independent variable)
-------------------------------------
``llm_only``  -- the LLM receives only the task sentence and the source.
               Its extracted output ships as-is: no analysis context, no
               constraints, no gates, no verification.
``codemorph`` -- the Phase-7 pipeline: analysis-context prompt (metrics,
               findings, structure, machine-checkable constraints) plus
               the full rejection pipeline (syntax gate -> structural
               guard -> sandboxed differential tests -> equivalence). A
               rejected generation falls back to the ORIGINAL source.

Both conditions use the SAME provider instance, so any measured
difference is attributable to the static-analysis harness around the
LLM -- the research hypothesis, isolated.

Dependent variables (per spec)
------------------------------
syntax success rate (of the condition's FINAL output), migration
success rate, differential test pass rate, average behavioral-
equivalence estimate, introduced defects, rejection rate, average
processing time.

Defect definition (documented, deliberate): a task counts as an
introduced defect when the condition's FINAL output fails to compile or
diverges on any decisive differential test case. A CodeMorph rejection
that safely returns the original is NOT a defect -- that fallback is the
safety property under test.

Honesty model
-------------
``--provider scripted`` runs a deterministic identity provider that
echoes the source back. It validates the measurement harness end-to-end
and writes results labeled SCRIPTED CALIBRATION -- never as model
behavior. Real experiments use ``--provider openai``. The results store
starts empty; /api/experiments and the Research panel show only what was
actually measured, with its label.
"""
from __future__ import annotations

import csv
import json
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from ..migration.llm_migrator import (
    _FENCE_RE,
    extract_code,
    LLMMigrator,
    LLMMigrationStatus,
    ProviderResponse,
)
from ..verification import compute_equivalence
from ..verification.sandbox import SandboxConfig
from ..verification.syntax_checker import check_syntax
from .benchmark import BENCHMARK_TASKS, BenchmarkTask

if TYPE_CHECKING:
    from ..migration.llm_migrator import LLMProvider
    from ..verification.equivalence import EquivalenceReport

DEFAULT_RESULTS_DIR = Path("benchmark") / "results"

TASK_SENTENCE = (
    "TASK: modernize the Python module below. Preserve its behavior "
    "exactly; improve only its form."
)
OUTPUT_SENTENCE = (
    "OUTPUT FORMAT: Respond with the complete migrated module in a single "
    "```python fenced block. No prose, no explanations."
)

SUMMARY_FIELDS = (
    "method", "mode", "model", "timestamp", "tasks",
    "syntax_success_pct", "migration_success_pct", "test_pass_pct",
    "avg_equivalence_pct", "introduced_defects", "rejection_rate_pct",
    "avg_duration_s", "note",
)

_METHODS = ("llm_only", "codemorph")


# --- prompts --------------------------------------------------------------------


def build_bare_prompt(task: BenchmarkTask) -> str:
    """The LLM-only prompt: task sentence + source. Nothing else."""
    return (
        TASK_SENTENCE
        + "\n\nSOURCE CODE\n```python\n"
        + task.source
        + "```\n\n"
        + OUTPUT_SENTENCE
        + "\n"
    )


# --- the scripted calibration provider ---------------------------------------------


class ScriptedProvider:
    """Deterministic identity provider (no network).

    Echoes the prompt's fenced source back as the "migration". Used to
    validate the measurement harness: with identity responses, every
    metric must come out perfect. Results from this provider are labeled
    SCRIPTED CALIBRATION and must never be read as model behavior.
    """

    name = "scripted"
    model = "scripted-identity"

    def generate(self, prompt: str) -> ProviderResponse:
        blocks = list(_FENCE_RE.finditer(prompt))
        if not blocks:
            return ProviderResponse(
                ok=False, text="",
                error="scripted: no fenced source found in prompt",
                model=self.model,
            )
        source = blocks[-1].group("body").strip("\n")
        return ProviderResponse(
            ok=True,
            text="```python\n" + source + "\n```",
            error=None,
            model=self.model,
        )


# --- models ------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskOutcome:
    """Measured outcome of one (task, method) pair."""

    task: str
    method: str
    status: str
    generated: bool
    accepted: bool
    generation_syntax_valid: "bool | None"
    final_syntax_valid: bool
    guard_passed: "bool | None"
    flagged: bool
    final_source: str
    test_total: int
    test_passed: int
    test_failed: int
    test_errors: int
    equivalence_score: float
    equivalence_label: str
    introduced_defect: bool
    duration: float

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "method": self.method,
            "status": self.status,
            "generated": self.generated,
            "accepted": self.accepted,
            "generation_syntax_valid": self.generation_syntax_valid,
            "final_syntax_valid": self.final_syntax_valid,
            "guard_passed": self.guard_passed,
            "flagged": self.flagged,
            "final_source": self.final_source,
            "test_total": self.test_total,
            "test_passed": self.test_passed,
            "test_failed": self.test_failed,
            "test_errors": self.test_errors,
            "equivalence_score": round(self.equivalence_score, 4),
            "equivalence_label": self.equivalence_label,
            "introduced_defect": self.introduced_defect,
            "duration": round(self.duration, 3),
        }


@dataclass
class ExperimentResult:
    """One complete experiment run over the benchmark."""

    label: str
    mode: str
    model: "str | None"
    timestamp: str
    note: str
    tasks: "list[str]"
    outcomes: "list[TaskOutcome]"
    summary: "list[dict]"
    results_dir: Path = DEFAULT_RESULTS_DIR

    # -- rendering -------------------------------------------------------

    def comparison_table(self) -> str:
        header = (
            f"{'Method':<10} {'Tasks':>5} {'Syntax%':>8} {'Migr%':>7} "
            f"{'Tests%':>7} {'Equiv%':>7} {'Defects':>8} {'Reject%':>8} "
            f"{'Time(s)':>8}"
        )
        lines = [header, "-" * len(header)]
        for record in self.summary:
            lines.append(
                f"{record['method']:<10} {record['tasks']:>5} "
                f"{record['syntax_success_pct']:>8.1f} "
                f"{record['migration_success_pct']:>7.1f} "
                f"{record['test_pass_pct']:>7.1f} "
                f"{record['avg_equivalence_pct']:>7.1f} "
                f"{record['introduced_defects']:>8} "
                f"{record['rejection_rate_pct']:>8.1f} "
                f"{record['avg_duration_s']:>8.3f}"
            )
        lines.append("")
        lines.append(f"mode: {self.mode}  model: {self.model or '-'}")
        lines.append(f"note: {self.note}")
        lines.append(
            "All numbers above are measured from executed runs; "
            "equivalence is an estimate, not a proof."
        )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "mode": self.mode,
            "model": self.model,
            "timestamp": self.timestamp,
            "note": self.note,
            "tasks": list(self.tasks),
            "summary": self.summary,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }

    # -- persistence ------------------------------------------------------

    def save(self, results_dir: "Path | None" = None) -> "list[Path]":
        """Write experiments.json (latest summary, what the UI reads),
        experiments.csv, and a timestamped full-detail record."""
        directory = Path(results_dir if results_dir is not None else self.results_dir)
        runs_dir = directory / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)

        json_path = directory / "experiments.json"
        json_path.write_text(
            json.dumps(self.summary, indent=2) + "\n", encoding="utf-8"
        )

        csv_path = directory / "experiments.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
            writer.writeheader()
            for record in self.summary:
                writer.writerow(record)

        safe_ts = self.timestamp.replace(":", "-")
        safe_label = re.sub(r"[^A-Za-z0-9_-]+", "-", self.label) or "run"
        details_path = runs_dir / f"{safe_ts}-{safe_label}.json"
        details_path.write_text(
            json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        return [json_path, csv_path, details_path]


# --- the runner -----------------------------------------------------------------------


class ExperimentRunner:
    """Runs both conditions over the benchmark and measures everything."""

    def __init__(
        self,
        provider: "LLMProvider",
        sandbox_config: "SandboxConfig | None" = None,
        results_dir: "Path | None" = None,
    ) -> None:
        self.provider = provider
        self.sandbox = sandbox_config if sandbox_config is not None else SandboxConfig.from_env()
        self.results_dir = Path(results_dir) if results_dir is not None else DEFAULT_RESULTS_DIR

    def run_experiment(
        self,
        tasks: "Sequence[BenchmarkTask] | None" = None,
        label: str = "",
        note: str = "",
    ) -> ExperimentResult:
        """Run both conditions over ``tasks`` and aggregate the metrics."""
        selected = list(BENCHMARK_TASKS if tasks is None else tasks)
        if not selected:
            raise ValueError("no benchmark tasks selected")

        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        outcomes: "list[TaskOutcome]" = []
        for task in selected:
            for method in _METHODS:
                outcomes.append(self._run_condition(task, method))
        summary = [
            self._summarize(method, outcomes, note, timestamp)
            for method in _METHODS
        ]
        return ExperimentResult(
            label=label,
            mode=self.provider.name,
            model=getattr(self.provider, "model", None),
            timestamp=timestamp,
            note=note,
            tasks=[t.name for t in selected],
            outcomes=outcomes,
            summary=summary,
            results_dir=self.results_dir,
        )

    # -- one (task, method) measurement --------------------------------------

    def _run_condition(self, task: BenchmarkTask, method: str) -> TaskOutcome:
        filename = f"{task.name}.py"
        started = time.perf_counter()

        if method == "llm_only":
            response = self.provider.generate(build_bare_prompt(task))
            if response.ok:
                code, _note = extract_code(response.text)
                status = "RAW" if code is not None else "NO_CODE"
            else:
                code, status = None, "PROVIDER_ERROR"
            generated = code is not None
            final = code if generated else ""
            generation_syntax = (
                check_syntax(code, filename=filename).valid if generated else False
            )
            final_syntax = (
                check_syntax(final, filename=filename).valid if final else False
            )
            accepted = generated
            guard_passed = None
            flagged = False
        else:
            result = LLMMigrator(
                provider=self.provider, sandbox_config=self.sandbox
            ).migrate(task.source, filename=filename)
            generated = result.extracted_code is not None
            final = result.migrated_source
            generation_syntax = (
                result.syntax_check.valid if result.syntax_check is not None else None
            )
            final_syntax = (
                check_syntax(final, filename=filename).valid if final else False
            )
            accepted = result.accepted
            flagged = result.flagged
            status = result.status
            if status in (
                LLMMigrationStatus.NOT_CONFIGURED,
                LLMMigrationStatus.PROVIDER_ERROR,
                LLMMigrationStatus.NO_CODE,
                LLMMigrationStatus.INVALID_SYNTAX,
            ):
                guard_passed = None
            else:
                guard_passed = status == LLMMigrationStatus.ACCEPTED

        # Uniform measurement for BOTH conditions: equivalence estimate
        # (which includes sandboxed differential testing) of original vs
        # the condition's final output.
        equivalence = compute_equivalence(
            task.source, final, filename=filename, sandbox_config=self.sandbox
        )
        verification = equivalence.verification
        test_total = verification.total if verification else 0
        test_passed = verification.passed if verification else 0
        test_failed = verification.failed if verification else 0
        test_errors = verification.errors if verification else 0
        introduced_defect = (not final_syntax) or test_failed > 0
        duration = time.perf_counter() - started

        return TaskOutcome(
            task=task.name,
            method=method,
            status=status,
            generated=generated,
            accepted=accepted,
            generation_syntax_valid=generation_syntax,
            final_syntax_valid=final_syntax,
            guard_passed=guard_passed,
            flagged=flagged,
            final_source=final,
            test_total=test_total,
            test_passed=test_passed,
            test_failed=test_failed,
            test_errors=test_errors,
            equivalence_score=equivalence.score,
            equivalence_label=equivalence.label,
            introduced_defect=introduced_defect,
            duration=duration,
        )

    # -- aggregation ------------------------------------------------------------

    def _summarize(
        self, method: str, outcomes: "list[TaskOutcome]", note: str, timestamp: str
    ) -> dict:
        rows = [o for o in outcomes if o.method == method]
        count = len(rows)
        syntax_ok = sum(1 for o in rows if o.final_syntax_valid)
        accepted = sum(1 for o in rows if o.accepted)
        decisive = sum(o.test_passed + o.test_failed for o in rows)
        passed = sum(o.test_passed for o in rows)
        defects = sum(1 for o in rows if o.introduced_defect)
        rejections = (
            sum(1 for o in rows if not o.accepted) if method == "codemorph" else 0
        )
        durations = [o.duration for o in rows]
        return {
            "method": method,
            "mode": self.provider.name,
            "model": getattr(self.provider, "model", None),
            "timestamp": timestamp,
            "tasks": count,
            "syntax_success_pct": round(100.0 * syntax_ok / count, 1) if count else 0.0,
            "migration_success_pct": round(100.0 * accepted / count, 1) if count else 0.0,
            "test_pass_pct": round(100.0 * passed / decisive, 1) if decisive else 0.0,
            "avg_equivalence_pct": round(
                100.0 * statistics.fmean(
                    o.equivalence_score for o in rows
                ), 1,
            ) if rows else 0.0,
            "introduced_defects": defects,
            "rejection_rate_pct": round(100.0 * rejections / count, 1) if count else 0.0,
            "avg_duration_s": round(statistics.fmean(durations), 3) if durations else 0.0,
            "note": note,
        }