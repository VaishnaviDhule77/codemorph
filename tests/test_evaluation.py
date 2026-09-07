"""Tests for backend.evaluation (Phase 10).

No test touches the network: the calibration provider is deterministic,
and the divergence tests inject scripted responses engineered to expose
the measured difference between the two conditions.
"""
from __future__ import annotations

import csv
import json
import pathlib

import pytest

from backend.analyzer import analyze_source
from backend.analyzer.findings import Finding, Severity
from backend.evaluation import (
    BENCHMARK_TASKS,
    BenchmarkTask,
    ExperimentRunner,
    ScriptedProvider,
    build_bare_prompt,
)
from backend.evaluation.__main__ import main as eval_main
from backend.evaluation.experiment import SUMMARY_FIELDS
from backend.migration.llm_migrator import (
    LLMMigrationStatus,
    ProviderResponse,
    _FENCE_RE,
    build_migration_prompt,
)
from backend.verification import SandboxConfig
from backend.verification.test_generator import generate_tests


DOUBLE = "def double(x):\n    return x * 2\n"
TRIPLE = "def triple(x):\n    return x * 3\n"
PARSE = (
    "def parse_flag(text):\n"
    "    try:\n"
    "        return int(text)\n"
    "    except:\n"
    "        return 0\n"
)
# Behavior-changed "modernization" of PARSE: -1 instead of 0 on failure.
CHANGED_PARSE = (
    "def parse_flag(text):\n"
    "    try:\n"
    "        return int(text)\n"
    "    except Exception:\n"
    "        return -1\n"
)


def task(name: str, source: str) -> BenchmarkTask:
    return BenchmarkTask(name=name, description="", source=source)


class DivergenceProvider:
    """Scripted divergence: parse_flag -> changed code, triple -> broken
    syntax, everything else -> identity (echo the prompt's source)."""

    name = "diverge"
    model = "diverge-1"

    def generate(self, prompt: str) -> ProviderResponse:
        if "parse_flag" in prompt:
            body = CHANGED_PARSE
        elif "triple" in prompt:
            body = "def broken(:"
        else:
            blocks = list(_FENCE_RE.finditer(prompt))
            body = blocks[-1].group("body").strip("\n")
        return ProviderResponse(
            ok=True, text="```python\n" + body + "\n```",
            error=None, model=self.model,
        )


class NoCodeProvider:
    name = "nocode"
    model = "nocode-1"

    def generate(self, prompt: str) -> ProviderResponse:
        return ProviderResponse(
            ok=True, text="Sorry, I cannot help with that.",
            error=None, model=self.model,
        )


def run(tasks, provider, results_dir=None):
    runner = ExperimentRunner(
        provider=provider,
        sandbox_config=SandboxConfig(timeout=20),
        results_dir=results_dir,
    )
    return runner.run_experiment(tasks=tasks, label="test", note="unit-test run")


def summary_of(result, method):
    return next(r for r in result.summary if r["method"] == method)


def outcome_of(result, task_name, method):
    return next(
        o for o in result.outcomes if o.task == task_name and o.method == method
    )


# -- benchmark -----------------------------------------------------------------


def test_benchmark_tasks_are_valid_and_testable():
    assert len(BENCHMARK_TASKS) == 8
    for task in BENCHMARK_TASKS:
        analysis = analyze_source(task.source, filename=f"{task.name}.py")
        cases = generate_tests(analysis)
        assert cases, f"task {task.name} has no testable functions"
        assert analysis.metrics.num_functions >= 1


def test_benchmark_task_names_unique():
    names = [t.name for t in BENCHMARK_TASKS]
    assert len(names) == len(set(names))


# -- prompts ----------------------------------------------------------------------


def test_bare_prompt_is_minimal():
    prompt = build_bare_prompt(BENCHMARK_TASKS[0])
    assert BENCHMARK_TASKS[0].source in prompt
    assert "```python" in prompt
    for section in ("CONSTRAINTS", "METRICS", "FINDINGS", "STRUCTURE SUMMARY"):
        assert section not in prompt, f"bare prompt must not contain {section}"


def test_prompts_differ_in_analysis_context():
    task = BENCHMARK_TASKS[3]  # unused_imports
    analysis = analyze_source(task.source, filename="t.py")
    # Finding.severity is the Severity enum (the prompt formatter calls
    # .value on it); a plain string violates the model contract.
    findings = [Finding(file="t.py", line=1, category="UNUSED_IMPORT",
                        severity=Severity.MEDIUM, message="m", suggestion="s")]
    rich = build_migration_prompt(task.source, analysis, findings)
    for section in ("CONSTRAINTS", "METRICS", "FINDINGS", "STRUCTURE SUMMARY"):
        assert section in rich
    assert "UNUSED_IMPORT" in rich


# -- calibration: identity responses must measure perfect -------------------------------


def test_calibration_identity_run():
    result = run([task("double", DOUBLE), task("triple", TRIPLE)],
                 ScriptedProvider())
    for method in ("llm_only", "codemorph"):
        record = summary_of(result, method)
        # identity migrations: everything passes, nothing rejected
        assert record["tasks"] == 2
        assert record["syntax_success_pct"] == 100.0
        assert record["migration_success_pct"] == 100.0
        # double: 3 decisive cases (int param: empty dedups with boundary);
        # triple: 3 decisive cases; all pass in both conditions.
        assert record["test_pass_pct"] == 100.0
        assert record["avg_equivalence_pct"] == 100.0
        assert record["introduced_defects"] == 0
        assert record["rejection_rate_pct"] == 0.0


def test_calibration_outcome_details():
    result = run([task("double", DOUBLE)], ScriptedProvider())
    llm = outcome_of(result, "double", "llm_only")
    assert llm.status == "RAW"
    assert llm.accepted and not llm.flagged
    assert llm.guard_passed is None
    # extract_code strips fenced blocks at both ends, so GENERATED output
    # has no trailing newline; only safe-fallback output keeps the
    # original verbatim (see the codemorph rejection test below).
    assert llm.final_source == DOUBLE.rstrip("\n")
    assert llm.test_total == 3 and llm.test_passed == 3
    morph = outcome_of(result, "double", "codemorph")
    assert morph.status == LLMMigrationStatus.ACCEPTED
    assert morph.guard_passed is True
    assert morph.accepted and not morph.flagged
    assert morph.test_total == 3 and morph.test_passed == 3


# -- divergence: engineered flaws, measured differences ---------------------------------


def test_divergence_llm_only_summary():
    tasks = [task("double", DOUBLE), task("parse_flag", PARSE),
             task("triple", TRIPLE)]
    result = run(tasks, DivergenceProvider())
    record = summary_of(result, "llm_only")
    # LLM-only ships everything as-is: broken syntax for triple counts
    # against it, the behavior change counts against it.
    assert record["syntax_success_pct"] == pytest.approx(66.7, abs=0.1)
    assert record["migration_success_pct"] == 100.0     # accepts all output
    # decisive: double 3 + parse_flag 4 (str param: 4 distinct categories)
    # + triple 0 (broken output: no test run) = 7; passed: double's 3 only.
    assert record["test_pass_pct"] == pytest.approx(42.9, abs=0.1)
    # equivalence: double 1.0, parse_flag 0.7083, triple 0.0 (invalid)
    assert record["avg_equivalence_pct"] == pytest.approx(56.9, abs=0.1)
    assert record["introduced_defects"] == 2
    assert record["rejection_rate_pct"] == 0.0


def test_divergence_codemorph_summary():
    tasks = [task("double", DOUBLE), task("parse_flag", PARSE),
             task("triple", TRIPLE)]
    result = run(tasks, DivergenceProvider())
    record = summary_of(result, "codemorph")
    # triple's broken generation is REJECTED -> original returned -> the
    # final output always compiles; parse_flag's subtle behavior change
    # passes the gates but is flagged by the differential tests.
    assert record["syntax_success_pct"] == 100.0
    assert record["migration_success_pct"] == pytest.approx(66.7, abs=0.1)
    # decisive: double 3 + parse_flag 4 + triple 3 (original, all pass) = 10;
    # passed: double 3 + parse_flag 0 + triple 3 = 6.
    assert record["test_pass_pct"] == pytest.approx(60.0, abs=0.1)
    # equivalence: double 1.0, parse_flag 0.7083, triple 1.0 (safe fallback)
    assert record["avg_equivalence_pct"] == pytest.approx(90.3, abs=0.1)
    assert record["introduced_defects"] == 1   # the behavior change slips in
    assert record["rejection_rate_pct"] == pytest.approx(33.3, abs=0.1)


def test_divergence_outcome_details():
    tasks = [task("double", DOUBLE), task("parse_flag", PARSE),
             task("triple", TRIPLE)]
    result = run(tasks, DivergenceProvider())

    llm_parse = outcome_of(result, "parse_flag", "llm_only")
    assert llm_parse.status == "RAW"
    # generated code is stripped of the trailing newline by extract_code
    assert llm_parse.final_source == CHANGED_PARSE.rstrip("\n")
    assert llm_parse.test_failed == 4 and llm_parse.test_passed == 0
    # structural 1.0, control-flow 1.0, data-flow 5/6 (uses +externals gain
    # 'Exception'), test behavior 0/4 -> (1+1+0.8333+0)/4 = 0.7083
    assert llm_parse.equivalence_score == pytest.approx(0.70833, abs=1e-3)
    assert llm_parse.introduced_defect

    morph_parse = outcome_of(result, "parse_flag", "codemorph")
    # passes syntax + structural guard, so ACCEPTED -- but FLAGGED by tests
    assert morph_parse.status == LLMMigrationStatus.ACCEPTED
    assert morph_parse.flagged is True
    assert morph_parse.introduced_defect
    assert morph_parse.test_failed == 4

    llm_triple = outcome_of(result, "triple", "llm_only")
    assert llm_triple.final_syntax_valid is False
    assert llm_triple.equivalence_label == "invalid"
    assert llm_triple.introduced_defect
    assert llm_triple.test_total == 0    # no verification on invalid syntax

    morph_triple = outcome_of(result, "triple", "codemorph")
    assert morph_triple.status == LLMMigrationStatus.INVALID_SYNTAX
    assert morph_triple.final_source == TRIPLE    # safe fallback: verbatim
    assert morph_triple.introduced_defect is False
    assert morph_triple.test_passed == 3
    assert morph_triple.flagged is False


def test_no_extraction_counts_as_defect():
    result = run([task("double", DOUBLE)], NoCodeProvider())

    llm = outcome_of(result, "double", "llm_only")
    assert llm.status == "NO_CODE"
    assert llm.generated is False
    assert llm.final_source == ""
    assert llm.final_syntax_valid is False
    assert llm.introduced_defect
    # empty final: static-only estimate, well below 1.0
    # structural (0+1+1)/3, control-flow 0, data-flow 0 -> 0.2222
    assert llm.equivalence_score == pytest.approx(2 / 9, abs=1e-3)

    morph = outcome_of(result, "double", "codemorph")
    assert morph.status == LLMMigrationStatus.NO_CODE
    assert morph.final_source == DOUBLE     # safe fallback
    assert morph.introduced_defect is False
    assert morph.test_passed == 3


# -- persistence ------------------------------------------------------------------------


def test_persistence_roundtrip(tmp_path):
    result = run([task("double", DOUBLE)], ScriptedProvider(),
                 results_dir=tmp_path)
    written = result.save(tmp_path)
    paths = {pathlib.Path(p).name for p in written}

    json_path = tmp_path / "experiments.json"
    assert "experiments.json" in paths and json_path.is_file()
    records = json.loads(json_path.read_text(encoding="utf-8"))
    assert isinstance(records, list) and len(records) == 2
    assert {r["method"] for r in records} == {"llm_only", "codemorph"}
    for record in records:
        assert set(record) == set(SUMMARY_FIELDS)
        assert record["tasks"] == 1
        assert record["syntax_success_pct"] == 100.0
        assert record["introduced_defects"] == 0
    assert records[0]["note"] == "unit-test run"

    with (tmp_path / "experiments.csv").open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    assert {r["method"] for r in rows} == {"llm_only", "codemorph"}

    runs_dir = tmp_path / "runs"
    detail_files = list(runs_dir.glob("*.json"))
    assert len(detail_files) == 1
    details = json.loads(detail_files[0].read_text(encoding="utf-8"))
    assert len(details["outcomes"]) == 2
    assert details["outcomes"][0]["task"] == "double"


def test_comparison_table_renders():
    result = run([task("double", DOUBLE), task("triple", TRIPLE)],
                 ScriptedProvider())
    table = result.comparison_table()
    assert "Method" in table and "Defects" in table
    assert "llm_only" in table and "codemorph" in table
    assert "100.0" in table
    assert "scripted" in table


def test_runner_rejects_empty_tasks():
    with pytest.raises(ValueError):
        run([], ScriptedProvider())


# -- CLI ------------------------------------------------------------------------------------


def test_cli_scripted_run(tmp_path, capsys):
    code = eval_main([
        "--provider", "scripted",
        "--tasks", "percent_format",
        "--results-dir", str(tmp_path),
        "--label", "cli-test",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "llm_only" in out and "codemorph" in out
    assert "SCRIPTED CALIBRATION" in out
    assert "100.0" in out
    records = json.loads(
        (tmp_path / "experiments.json").read_text(encoding="utf-8")
    )
    assert len(records) == 2
    assert records[0]["tasks"] == 1


def test_cli_unknown_task(capsys):
    assert eval_main(["--tasks", "nope"]) == 2
    assert "unknown benchmark task" in capsys.readouterr().err


def test_cli_unknown_provider(capsys):
    # argparse rejects invalid --provider choices by exiting with code 2
    # (standard behavior, same as the other CodeMorph CLIs).
    with pytest.raises(SystemExit) as excinfo:
        eval_main(["--provider", "bogus"])
    assert excinfo.value.code == 2