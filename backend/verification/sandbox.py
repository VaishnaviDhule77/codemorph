"""Sandboxed execution of analyzed/generated code (Phase 5).

Security model (and its honest limits):

Enforced
--------
* Separate process: code never runs inside the CodeMorph process.
* ``python -I`` (isolated mode): interpreter ignores ``PYTHON*``
  environment variables and the user site-packages directory.
* Near-empty environment: the child receives only ``PATH`` (plus
  ``SYSTEMROOT`` on Windows). Host environment variables -- including any
  API key -- never pass through.
* Private working directory: the child runs in a fresh temporary
  directory, so relative-path reads/writes land in scratch space.
* Wall-clock timeout: runaway code is killed.
* POSIX resource limits (best effort): address space, CPU seconds, and
  file size; the child also gets its own session (``setsid``).
* The program is delivered via stdin, not argv, so analyzed source never
  appears in the process list.

NOT enforced (documented limitations)
-------------------------------------
* No OS-level container: with an absolute path the child can still read
  the host filesystem, and ``import os`` works inside the sandbox. Full
  isolation requires a container/jail runner (future work).
* Only the direct child is killed on timeout; grandchildren of tested
  code are not tracked (the separate session limits the blast radius but
  does not guarantee cleanup).
* Network access is not blocked.
* Non-POSIX platforms skip the resource limits; timeout, environment,
  and cwd isolation still apply.

Result protocol: the child prints one line
``__CODEMORPH_RESULT__<json>``; output before that line is program noise
and is kept for diagnostics.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

try:
    import resource
except ImportError:  # non-POSIX platform
    resource = None  # type: ignore[assignment]

_RESULT_MARKER = "__CODEMORPH_RESULT__"


@dataclass(frozen=True)
class SandboxConfig:
    """Tunables for one sandboxed run."""

    timeout: float = 5.0          # wall-clock seconds
    memory_mb: int = 512          # RLIMIT_AS (POSIX only)
    file_size_mb: int = 1         # RLIMIT_FSIZE (POSIX only)

    @classmethod
    def from_env(cls) -> "SandboxConfig":
        """Read ``CODEMORPH_EXEC_TIMEOUT`` (seconds); default 5."""
        raw = os.environ.get("CODEMORPH_EXEC_TIMEOUT", "5")
        try:
            timeout = max(1, int(float(raw)))
        except (TypeError, ValueError):
            timeout = 5
        return cls(timeout=timeout)


@dataclass(frozen=True)
class SandboxRun:
    """Outcome of one sandboxed program execution."""

    ok: bool
    payload: Any                   # parsed marker JSON, or None
    stdout: str
    stderr: str
    timed_out: bool
    exit_code: "int | None"
    error: "str | None"


def _minimal_env() -> "dict[str, str]":
    """A near-empty environment: host variables never pass through."""
    env = {"PATH": os.defpath}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "C:\\Windows")
    return env


def _as_text(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return str(data)


def _parse_marker(stdout: str) -> "tuple[bool, Any, str | None]":
    """Find and parse the result-marker line, if the child printed one."""
    for line in stdout.splitlines():
        if line.startswith(_RESULT_MARKER):
            try:
                return True, json.loads(line[len(_RESULT_MARKER):]), None
            except json.JSONDecodeError as exc:
                return False, None, f"invalid result JSON: {exc}"
    return False, None, "result marker missing"


def _resource_limiter(config: SandboxConfig):
    """POSIX preexec hook: own session + best-effort rlimits."""

    def apply() -> None:
        try:
            os.setsid()
        except OSError:
            pass
        if resource is None:
            return
        for kind, value in (
            (resource.RLIMIT_AS, config.memory_mb * 1024 * 1024),
            (resource.RLIMIT_CPU, max(1, int(config.timeout) + 2)),
            (resource.RLIMIT_FSIZE, config.file_size_mb * 1024 * 1024),
        ):
            try:
                resource.setrlimit(kind, (value, value))
            except (OSError, ValueError):
                pass  # unsupported limit on this platform: skip silently

    return apply


class Sandbox:
    """Executes one Python program in an isolated subprocess."""

    def __init__(self, config: "SandboxConfig | None" = None) -> None:
        self.config = config if config is not None else SandboxConfig()

    def run_program(self, program: str) -> SandboxRun:
        """Run ``program`` (Python source) and return its outcome."""
        with tempfile.TemporaryDirectory(prefix="codemorph-run-") as workdir:
            try:
                completed = subprocess.run(
                    [sys.executable, "-I", "-"],
                    input=program,
                    capture_output=True,
                    text=True,
                    timeout=self.config.timeout,
                    cwd=workdir,
                    env=_minimal_env(),
                    preexec_fn=(
                        _resource_limiter(self.config)
                        if os.name == "posix"
                        else None
                    ),
                )
            except subprocess.TimeoutExpired as exc:
                return SandboxRun(
                    ok=False,
                    payload=None,
                    stdout=_as_text(exc.output),
                    stderr=_as_text(exc.stderr),
                    timed_out=True,
                    exit_code=None,
                    error=f"timeout after {self.config.timeout}s",
                )
        found, payload, error = _parse_marker(completed.stdout)
        if completed.returncode != 0:
            error = error or f"child exited with code {completed.returncode}"
        return SandboxRun(
            ok=found and completed.returncode == 0,
            payload=payload if found else None,
            stdout=completed.stdout,
            stderr=completed.stderr,
            timed_out=False,
            exit_code=completed.returncode,
            error=error,
        )