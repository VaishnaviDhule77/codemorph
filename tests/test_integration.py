"""Cross-process integration tests (Phase 11): the real CLIs, as launched.

Each test runs `python -m backend.<package>` in a subprocess with the
repository root as cwd, capturing stdout/stderr and the exit code --
covering module imports, argparse wiring, and process-level behavior the
in-process suites never touch.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

LEGACY = (
    "def greet(name):\n"
    "    return 'Hello %s!' % (name,)\n"
    "\n"
    "\n"
    "def bump(x):\n"
    "    x = x + 1\n"
    "    x = x + 1\n"
    "    x = x + 1\n"
    "    return x\n"
)


def run_cli(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=180,
    )


def write_sample(tmp_path: Path, source: str, name: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def test_analyzer_equivalence_cli_end_to_end(tmp_path):
    # greet + bump: 2 transformations (percent->f-string + collapse),
    # 7/7 sandboxed cases, equivalence 98% (all pinned by earlier phases).
    sample = write_sample(tmp_path, LEGACY, "legacy.py")
    proc = run_cli(["backend.analyzer", str(sample), "--equivalence"])
    assert proc.returncode == 0, proc.stderr
    assert "Deterministic migrations: 2 transformation(s), applied" in proc.stdout
    assert "Semantic Equivalence Estimate: 98% (very-high)" in proc.stdout
    assert "(7/7 cases passed)" in proc.stdout
    assert "NOT a formal proof" in proc.stdout


def test_analyzer_json_cli(tmp_path):
    sample = write_sample(tmp_path, "def add(a, b):\n    return a + b\n", "add.py")
    proc = run_cli(["backend.analyzer", str(sample), "--json"])
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["metrics"]["num_functions"] == 1
    assert data["complexity"]["functions"][0]["complexity"] == 1
    assert "structure" in data and len(data["cfgs"]) == 1
    assert len(data["data_flows"]) == 1


def test_migration_cli_prompt_only(tmp_path):
    sample = write_sample(tmp_path, "def add(a, b):\n    return a + b\n", "add.py")
    proc = run_cli(["backend.migration", str(sample)])
    assert proc.returncode == 0, proc.stderr
    assert "Migration prompt for add.py" in proc.stdout
    assert "no provider will be called" in proc.stdout
    assert "CONSTRAINTS" in proc.stdout


def test_repository_cli_analyzes_codemorph_itself():
    # Dog-food: the repository CLI analyzes this very repository.
    proc = run_cli(["backend.repository", ".", "--top", "3"])
    assert proc.returncode == 0, proc.stderr
    assert "Repository Analysis:" in proc.stdout
    assert "Files:" in proc.stdout
    assert "analyzed" in proc.stdout
    assert "High-risk files" in proc.stdout


def test_evaluation_cli_writes_results(tmp_path):
    proc = run_cli([
        "backend.evaluation", "--provider", "scripted",
        "--tasks", "percent_format",
        "--results-dir", str(tmp_path), "--label", "itest",
    ])
    assert proc.returncode == 0, proc.stderr
    assert "SCRIPTED CALIBRATION" in proc.stdout
    assert "llm_only" in proc.stdout and "codemorph" in proc.stdout
    records = json.loads((tmp_path / "experiments.json").read_text("utf-8"))
    assert len(records) == 2
    assert {r["method"] for r in records} == {"llm_only", "codemorph"}
    assert all(r["syntax_success_pct"] == 100.0 for r in records)
    assert records[0]["mode"] == "scripted"