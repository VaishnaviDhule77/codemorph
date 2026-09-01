"""Tests for backend.verification.sandbox (the isolation boundary)."""
from __future__ import annotations

import os

from backend.verification import Sandbox, SandboxConfig


def test_executes_program_and_returns_payload():
    program = (
        "import json\n"
        "print('__CODEMORPH_RESULT__' + json.dumps({'n': 1}))\n"
    )
    run = Sandbox(SandboxConfig(timeout=10)).run_program(program)
    assert run.ok is True
    assert run.payload == {"n": 1}
    assert run.timed_out is False
    assert run.exit_code == 0
    assert run.error is None


def test_stdout_before_marker_is_preserved():
    program = (
        'print("noise")\n'
        "import json\n"
        "print('__CODEMORPH_RESULT__' + json.dumps({'n': 2}))\n"
    )
    run = Sandbox(SandboxConfig(timeout=10)).run_program(program)
    assert run.ok is True
    assert run.payload == {"n": 2}
    assert "noise" in run.stdout


def test_timeout_kills_runaway_program():
    run = Sandbox(SandboxConfig(timeout=2)).run_program(
        "while True:\n    pass\n"
    )
    assert run.ok is False
    assert run.timed_out is True
    assert run.payload is None
    assert run.error and "timeout" in run.error.lower()


def test_environment_is_not_inherited(monkeypatch):
    # The host's environment (including any API key) must never reach the
    # analyzed program.
    monkeypatch.setenv("CODEMORPH_SECRET_TOKEN", "s3cret")
    program = (
        "import json, os\n"
        "print('__CODEMORPH_RESULT__' + "
        "json.dumps({'leaked': 'CODEMORPH_SECRET_TOKEN' in os.environ}))\n"
    )
    run = Sandbox(SandboxConfig(timeout=10)).run_program(program)
    assert run.ok is True
    assert run.payload == {"leaked": False}


def test_working_directory_is_isolated():
    program = (
        "import json, os\n"
        "print('__CODEMORPH_RESULT__' + json.dumps({'cwd': os.getcwd()}))\n"
    )
    run = Sandbox(SandboxConfig(timeout=10)).run_program(program)
    assert run.ok is True
    assert "codemorph-run-" in run.payload["cwd"]
    assert os.path.realpath(run.payload["cwd"]) != os.path.realpath(os.getcwd())


def test_crashing_program_reports_stderr():
    run = Sandbox(SandboxConfig(timeout=10)).run_program(
        'raise RuntimeError("kaboom")\n'
    )
    assert run.ok is False
    assert run.timed_out is False
    assert run.exit_code == 1
    assert "kaboom" in run.stderr
    assert run.error == "result marker missing"


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("CODEMORPH_EXEC_TIMEOUT", "7")
    assert SandboxConfig.from_env().timeout == 7
    monkeypatch.setenv("CODEMORPH_EXEC_TIMEOUT", "not-a-number")
    assert SandboxConfig.from_env().timeout == 5
    monkeypatch.delenv("CODEMORPH_EXEC_TIMEOUT", raising=False)
    assert SandboxConfig.from_env().timeout == 5