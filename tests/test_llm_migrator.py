"""Tests for backend.migration.llm_migrator (Phase 7).

No test touches the network: the OpenAI provider's HTTP layer is
monkeypatched, and every pipeline test injects a deterministic fake
provider.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from backend.analyzer import Finding, Severity, analyze_source
from backend.migration import LLMMigrator, LLMMigrationStatus
from backend.migration.__main__ import main
from backend.migration.llm_migrator import (
    DEFAULT_MODEL,
    NullProvider,
    OpenAIProvider,
    ProviderResponse,
    _format_findings,
    build_migration_prompt,
    collect_all_findings,
    create_provider,
    extract_code,
)
from backend.verification import SandboxConfig

SOURCE = "def f(x):\n    return x + 1"


def fence(code: str) -> str:
    return "```python\n" + code + "\n```"


class FakeProvider:
    name = "fake"
    model = "fake-1"

    def __init__(self, text: str = "", ok: bool = True,
                 error: "str | None" = None) -> None:
        self.text = text
        self.ok = ok
        self.error = error
        self.prompts: "list[str]" = []

    def generate(self, prompt: str) -> ProviderResponse:
        self.prompts.append(prompt)
        if self.ok:
            return ProviderResponse(
                ok=True, text=self.text, error=None, model=self.model
            )
        return ProviderResponse(ok=False, text="", error=self.error, model=None)


def migrate_with(text, source, filename="inline.py", ok=True, error=None,
                 timeout=20.0):
    provider = FakeProvider(text, ok=ok, error=error)
    migrator = LLMMigrator(
        provider=provider, sandbox_config=SandboxConfig(timeout=timeout)
    )
    return migrator.migrate(source, filename=filename), provider


@pytest.fixture()
def clean_llm_env(monkeypatch):
    for name in (
        "CODEMORPH_LLM_PROVIDER", "CODEMORPH_LLM_API_KEY",
        "CODEMORPH_LLM_MODEL", "CODEMORPH_LLM_BASE_URL",
        "CODEMORPH_LLM_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)


# -- prompt construction ---------------------------------------------------------


def test_prompt_contains_all_sections():
    analysis = analyze_source(SOURCE, filename="inline.py")
    prompt = build_migration_prompt(SOURCE, analysis, [])
    for section in (
        "TASK:", "CONSTRAINTS", "METRICS", "FINDINGS",
        "STRUCTURE SUMMARY", "SOURCE CODE", "OUTPUT FORMAT",
    ):
        assert section in prompt
    assert "def f(x):" in prompt
    assert "```python" in prompt
    assert "single ```python fenced block" in prompt
    assert "(no findings)" in prompt


def test_prompt_includes_findings_and_structure(smelly_source):
    analysis = analyze_source(smelly_source, filename="smelly.py")
    findings = collect_all_findings(analysis, "smelly.py")
    prompt = build_migration_prompt(smelly_source, analysis, findings)
    assert "UNUSED_IMPORT" in prompt
    assert "DEAD_STORE" in prompt
    assert "is never used in this module" in prompt
    assert "function classify(value)" in prompt
    assert "function circle_area(radius)" in prompt
    assert "- functions: 9 (0 methods)" in prompt
    assert "max 22" in prompt


def test_prompt_is_deterministic(smelly_source):
    first = analyze_source(smelly_source, filename="smelly.py")
    second = analyze_source(smelly_source, filename="smelly.py")
    assert build_migration_prompt(
        smelly_source, first, collect_all_findings(first, "smelly.py")
    ) == build_migration_prompt(
        smelly_source, second, collect_all_findings(second, "smelly.py")
    )


def test_format_findings_caps_at_40():
    findings = [
        Finding(
            file="f.py", line=i, category="X", severity=Severity.LOW,
            message=f"m{i}", suggestion="s",
        )
        for i in range(45)
    ]
    text = _format_findings(findings)
    assert "... and 5 more findings" in text
    assert text.count("line ") == 40


# -- response parsing ---------------------------------------------------------------


def test_extract_code_python_fence():
    text = "Here you go:\n```python\ndef f(x):\n    return x\n```\nDone."
    code, note = extract_code(text)
    assert code == "def f(x):\n    return x"
    assert "python" in note


def test_extract_code_generic_fence():
    code, _ = extract_code("```\ndef f(x):\n    return x\n```")
    assert code == "def f(x):\n    return x"


def test_extract_code_prefers_python_fence():
    text = "```text\nsome prose\n```\n```python\ndef g():\n    pass\n```"
    code, _ = extract_code(text)
    assert code == "def g():\n    pass"


def test_extract_code_without_fence_returns_none():
    code, note = extract_code("I cannot provide that code.")
    assert code is None
    assert "no fenced code block" in note


def test_extract_code_empty_fence_returns_none():
    code, note = extract_code("```python\n\n```")
    assert code is None
    assert "empty" in note


# -- provider factory ------------------------------------------------------------------


def test_create_provider_default_none():
    provider = create_provider(env={})
    assert isinstance(provider, NullProvider)
    response = provider.generate("prompt")
    assert response.ok is False
    assert "CODEMORPH_LLM_PROVIDER" in response.error


def test_create_provider_openai_missing_key():
    provider = create_provider(env={"CODEMORPH_LLM_PROVIDER": "openai"})
    assert isinstance(provider, NullProvider)
    assert "CODEMORPH_LLM_API_KEY" in provider.generate("x").error


def test_create_provider_openai_configured():
    provider = create_provider(env={
        "CODEMORPH_LLM_PROVIDER": "openai",
        "CODEMORPH_LLM_API_KEY": "sk-test",
        "CODEMORPH_LLM_MODEL": "gpt-custom",
        "CODEMORPH_LLM_TIMEOUT": "12.5",
    })
    assert isinstance(provider, OpenAIProvider)
    assert provider.model == "gpt-custom"
    assert provider._api_key == "sk-test"
    assert provider.base_url == "https://api.openai.com/v1"
    assert provider.timeout == 12.5
    default_model = create_provider(env={
        "CODEMORPH_LLM_PROVIDER": "openai",
        "CODEMORPH_LLM_API_KEY": "k",
    })
    assert default_model.model == DEFAULT_MODEL


def test_create_provider_unknown_name():
    provider = create_provider(env={"CODEMORPH_LLM_PROVIDER": "wat"})
    assert isinstance(provider, NullProvider)
    error = provider.generate("x").error
    assert "unknown provider 'wat'" in error
    assert "openai" in error


# -- OpenAI provider HTTP layer (monkeypatched, no network) -------------------------------


class _FakeHTTPResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_openai_generate_success(monkeypatch):
    provider = OpenAIProvider("sk-test", model="gpt-custom", timeout=5.0)
    content = "```python\ndef f(x):\n    return x\n```"
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        captured["timeout"] = timeout
        return _FakeHTTPResponse({"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    response = provider.generate("MIGRATE THIS")
    assert response.ok is True
    assert response.text == content
    assert response.model == "gpt-custom"
    assert response.error is None

    request = captured["request"]
    assert request.full_url == "https://api.openai.com/v1/chat/completions"
    assert request.headers.get("Authorization") == "Bearer sk-test"
    assert captured["timeout"] == 5.0
    body = json.loads(request.data.decode("utf-8"))
    assert body["model"] == "gpt-custom"
    assert body["temperature"] == 0
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][1]["content"] == "MIGRATE THIS"


def test_openai_generate_http_error_no_key_leak(monkeypatch):
    provider = OpenAIProvider("sk-secret-123")

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", None, io.BytesIO(b"")
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    response = provider.generate("prompt")
    assert response.ok is False
    assert "401" in response.error
    assert "sk-secret-123" not in response.error


def test_openai_generate_network_error(monkeypatch):
    provider = OpenAIProvider("sk-x")

    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection timed out")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    response = provider.generate("prompt")
    assert response.ok is False
    assert "timed out" in response.error


# -- the gated pipeline (fake providers) ---------------------------------------------------


def test_migrate_not_configured(clean_llm_env):
    migrator = LLMMigrator(sandbox_config=SandboxConfig(timeout=20))
    result = migrator.migrate(SOURCE, filename="inline.py")
    assert result.status == LLMMigrationStatus.NOT_CONFIGURED
    assert result.provider == "none"
    assert result.migrated_source == SOURCE
    assert result.rejection_reason and "CODEMORPH_LLM_PROVIDER" in result.rejection_reason
    assert result.equivalence is None
    assert result.accepted is False


def test_migrate_provider_error():
    result, _ = migrate_with(
        "", source=SOURCE, ok=False, error="boom: upstream 500"
    )
    assert result.status == LLMMigrationStatus.PROVIDER_ERROR
    assert result.rejection_reason == "boom: upstream 500"
    assert result.migrated_source == SOURCE
    assert result.raw_response is None


def test_migrate_no_code_in_response():
    result, _ = migrate_with("Sorry, I cannot help with that.", source=SOURCE)
    assert result.status == LLMMigrationStatus.NO_CODE
    assert "no fenced code block" in result.rejection_reason
    assert result.migrated_source == SOURCE


def test_migrate_invalid_syntax_rejected():
    result, _ = migrate_with(fence("def broken(:"), source=SOURCE)
    assert result.status == LLMMigrationStatus.INVALID_SYNTAX
    assert result.syntax_check is not None and result.syntax_check.valid is False
    assert result.syntax_check.error_line == 1
    assert result.rejection_reason and "line 1" in result.rejection_reason
    assert result.migrated_source == SOURCE
    assert result.equivalence is None


def test_migrate_structural_rejection():
    result, _ = migrate_with(
        fence("def g(x):\n    return x"), source=SOURCE
    )
    assert result.status == LLMMigrationStatus.STRUCTURAL_REJECTION
    assert result.guard_reason and "function" in result.guard_reason
    assert result.rejection_reason == result.guard_reason
    assert result.migrated_source == SOURCE
    assert result.equivalence is None


def test_migrate_identical_code_accepted():
    result, provider = migrate_with(fence(SOURCE), source=SOURCE)
    assert result.status == LLMMigrationStatus.ACCEPTED
    assert result.accepted and not result.flagged
    assert result.migrated_source == SOURCE
    assert result.extracted_code == SOURCE
    assert result.warnings == ()
    assert result.equivalence is not None
    assert result.equivalence.score_percent == 100
    assert result.equivalence.verification.total == 3
    assert result.equivalence.verification.passed == 3
    assert len(provider.prompts) == 1
    assert "def f(x):" in provider.prompts[0]
    assert result.findings_before == 0 and result.findings_after == 0


def test_migrate_behavior_change_flagged():
    original = "def f(x):\n    return x + 1"
    changed = "def f(x):\n    return x + 2"
    result, _ = migrate_with(fence(changed), source=original)
    assert result.status == LLMMigrationStatus.ACCEPTED
    assert result.flagged is True
    assert result.migrated_source == changed
    assert any("FAILED" in w for w in result.warnings)
    assert result.equivalence.score_percent == 83
    assert result.equivalence.verification.failed == 2


def test_migrate_findings_reduction_analysis_again():
    original = "import os\n\n\ndef f(x):\n    return x + 1\n"
    improved = "def f(x):\n    return x + 1\n"
    result, _ = migrate_with(fence(improved), source=original)
    assert result.status == LLMMigrationStatus.ACCEPTED
    assert result.findings_before == 1      # UNUSED_IMPORT in the original
    assert result.findings_after == 0
    assert result.equivalence.score_percent == 100
    assert not result.flagged


def test_migrate_result_serialization():
    result, _ = migrate_with(fence(SOURCE), source=SOURCE)
    data = json.loads(json.dumps(result.to_dict()))
    assert data["status"] == "ACCEPTED"
    assert data["accepted"] is True
    assert data["flagged"] is False
    assert data["provider"] == "fake"
    assert data["model"] == "fake-1"
    assert data["findings_before"] == 0
    assert data["findings_after"] == 0
    assert data["equivalence"]["score"] == 100
    assert data["warnings"] == []
    assert "def f(x):" in data["prompt"]
    assert "api_key" not in json.dumps(data)


def test_api_key_never_leaks_into_results(monkeypatch):
    provider = OpenAIProvider("sk-secret-123")

    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = LLMMigrator(provider=provider).migrate(SOURCE, filename="inline.py")
    assert result.status == LLMMigrationStatus.PROVIDER_ERROR
    blob = json.dumps(result.to_dict())
    assert "sk-secret-123" not in blob
    assert "sk-secret-123" not in result.prompt
    assert "sk-secret-123" not in (result.raw_response or "")


# -- CLI ---------------------------------------------------------------------------------------


def test_cli_prompt_only_by_default(tmp_path, capsys):
    sample = tmp_path / "sample.py"
    sample.write_text("def f(x):\n    return x + 1\n", encoding="utf-8")
    assert main([str(sample)]) == 0
    out = capsys.readouterr().out
    assert "Migration prompt for sample.py" in out
    assert "no provider will be called" in out
    assert "CONSTRAINTS" in out
    assert "OUTPUT FORMAT" in out


def test_cli_llm_not_configured(tmp_path, capsys, clean_llm_env):
    sample = tmp_path / "sample.py"
    sample.write_text("def f(x):\n    return x + 1\n", encoding="utf-8")
    assert main([str(sample), "--llm"]) == 0
    out = capsys.readouterr().out
    assert "LLM migration: NOT_CONFIGURED" in out
    assert "CODEMORPH_LLM_PROVIDER" in out


def test_cli_llm_json_not_configured(tmp_path, capsys, clean_llm_env):
    source = "def f(x):\n    return x + 1\n"
    sample = tmp_path / "sample.py"
    sample.write_text(source, encoding="utf-8")
    assert main([str(sample), "--llm", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "NOT_CONFIGURED"
    assert data["provider"] == "none"
    assert data["migrated_source"] == source
    assert "prompt" in data