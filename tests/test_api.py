"""Tests for backend.api (Phase 9a: FastAPI backend).

No test touches the network: the LLM endpoint is exercised via the
NOT_CONFIGURED path, an injected fake provider, and a monkeypatched
HTTP layer.
"""
from __future__ import annotations

import json
import urllib.error

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.routes import MAX_UPLOAD_BYTES
from backend.migration.llm_migrator import ProviderResponse

SOURCE = "def f(x):\n    return x + 1"
API_SOURCE = "import os\n\n\ndef f(x):\n    y = 1\n    return x + 1\n"
LEGACY = "import os\n\n\ndef greet(name):\n    return 'Hello %s!' % (name,)\n"


@pytest.fixture()
def client():
    return TestClient(create_app())


@pytest.fixture()
def clean_llm_env(monkeypatch):
    for name in (
        "CODEMORPH_LLM_PROVIDER", "CODEMORPH_LLM_API_KEY",
        "CODEMORPH_LLM_MODEL", "CODEMORPH_LLM_BASE_URL",
        "CODEMORPH_LLM_TIMEOUT", "CODEMORPH_EXEC_TIMEOUT",
        "CODEMORPH_RESULTS_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


# -- health ------------------------------------------------------------------------


def test_health_reports_status_and_provider(client, clean_llm_env):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["llm_provider"] == "none"
    assert data["llm_configured"] is False
    assert "api_key" not in json.dumps(data).lower()


# -- analysis ------------------------------------------------------------------------


def test_analyze_returns_full_report(client):
    resp = client.post(
        "/api/analyze", json={"source": API_SOURCE, "filename": "inline.py"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["filename"] == "inline.py"
    assert data["metrics"]["num_functions"] == 1
    assert data["metrics"]["num_imports"] == 1
    assert data["metrics"]["total_lines"] == 6
    assert data["metrics"]["code_lines"] == 4
    # lexical: unused import + unused variable; flow: dead store
    assert [f["category"] for f in data["findings"]] == [
        "UNUSED_IMPORT", "UNUSED_VARIABLE",
    ]
    assert [f["category"] for f in data["flow_findings"]] == ["DEAD_STORE"]
    assert "Function: f" in data["structure"]
    assert len(data["cfgs"]) == 1
    assert data["data_flows"][0]["qualified_name"] == "f"
    assert data["complexity"]["functions"][0]["complexity"] == 1


def test_analyze_syntax_error_is_422(client):
    resp = client.post(
        "/api/analyze", json={"source": "def broken(:\n", "filename": "bad.py"}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "syntax"
    assert detail["line"] == 1
    assert detail["filename"] == "bad.py"
    assert detail["message"]


def test_analyze_empty_source_is_rejected(client):
    resp = client.post("/api/analyze", json={"source": "", "filename": "x.py"})
    assert resp.status_code == 422


# -- upload ---------------------------------------------------------------------------


def test_upload_analyze_valid_file(client):
    content = b"def add(a, b):\n    return a + b\n"
    resp = client.post(
        "/api/analyze/upload",
        files={"file": ("sample.py", content, "text/x-python")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["filename"] == "sample.py"
    assert data["metrics"]["num_functions"] == 1
    assert data["findings"] == []


def test_upload_rejects_wrong_extension(client):
    resp = client.post(
        "/api/analyze/upload",
        files={"file": ("data.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 400


def test_upload_rejects_binary_content(client):
    resp = client.post(
        "/api/analyze/upload",
        files={"file": ("blob.py", b"\x80\x81\x82", "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_upload_rejects_oversized_file(client):
    content = b"# padding\n" + b"x" * MAX_UPLOAD_BYTES
    resp = client.post(
        "/api/analyze/upload",
        files={"file": ("big.py", content, "text/x-python")},
    )
    assert resp.status_code == 413


# -- repository --------------------------------------------------------------------------


def test_repository_endpoint(client, tmp_path):
    (tmp_path / "a.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import a\n", encoding="utf-8")
    resp = client.post("/api/repository", json={"path": str(tmp_path)})
    assert resp.status_code == 200
    data = resp.json()
    assert data["files_analyzed"] == 2
    assert data["totals"]["lines_code"] == 2
    assert data["dependencies"] == [
        {"source": "b.py", "target": "a.py", "module": "a"}
    ]


def test_repository_missing_path_is_400(client):
    resp = client.post("/api/repository", json={"path": "/nonexistent/xyz"})
    assert resp.status_code == 400


# -- deterministic migration -----------------------------------------------------------------


def test_migrate_deterministic(client):
    resp = client.post(
        "/api/migrate", json={"source": LEGACY, "filename": "legacy.py"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["applied"] is True
    assert [t["kind"] for t in data["transformations"]] == [
        "UNUSED_IMPORT_REMOVAL", "PERCENT_TO_FSTRING",
    ]
    assert 'f"Hello {name}!"' in data["migrated_source"]
    assert data["syntax_valid"] is True
    assert data["structural_guard_passed"] is True


def test_migrate_clean_source_is_noop(client):
    resp = client.post(
        "/api/migrate",
        json={"source": "def add(a, b):\n    return a + b\n",
              "filename": "clean.py"},
    )
    data = resp.json()
    assert data["applied"] is False
    assert data["transformations"] == []


def test_migrate_syntax_error_is_422(client):
    resp = client.post(
        "/api/migrate", json={"source": "def broken(:\n", "filename": "bad.py"}
    )
    assert resp.status_code == 422


# -- LLM migration ------------------------------------------------------------------------------


def test_llm_migrate_not_configured(client, clean_llm_env):
    resp = client.post(
        "/api/llm-migrate", json={"source": SOURCE, "filename": "inline.py"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "NOT_CONFIGURED"
    assert data["provider"] == "none"
    assert data["migrated_source"] == SOURCE


def test_llm_migrate_accepts_valid_generation(client, monkeypatch):
    class FakeProvider:
        name = "fake"
        model = "fake-1"

        def generate(self, prompt):
            return ProviderResponse(
                ok=True,
                text="```python\n" + SOURCE + "\n```",
                error=None,
                model="fake-1",
            )

    monkeypatch.setattr(
        "backend.migration.llm_migrator.create_provider",
        lambda *args, **kwargs: FakeProvider(),
    )
    resp = client.post(
        "/api/llm-migrate", json={"source": SOURCE, "filename": "inline.py"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ACCEPTED"
    assert data["model"] == "fake-1"
    assert data["equivalence"]["score"] == 100
    assert data["equivalence"]["verification"]["total"] == 3


def test_llm_migrate_key_never_exposed(client, monkeypatch):
    monkeypatch.setenv("CODEMORPH_LLM_PROVIDER", "openai")
    monkeypatch.setenv("CODEMORPH_LLM_API_KEY", "sk-api-secret-42")

    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    resp = client.post(
        "/api/llm-migrate", json={"source": SOURCE, "filename": "inline.py"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "PROVIDER_ERROR"
    assert "sk-api-secret-42" not in resp.text


# -- verification & equivalence --------------------------------------------------------------------


def test_verify_identical_sources(client):
    resp = client.post(
        "/api/verify",
        json={"original": SOURCE, "migrated": SOURCE, "filename": "inline.py"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["score"] == 100
    assert data["label"] == "very-high"
    assert data["estimate"] is True
    assert data["verification"]["total"] == 3
    assert data["verification"]["passed"] == 3


def test_verify_flags_behavior_change(client):
    migrated = "def f(x):\n    return x + 2"
    resp = client.post(
        "/api/verify",
        json={"original": SOURCE, "migrated": migrated, "filename": "inline.py"},
    )
    data = resp.json()
    assert data["score"] == 83
    assert data["verification"]["failed"] == 2
    assert data["verification"]["passed"] == 1


def test_verify_static_only(client):
    resp = client.post(
        "/api/verify",
        json={
            "original": SOURCE, "migrated": SOURCE,
            "filename": "inline.py", "run_tests": False,
        },
    )
    data = resp.json()
    assert data["verification"] is None
    assert any("static signals only" in note for note in data["notes"])


# -- full pipeline -----------------------------------------------------------------------------------


def test_pipeline_end_to_end(client):
    resp = client.post(
        "/api/pipeline", json={"source": LEGACY, "filename": "legacy.py"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["migration"]["applied"] is True
    assert 'f"Hello {name}!"' in data["migration"]["migrated_source"]
    assert data["analysis"]["metrics"]["num_functions"] == 1
    assert data["analysis"]["findings"][0]["category"] == "UNUSED_IMPORT"
    assert data["findings_before"] == 1
    assert data["findings_after"] == 0
    assert data["equivalence"]["score"] == 100
    # greet has 4 generated cases (normal/boundary/empty/invalid), all pass
    assert data["equivalence"]["verification"]["passed"] == 4


# -- diff -----------------------------------------------------------------------------------------------


def test_diff_identical(client):
    text = "a\nb\n"
    resp = client.post("/api/diff", json={"original": text, "migrated": text})
    data = resp.json()
    assert data["summary"] == {
        "same": 2, "changed": 0, "added": 0, "removed": 0,
        "old_lines": 2, "new_lines": 2,
    }
    assert [r["type"] for r in data["rows"]] == ["same", "same"]


def test_diff_changed_line(client):
    resp = client.post(
        "/api/diff", json={"original": "a\nb\n", "migrated": "a\nc\n"}
    )
    data = resp.json()
    changed = [r for r in data["rows"] if r["type"] == "changed"]
    assert len(changed) == 1
    assert changed[0]["old"] == 2 and changed[0]["new"] == 2
    assert changed[0]["old_text"] == "b"
    assert changed[0]["new_text"] == "c"
    assert data["summary"]["changed"] == 1


def test_diff_added_and_removed_lines(client):
    added = client.post(
        "/api/diff", json={"original": "a\n", "migrated": "a\nb\n"}
    ).json()
    assert added["summary"]["added"] == 1
    assert added["summary"]["removed"] == 0
    added_row = next(r for r in added["rows"] if r["type"] == "added")
    assert added_row["new"] == 2 and added_row["old_text"] is None

    removed = client.post(
        "/api/diff", json={"original": "a\nb\n", "migrated": "a\n"}
    ).json()
    assert removed["summary"]["removed"] == 1
    removed_row = next(r for r in removed["rows"] if r["type"] == "removed")
    assert removed_row["old"] == 2 and removed_row["new_text"] is None


# -- experiments ------------------------------------------------------------------------------------------


def test_experiments_empty_when_none_stored(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEMORPH_RESULTS_DIR", str(tmp_path))
    resp = client.get("/api/experiments")
    assert resp.status_code == 200
    data = resp.json()
    assert data["experiments"] == []
    assert "no stored experiment results" in data["note"]


def test_experiments_returns_stored_results(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEMORPH_RESULTS_DIR", str(tmp_path))
    (tmp_path / "experiments.json").write_text(
        json.dumps([{"name": "trial", "score": 91}]), encoding="utf-8"
    )
    resp = client.get("/api/experiments")
    data = resp.json()
    assert data["experiments"] == [{"name": "trial", "score": 91}]