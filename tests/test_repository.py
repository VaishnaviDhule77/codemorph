"""Tests for backend.repository (Phase 8).

All expectations are hand-derived from the sample repository built by
``build_sample_repo``: per-file line counts and finding lines, dependency
edges, fan-in/out, and risk arithmetic (risk = 3*HIGH + 2*MEDIUM + 1*LOW
+ max function complexity + fan-in).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.repository import (
    RepositoryError,
    analyze_repository,
    discover_python_files,
    render_repository,
)
from backend.repository.__main__ import main as repo_main


MODELS = '''"""Data models."""


MAX_TOTAL = 100


class Order:
    def __init__(self, amount):
        self.amount = amount

    def total(self):
        return self.amount * 2


def describe(order):
    return "order: " + str(order.amount)
'''

SERVICES = '''"""Business services."""

import os

import models


def process_payment(order, discount: float = 0.0):
    """Compute the payment total."""
    amount = order.amount
    if amount < 0:
        raise ValueError("negative amount")
    total = amount * 1.2
    if discount:
        total = total - discount
    if total > models.MAX_TOTAL:
        total = models.MAX_TOTAL
    return total


def load_order(path):
    handle = open(path)
    data = int(handle.read())
    handle.close()
    return data
'''

API = '''"""HTTP API layer."""

import json

import services


def create_order(payload):
    order = json.loads(payload)
    amount = int(order["amount"])
    return services.process_payment({"amount": amount})
'''

HELPERS = '''"""Helper functions."""

import os


def clamp(value, low, high):
    if value < low:
        return low
    if value > high:
        return high
    return value


def run_expression(source):
    return eval(source)
'''

UTILS_INIT = '"""Utility package."""\n'
BROKEN = "def broken(:\n    pass\n"


def build_sample_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "models.py").write_text(MODELS, encoding="utf-8")
    (root / "services.py").write_text(SERVICES, encoding="utf-8")
    (root / "api.py").write_text(API, encoding="utf-8")
    (root / "broken.py").write_text(BROKEN, encoding="utf-8")
    (root / "binary.py").write_bytes(b"\x80\x81\x82 not utf8\n")
    utils = root / "utils"
    utils.mkdir()
    (utils / "__init__.py").write_text(UTILS_INIT, encoding="utf-8")
    (utils / "helpers.py").write_text(HELPERS, encoding="utf-8")
    cache = root / "__pycache__"
    cache.mkdir()
    (cache / "junk.py").write_text("x = 1\n", encoding="utf-8")
    venv = root / ".venv"
    venv.mkdir()
    (venv / "site.py").write_text("y = 2\n", encoding="utf-8")


@pytest.fixture()
def sample_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    build_sample_repo(root)
    return root


@pytest.fixture()
def sample_report(sample_repo: Path):
    return analyze_repository(sample_repo)


# -- discovery -----------------------------------------------------------------


def test_discover_finds_and_sorts_py_files(sample_repo):
    files = discover_python_files(sample_repo)
    assert [f.relative_to(sample_repo).as_posix() for f in files] == [
        "api.py", "binary.py", "broken.py", "models.py", "services.py",
        "utils/__init__.py", "utils/helpers.py",
    ]


def test_discover_excludes_junk_directories(sample_repo):
    names = {f.name for f in discover_python_files(sample_repo)}
    assert "junk.py" not in names      # __pycache__/
    assert "site.py" not in names      # .venv/


def test_discover_rejects_missing_root(tmp_path):
    with pytest.raises(RepositoryError):
        discover_python_files(tmp_path / "missing")


def test_discover_rejects_file_as_root(tmp_path):
    target = tmp_path / "not_a_dir.py"
    target.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(RepositoryError):
        discover_python_files(target)


def test_discover_empty_repo_returns_no_files(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    assert discover_python_files(root) == []


# -- aggregates ------------------------------------------------------------------


def test_repository_aggregates(sample_report):
    # 7 .py files discovered (junk dirs pruned); broken.py fails to parse,
    # binary.py fails to decode; metrics cover the 5 analyzable files.
    # lines: 16+25+11+15+1 = 68 total; code 9+19+7+10+1 = 46.
    # (models.py: docstring, 2 blanks, MAX_TOTAL, 2 blanks,
    # class block, 2 blanks, describe block = 16 lines.)
    assert sample_report.files_discovered == 7
    assert sample_report.files_analyzed == 5
    assert sample_report.files_with_errors == 2
    assert sample_report.total_functions == 8    # 3+2+1+2+0
    assert sample_report.total_methods == 2
    assert sample_report.total_classes == 1
    assert sample_report.total_imports == 5      # 0+2+2+0+1
    assert sample_report.total_lines == 68
    assert sample_report.total_code_lines == 46


def test_repository_finding_counts(sample_report):
    # models 1 LOW; services 2 MEDIUM + 1 LOW; api 2 LOW; helpers 1 HIGH + 1 MEDIUM
    assert len(sample_report.findings) == 8
    assert sample_report.severity_counts() == {"HIGH": 1, "MEDIUM": 3, "LOW": 4}


def test_file_statuses_and_errors(sample_report):
    by_path = {f.path: f for f in sample_report.files}
    assert by_path["broken.py"].status == "parse_error"
    assert by_path["broken.py"].error                # message present
    assert by_path["binary.py"].status == "read_error"
    assert by_path["binary.py"].error
    assert by_path["binary.py"].metrics is None      # error files carry no metrics
    assert by_path["models.py"].status == "ok"
    assert by_path["models.py"].error is None


# -- per-file findings (hand-derived line numbers) ------------------------------------


def test_services_findings(sample_report):
    summary = sample_report.file("services.py")
    assert [(f.line, f.category, f.severity.value) for f in summary.findings] == [
        (3, "UNUSED_IMPORT", "MEDIUM"),            # os never read (models is)
        (22, "MISSING_ERROR_HANDLING", "MEDIUM"),  # open(path) unguarded
        (23, "MISSING_ERROR_HANDLING", "LOW"),     # int(...) unguarded
    ]
    assert summary.metrics.code_lines == 19
    assert summary.metrics.num_functions == 2
    assert summary.metrics.num_imports == 2
    assert summary.max_complexity == 4             # process_payment: 1 + 3 ifs


def test_models_findings(sample_report):
    summary = sample_report.file("models.py")
    # MAX_TOTAL is never read within models.py itself (services.py reads it
    # cross-file, which per-module usage analysis cannot see).
    assert [(f.line, f.category, f.severity.value) for f in summary.findings] == [
        (4, "UNUSED_VARIABLE", "LOW"),
    ]


def test_helpers_findings(sample_report):
    summary = sample_report.file("utils/helpers.py")
    assert [(f.line, f.category, f.severity.value) for f in summary.findings] == [
        (3, "UNUSED_IMPORT", "MEDIUM"),
        (15, "DANGEROUS_EVAL", "HIGH"),
    ]


def test_api_findings(sample_report):
    summary = sample_report.file("api.py")
    assert [(f.line, f.category, f.severity.value) for f in summary.findings] == [
        (9, "MISSING_ERROR_HANDLING", "LOW"),   # json.loads
        (10, "MISSING_ERROR_HANDLING", "LOW"),  # int()
    ]


# -- cross-file dependencies ------------------------------------------------------------


def test_dependency_edges(sample_report):
    assert [(e.source, e.target, e.module) for e in sample_report.dependencies] == [
        ("api.py", "services.py", "services"),
        ("services.py", "models.py", "models"),
    ]


def test_fan_in_and_fan_out(sample_report):
    fan = {f.path: (f.fan_in, f.fan_out) for f in sample_report.files}
    assert fan["models.py"] == (1, 0)
    assert fan["services.py"] == (1, 1)
    assert fan["api.py"] == (0, 1)
    assert fan["utils/__init__.py"] == (0, 0)
    assert fan["utils/helpers.py"] == (0, 0)
    assert fan["broken.py"] == (0, 0)
    assert fan["binary.py"] == (0, 0)


def test_relative_and_dotted_imports_resolve(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "models.py").write_text("X = 1\n", encoding="utf-8")
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from .helpers import clamp\n", encoding="utf-8"
    )
    (pkg / "helpers.py").write_text(
        "from ..models import X\n\n\ndef clamp(v):\n    return v\n",
        encoding="utf-8",
    )
    (root / "top.py").write_text(
        "from pkg.helpers import clamp\n", encoding="utf-8"
    )
    report = analyze_repository(root)
    edges = {(e.source, e.target): e.module for e in report.dependencies}
    # Importing pkg.helpers also executes pkg/__init__.py: ancestor package
    # inits count as dependency targets (documented semantics).
    assert edges == {
        ("pkg/__init__.py", "pkg/helpers.py"): "helpers",
        ("pkg/helpers.py", "models.py"): "models",
        ("top.py", "pkg/helpers.py"): "pkg.helpers",
        ("top.py", "pkg/__init__.py"): "pkg.helpers",
    }


def test_plain_package_import_resolves_to_init(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    utils = root / "utils"
    utils.mkdir()
    (utils / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    (root / "main.py").write_text("import utils\n", encoding="utf-8")
    report = analyze_repository(root)
    assert [(e.source, e.target, e.module) for e in report.dependencies] == [
        ("main.py", "utils/__init__.py", "utils")
    ]


def test_external_imports_create_no_edges(sample_report):
    modules = {e.module for e in sample_report.dependencies}
    assert "os" not in modules
    assert "json" not in modules


# -- risk ranking ------------------------------------------------------------------------


def test_risk_scores(sample_report):
    # services: 2*2+1 findings + 4 complexity + 1 fan-in = 10
    # helpers: 3+2 findings + 3 complexity + 0 = 8
    # api: 2 findings + 1 complexity + 0 = 3; models: 1 + 1 + 1 = 3
    risk = {f.path: f.risk_score for f in sample_report.files}
    assert risk == {
        "api.py": 3,
        "binary.py": 0,
        "broken.py": 0,
        "models.py": 3,
        "services.py": 10,
        "utils/__init__.py": 0,
        "utils/helpers.py": 8,
    }


def test_high_risk_ranking_order(sample_report):
    ranked = sample_report.high_risk_files(limit=3)
    assert [f.path for f in ranked] == [
        "services.py", "utils/helpers.py", "api.py",
    ]


def test_error_files_excluded_from_ranking(sample_report):
    ranked = sample_report.high_risk_files(limit=10)
    assert all(f.ok for f in ranked)
    assert len(ranked) == 5
    assert "broken.py" not in {f.path for f in ranked}
    assert "binary.py" not in {f.path for f in ranked}


def test_highest_complexity(sample_report):
    assert sample_report.highest_complexity() == (
        "services.py", "process_payment", 4
    )


def test_highest_complexity_none_for_empty_repo(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    assert analyze_repository(root).highest_complexity() is None


# -- flow findings included ------------------------------------------------------------------


def test_flow_sensitive_findings_are_included(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "dead.py").write_text(
        "def f(x):\n    y = 1\n    return x\n", encoding="utf-8"
    )
    report = analyze_repository(root)
    summary = report.file("dead.py")
    # Both engines fire: the Phase-2 lexical unused-variable rule AND the
    # Phase-3 flow-sensitive dead-store rule.
    assert [(f.line, f.category, f.severity.value) for f in summary.findings] == [
        (2, "DEAD_STORE", "LOW"),
        (2, "UNUSED_VARIABLE", "LOW"),
    ]


# -- rendering & serialization ------------------------------------------------------------------


def test_render_repository(sample_report):
    text = render_repository(sample_report)
    assert "Files: 7 discovered | 5 analyzed | 2 with errors" in text
    assert "Functions: 8 (2 methods) | Classes: 1 | Imports: 5" in text
    assert "Lines: 68 total | 46 code" in text
    assert "Findings: 8 total (1 high, 3 medium, 4 low)" in text
    assert "api.py -> services.py" in text
    assert "services.py :: process_payment (complexity 4)" in text
    assert "broken.py: parse_error:" in text
    assert "binary.py: read_error:" in text


def test_to_dict_serializable(sample_report):
    data = json.loads(json.dumps(sample_report.to_dict()))
    assert data["files_discovered"] == 7
    assert data["files_analyzed"] == 5
    assert data["files_with_errors"] == 2
    assert data["totals"]["functions"] == 8
    assert data["totals"]["lines_code"] == 46
    assert data["severity_counts"] == {"HIGH": 1, "MEDIUM": 3, "LOW": 4}
    assert data["highest_complexity"]["function"] == "process_payment"
    assert data["high_risk_files"][0] == {"path": "services.py", "risk_score": 10}
    assert len(data["files"]) == 7
    assert {f["path"] for f in data["files"]} >= {"utils/helpers.py", "broken.py"}


# -- CLI ------------------------------------------------------------------------------------------


def test_cli_text_report(sample_repo, capsys):
    assert repo_main([str(sample_repo)]) == 0
    out = capsys.readouterr().out
    assert f"Repository Analysis: {sample_repo}" in out
    assert "Files: 7 discovered | 5 analyzed | 2 with errors" in out
    assert "High-risk files" in out


def test_cli_json_report(sample_repo, capsys):
    assert repo_main([str(sample_repo), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["files_analyzed"] == 5
    assert data["totals"]["functions"] == 8
    assert data["severity_counts"]["HIGH"] == 1


def test_cli_missing_root_returns_error(capsys):
    assert repo_main(["/nonexistent/repo/xyz"]) == 2
    assert "error" in capsys.readouterr().err