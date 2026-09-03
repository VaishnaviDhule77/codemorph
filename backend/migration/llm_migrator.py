"""LLM-assisted migration (Phase 7): provider abstraction + gated pipeline.

The LLM never rewrites blindly. Its prompt is built from the static
analysis of the ORIGINAL module (source, metrics, findings, structure
summary) plus explicit, machine-checkable constraints. Its output then
passes the same rejection pipeline as every other generated code:

1. code extraction   -- the response must contain a fenced code block
2. syntax gate       -- ``compile()`` via the Phase-5 syntax checker
3. structural guard  -- the Phase-4 guard (signatures/classes/variables)
4. analysis again    -- the migrated module is re-analyzed (findings)
5. tests             -- Phase-5 sandboxed differential testing
6. equivalence       -- the Phase-6 multi-signal estimate

Any gate failure returns the ORIGINAL source unchanged with a recorded
reason; accepted results still carry warnings when tests failed or the
equivalence estimate is weak ("reject or flag", never silent trust).

Providers
---------
``none``   -- default; no network, every migration returns NOT_CONFIGURED
``openai`` -- any OpenAI-compatible chat-completions endpoint (set
              CODEMORPH_LLM_BASE_URL for local/vLLM/compatible servers)

Configuration is environment-only (never hard-coded, never serialized):
CODEMORPH_LLM_PROVIDER, CODEMORPH_LLM_API_KEY, CODEMORPH_LLM_MODEL,
CODEMORPH_LLM_BASE_URL, CODEMORPH_LLM_TIMEOUT. The API key is held in
memory only and never appears in results, prompts, logs, or errors.

Security limitations (documented): the provider request carries the
analysis context (including source code) to the configured endpoint --
do not point CodeMorph at third-party endpoints with proprietary code.
No retry logic; temperature is pinned to 0 for determinism.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Sequence

from ..analyzer.data_flow import flow_findings
from ..analyzer.findings import Finding
from ..analyzer.service import FileAnalysis, analyze_source, run_findings
from ..verification import compute_equivalence
from ..verification.sandbox import SandboxConfig
from ..verification.syntax_checker import check_syntax
from .deterministic import structural_guard

if TYPE_CHECKING:
    from ..verification.equivalence import EquivalenceReport
    from ..verification.syntax_checker import SyntaxCheckResult


DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT = 60.0
_FINDINGS_CAP = 40

_NOT_CONFIGURED_DEFAULT = (
    "no LLM provider configured: set CODEMORPH_LLM_PROVIDER (and, for "
    "openai, CODEMORPH_LLM_API_KEY); see .env.example"
)

_SYSTEM_MESSAGE = (
    "You are a precise code-migration engine. You rewrite Python modules "
    "exactly as instructed, preserve behavior, and never invent "
    "functionality."
)

_CONSTRAINTS = (
    "1. Preserve every function and class definition: same names, same set.",
    "2. Preserve exact function signatures: parameter names, order, "
    "defaults, and annotations.",
    "3. Preserve module-level variable names.",
    "4. Do not add or remove top-level functions or classes.",
    "5. Behavior must be identical to the original for every input.",
    "6. Use modern Python 3 idioms; address the listed findings only "
    "where it is safe to do so.",
    "7. Keep comments and docstrings that document intent.",
)


# --- provider layer -----------------------------------------------------------


@dataclass(frozen=True)
class ProviderResponse:
    """Outcome of one provider call."""

    ok: bool
    text: str
    error: str | None
    model: str | None


class LLMProvider(Protocol):
    """Anything that can generate a migration proposal from a prompt."""

    name: str

    def generate(self, prompt: str) -> ProviderResponse:
        ...


class NullProvider:
    """The default provider: no network, always NOT_CONFIGURED."""

    name = "none"

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason or _NOT_CONFIGURED_DEFAULT

    def generate(self, prompt: str) -> ProviderResponse:
        return ProviderResponse(ok=False, text="", error=self.reason, model=None)


class OpenAIProvider:
    """OpenAI-compatible chat-completions provider (stdlib urllib only)."""

    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def generate(self, prompt: str) -> ProviderResponse:
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _SYSTEM_MESSAGE},
                {"role": "user", "content": prompt},
            ],
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # HTTPError guarantees .code; .reason is not documented for it,
            # so use .msg (the HTTP status message) with a str() fallback.
            detail = getattr(exc, "msg", None) or str(exc)
            return ProviderResponse(
                ok=False, text="", error=f"HTTP {exc.code}: {detail}",
                model=self.model,
            )
        except urllib.error.URLError as exc:
            return ProviderResponse(
                ok=False, text="", error=f"request failed: {exc.reason}",
                model=self.model,
            )
        except ValueError as exc:
            return ProviderResponse(
                ok=False, text="", error=f"invalid API response: {exc}",
                model=self.model,
            )
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            return ProviderResponse(
                ok=False, text="", error=f"unexpected API response shape: {exc}",
                model=self.model,
            )
        return ProviderResponse(ok=True, text=text, error=None, model=self.model)


def _env_float(env: dict, name: str, default: float) -> float:
    raw = env.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def create_provider(env: dict | None = None) -> LLMProvider:
    """Build the provider from the environment (or an injected env map)."""
    env = os.environ if env is None else env
    name = (env.get("CODEMORPH_LLM_PROVIDER") or "none").strip().lower()
    if name in ("", "none"):
        return NullProvider()
    if name != "openai":
        return NullProvider(
            f"unknown provider '{name}'; known providers: none, openai"
        )
    api_key = env.get("CODEMORPH_LLM_API_KEY")
    if not api_key:
        return NullProvider(
            "provider 'openai' selected but CODEMORPH_LLM_API_KEY is not set"
        )
    return OpenAIProvider(
        api_key,
        model=env.get("CODEMORPH_LLM_MODEL") or DEFAULT_MODEL,
        base_url=env.get("CODEMORPH_LLM_BASE_URL") or DEFAULT_BASE_URL,
        timeout=_env_float(env, "CODEMORPH_LLM_TIMEOUT", DEFAULT_TIMEOUT),
    )


# --- response parsing -------------------------------------------------------------


_FENCE_RE = re.compile(
    r"```[ \t]*(?P<lang>[A-Za-z0-9_+-]*)[ \t]*\r?\n(?P<body>.*?)```",
    re.DOTALL,
)


def extract_code(text: str) -> tuple[str | None, str]:
    """Extract migrated code from an LLM response.

    Prefers the first ```python fence; falls back to the first fence of any
    language. Returns (code, note); code is None when nothing usable exists.
    """
    blocks = list(_FENCE_RE.finditer(text))
    if not blocks:
        return None, "no fenced code block found in the response"
    python_blocks = [
        b for b in blocks if b.group("lang").lower() in ("python", "py", "python3")
    ]
    chosen = python_blocks[0] if python_blocks else blocks[0]
    code = chosen.group("body").strip("\n")
    if not code.strip():
        return None, "fenced code block is empty"
    note = f"extracted {chosen.group('lang') or 'plain'} fenced block"
    return code, note


# --- prompt construction -----------------------------------------------------------


def _format_param(param) -> str:
    prefix = {"vararg": "*", "kwarg": "**"}.get(param.kind, "")
    text = f"{prefix}{param.name}"
    if param.annotation:
        text += f": {param.annotation}"
    if param.default is not None:
        text += f" = {param.default}"
    return text


def _format_findings(findings: Sequence, cap: int = _FINDINGS_CAP) -> str:
    lines = [
        f"line {f.line} [{f.severity.value}] {f.category}: {f.message}"
        for f in findings[:cap]
    ]
    if len(findings) > cap:
        lines.append(
            f"... and {len(findings) - cap} more findings "
            f"(omitted to bound the prompt size)"
        )
    return "\n".join(lines) if lines else "(no findings)"


def _structure_summary(analysis: FileAnalysis) -> str:
    lines: list[str] = []
    for imp in analysis.module.imports:
        lines.append(f"import: {imp.statement}")
    if analysis.module.module_variables:
        lines.append(
            "module variables: " + ", ".join(analysis.module.module_variables)
        )
    complexity = {fc.qualified_name: fc for fc in analysis.complexity.functions}
    for fn in analysis.module.functions:
        params = ", ".join(_format_param(p) for p in fn.params)
        fc = complexity.get(fn.qualified_name)
        complexity_text = f", complexity {fc.complexity}" if fc else ""
        lines.append(
            f"function {fn.qualified_name}({params})  "
            f"[{fn.length} lines{complexity_text}]"
        )
    for cls in analysis.module.classes:
        bases = ", ".join(cls.bases)
        methods = ", ".join(cls.methods) or "(none)"
        lines.append(f"class {cls.qualified_name}({bases})  methods: {methods}")
    for caller in sorted(analysis.module.dependencies):
        callees = ", ".join(analysis.module.dependencies[caller])
        lines.append(f"internal calls: {caller} -> {callees}")
    return "\n".join(lines) if lines else "(empty module)"


def build_migration_prompt(
    source: str, analysis: FileAnalysis, findings: Sequence
) -> str:
    """Deterministic migration prompt: analysis context + hard constraints."""
    metrics = analysis.metrics
    complexity = analysis.complexity
    max_complexity = (
        complexity.max_function.complexity if complexity.max_function else 0
    )
    sections = [
        (
            "TASK: modernize the Python module below. Preserve its behavior "
            "exactly; improve only its form."
        ),
        "",
        "CONSTRAINTS (machine-enforced after generation; violations are rejected):",
        *_CONSTRAINTS,
        "",
        "METRICS",
        f"- lines of code: {metrics.code_lines} ({metrics.total_lines} total)",
        f"- functions: {metrics.num_functions} ({metrics.num_methods} methods)",
        f"- classes: {metrics.num_classes}",
        f"- imports: {metrics.num_imports}",
        f"- max nesting depth: {metrics.max_nesting_depth}",
        (
            f"- cyclomatic complexity: max {max_complexity}, "
            f"average {complexity.average}, total {complexity.total}"
        ),
        "",
        "FINDINGS (static analysis of the original)",
        _format_findings(findings),
        "",
        "STRUCTURE SUMMARY",
        _structure_summary(analysis),
        "",
        "SOURCE CODE",
        "```python",
        source,
        "```",
        "",
        "OUTPUT FORMAT",
        (
            "Respond with the complete migrated module in a single ```python "
            "fenced block. No prose, no explanations."
        ),
    ]
    return "\n".join(sections) + "\n"


# --- findings aggregation ------------------------------------------------------------


def collect_all_findings(analysis: FileAnalysis, filename: str) -> list:
    """Lexical (Phase 2) + flow-sensitive (Phase 3) findings, sorted."""
    findings: list = list(run_findings(analysis))
    findings.extend(flow_findings(analysis.flows, filename))
    findings.sort(key=lambda f: (f.line, f.category, f.message))
    return findings


# --- the gated pipeline -----------------------------------------------------------------


class LLMMigrationStatus:
    NOT_CONFIGURED = "NOT_CONFIGURED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    NO_CODE = "NO_CODE"
    INVALID_SYNTAX = "INVALID_SYNTAX"
    STRUCTURAL_REJECTION = "STRUCTURAL_REJECTION"
    ACCEPTED = "ACCEPTED"


@dataclass
class LLMMigrationResult:
    """Outcome of one LLM-assisted migration attempt."""

    filename: str = "<string>"
    status: str = LLMMigrationStatus.NOT_CONFIGURED
    provider: str = "none"
    model: str | None = None
    prompt: str = ""
    raw_response: str | None = None
    extracted_code: str | None = None
    original_source: str = ""
    migrated_source: str = ""
    rejection_reason: str | None = None
    syntax_check: "SyntaxCheckResult | None" = None
    guard_reason: str | None = None
    equivalence: "EquivalenceReport | None" = None
    findings_before: int | None = None
    findings_after: int | None = None
    warnings: tuple = ()
    duration: float = 0.0

    @property
    def accepted(self) -> bool:
        return self.status == LLMMigrationStatus.ACCEPTED

    @property
    def flagged(self) -> bool:
        """Accepted, but tests failed / equivalence is weak: needs review."""
        return self.accepted and bool(self.warnings)

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "status": self.status,
            "accepted": self.accepted,
            "flagged": self.flagged,
            "provider": self.provider,
            "model": self.model,
            "prompt": self.prompt,
            "raw_response": _truncate(self.raw_response, 2000),
            "extracted_code": self.extracted_code,
            "original_source": self.original_source,
            "migrated_source": self.migrated_source,
            "rejection_reason": self.rejection_reason,
            "syntax_check": (
                self.syntax_check.to_dict() if self.syntax_check else None
            ),
            "guard_reason": self.guard_reason,
            "equivalence": self.equivalence.to_dict() if self.equivalence else None,
            "findings_before": self.findings_before,
            "findings_after": self.findings_after,
            "warnings": list(self.warnings),
            "duration": round(self.duration, 3),
        }


def _truncate(text: "str | None", limit: int) -> "str | None":
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} characters omitted]"


def _make_result(**overrides) -> LLMMigrationResult:
    fields = {
        "filename": "<string>",
        "status": LLMMigrationStatus.NOT_CONFIGURED,
        "provider": "none",
        "model": None,
        "prompt": "",
        "raw_response": None,
        "extracted_code": None,
        "original_source": "",
        "migrated_source": "",
        "rejection_reason": None,
        "syntax_check": None,
        "guard_reason": None,
        "equivalence": None,
        "findings_before": None,
        "findings_after": None,
        "warnings": (),
        "duration": 0.0,
    }
    fields.update(overrides)
    return LLMMigrationResult(**fields)


def _build_warnings(equivalence: "EquivalenceReport") -> tuple:
    warnings: list[str] = []
    verification = equivalence.verification
    if verification is not None:
        if verification.failed:
            warnings.append(
                f"{verification.failed} differential test case(s) FAILED -- "
                f"inspect the divergences before adopting this migration"
            )
        if verification.errors:
            warnings.append(
                f"{verification.errors} test case(s) errored during "
                f"verification (sandbox/infrastructure)"
            )
    if equivalence.label in ("moderate", "low", "very-low"):
        warnings.append(
            f"equivalence estimate is {equivalence.score_percent}% "
            f"({equivalence.label})"
        )
    return tuple(warnings)


class LLMMigrator:
    """Runs the gated LLM migration pipeline for one source file."""

    def __init__(
        self,
        provider: "LLMProvider | None" = None,
        sandbox_config: "SandboxConfig | None" = None,
    ) -> None:
        self.provider = provider if provider is not None else create_provider()
        self.sandbox_config = sandbox_config

    def migrate(self, source: str, filename: str = "<string>") -> LLMMigrationResult:
        """Prompt -> generate -> extract -> syntax gate -> structural guard
        -> re-analysis -> sandboxed tests -> equivalence estimate.

        Raises:
            SourceParseError: if ``source`` is not valid Python.
        """
        started = time.perf_counter()

        def elapsed() -> float:
            return time.perf_counter() - started

        analysis = analyze_source(source, filename=filename)
        findings = collect_all_findings(analysis, filename)
        prompt = build_migration_prompt(source, analysis, findings)
        common = {
            "filename": filename,
            "provider": self.provider.name,
            "prompt": prompt,
            "original_source": source,
            "findings_before": len(findings),
        }

        response = self.provider.generate(prompt)
        if not response.ok:
            status = (
                LLMMigrationStatus.NOT_CONFIGURED
                if isinstance(self.provider, NullProvider)
                else LLMMigrationStatus.PROVIDER_ERROR
            )
            return _make_result(
                **common,
                status=status,
                model=response.model,
                migrated_source=source,  # every rejection keeps the original
                rejection_reason=response.error,
                duration=elapsed(),
            )

        code, note = extract_code(response.text)
        if code is None:
            return _make_result(
                **common,
                status=LLMMigrationStatus.NO_CODE,
                model=response.model,
                raw_response=response.text,
                migrated_source=source,
                rejection_reason=note,
                duration=elapsed(),
            )

        syntax = check_syntax(code, filename=filename)
        if not syntax.valid:
            return _make_result(
                **common,
                status=LLMMigrationStatus.INVALID_SYNTAX,
                model=response.model,
                raw_response=response.text,
                extracted_code=code,
                syntax_check=syntax,
                migrated_source=source,
                rejection_reason=f"line {syntax.error_line}: {syntax.error_message}",
                duration=elapsed(),
            )

        guard_ok, guard_reason = structural_guard(source, code, filename=filename)
        if not guard_ok:
            return _make_result(
                **common,
                status=LLMMigrationStatus.STRUCTURAL_REJECTION,
                model=response.model,
                raw_response=response.text,
                extracted_code=code,
                syntax_check=syntax,
                migrated_source=source,
                guard_reason=guard_reason,
                rejection_reason=guard_reason,
                duration=elapsed(),
            )

        equivalence = compute_equivalence(
            source, code, filename=filename, sandbox_config=self.sandbox_config
        )
        migrated_analysis = analyze_source(code, filename=filename)
        findings_after = len(collect_all_findings(migrated_analysis, filename))
        return _make_result(
            **common,
            status=LLMMigrationStatus.ACCEPTED,
            model=response.model,
            raw_response=response.text,
            extracted_code=code,
            syntax_check=syntax,
            migrated_source=code,
            equivalence=equivalence,
            findings_after=findings_after,
            warnings=_build_warnings(equivalence),
            duration=elapsed(),
        )