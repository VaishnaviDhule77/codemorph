"""HTTP API routes for CodeMorph (Phase 9a).

Every endpoint is a thin, validated composition of the existing analysis,
migration, and verification layers -- no analysis logic lives here.

Security posture (localhost research tool; documented, not hidden):

* Uploaded files are validated before any analysis: ``.py`` extension
  only, a 1 MB size cap, and UTF-8 decoding.
* All code execution requested by these endpoints (differential testing,
  equivalence estimation) goes through the Phase-5 sandbox: separate
  ``python -I`` process, near-empty environment, private temp cwd,
  wall-clock timeout. The API never executes analyzed code in-process.
* ``POST /api/repository`` analyzes a caller-supplied local path. The
  analysis is read-only, but this endpoint must not be exposed on an
  untrusted network -- it discloses repository structure to the caller.
* LLM configuration is environment-only; the API key is never echoed by
  any endpoint (tested).

SAFE MODE (public deployments): ``CODEMORPH_SAFE_MODE`` set to a truthy
value disables every endpoint that probes local paths or executes
submitted/generated code (``/repository``, ``/verify``, ``/llm-migrate``
return 403; ``/pipeline`` falls back to a static-only equivalence
estimate). Analysis, deterministic migration, and the diff view remain
fully functional -- they are pure computation. The environment variable
is read once at import time (deployments restart to pick it up); the
module-level flag is read at call time, so tests can toggle it via
``monkeypatch.setattr``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ..analyzer.ast_analyzer import SourceParseError
from ..analyzer.data_flow import flow_findings
from ..analyzer.service import analyze_source, run_findings
from ..migration.deterministic import TransformationEngine
from ..migration.llm_migrator import (
    LLMMigrator,
    NullProvider,
    collect_all_findings,
    create_provider,
)
from ..repository import RepositoryError, analyze_repository
from ..verification import compute_equivalence
from ..verification.sandbox import SandboxConfig
from .diffing import line_diff

SAFE_MODE = os.environ.get("CODEMORPH_SAFE_MODE", "") not in (
    "", "0", "false", "False",
)

router = APIRouter(prefix="/api")

MAX_UPLOAD_BYTES = 1_000_000
DEFAULT_RESULTS_DIR = Path("benchmark") / "results"
_EXPERIMENTS_FILE = "experiments.json"


# --- request models --------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    source: str = Field(min_length=1)
    filename: str = "uploaded.py"


class PipelineRequest(AnalyzeRequest):
    run_tests: bool = True


class VerifyRequest(BaseModel):
    original: str = Field(min_length=1)
    migrated: str = Field(min_length=1)
    filename: str = "uploaded.py"
    run_tests: bool = True


class DiffRequest(BaseModel):
    original: str
    migrated: str


class RepositoryRequest(BaseModel):
    path: str = Field(min_length=1)


# --- helpers ----------------------------------------------------------------------


def _syntax_error(exc: SourceParseError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "error": "syntax",
            "filename": exc.filename,
            "line": exc.lineno,
            "offset": exc.offset,
            "message": exc.message,
        },
    )


def _analyze_payload(source: str, filename: str) -> dict:
    analysis = analyze_source(source, filename=filename)
    payload = analysis.to_dict()
    payload["findings"] = [f.to_dict() for f in run_findings(analysis)]
    payload["flow_findings"] = [
        f.to_dict() for f in flow_findings(analysis.flows, filename)
    ]
    return payload


# --- health -----------------------------------------------------------------------


@router.get("/health")
def health() -> dict:
    """Liveness + LLM provider status (never the key) + safe-mode flag."""
    provider = create_provider()
    return {
        "status": "ok",
        "llm_provider": provider.name,
        "llm_configured": not isinstance(provider, NullProvider),
        "safe_mode": SAFE_MODE,
    }


# --- analysis ------------------------------------------------------------------------


@router.post("/analyze")
def analyze(request: AnalyzeRequest) -> dict:
    """Full Phase 1-3 analysis of one source file + all findings."""
    try:
        return _analyze_payload(request.source, request.filename)
    except SourceParseError as exc:
        raise _syntax_error(exc)


@router.post("/analyze/upload")
async def analyze_upload(file: UploadFile = File(...)) -> dict:
    """Analyze an uploaded Python file (validated: .py, <=1 MB, UTF-8)."""
    if file.filename is None or not file.filename.endswith(".py"):
        raise HTTPException(
            status_code=400, detail="only .py files are accepted"
        )
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds the {MAX_UPLOAD_BYTES}-byte upload limit",
        )
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400, detail="file is not valid UTF-8 text"
        )
    try:
        return _analyze_payload(source, file.filename)
    except SourceParseError as exc:
        raise _syntax_error(exc)


@router.post("/repository")
def repository(request: RepositoryRequest) -> dict:
    """Phase-8 repository analysis for a local path (read-only)."""
    if SAFE_MODE:
        raise HTTPException(
            status_code=403,
            detail="repository analysis is disabled in safe mode",
        )
    try:
        report = analyze_repository(request.path)
    except RepositoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return report.to_dict()


# --- migration -------------------------------------------------------------------------


@router.post("/migrate")
def migrate(request: AnalyzeRequest) -> dict:
    """Phase-4 deterministic migration with a traceable transformation log."""
    try:
        result = TransformationEngine().transform_source(
            request.source, filename=request.filename
        )
    except SourceParseError as exc:
        raise _syntax_error(exc)
    return result.to_dict()


@router.post("/llm-migrate")
def llm_migrate(request: AnalyzeRequest) -> dict:
    """Phase-7 gated LLM migration (provider from the environment)."""
    if SAFE_MODE:
        # The pipeline's verification stage executes GENERATED code in the
        # sandbox once a provider is configured; safe mode forbids that.
        raise HTTPException(
            status_code=403,
            detail=(
                "LLM migration is disabled in safe mode: its verification "
                "stage executes generated code in the sandbox"
            ),
        )
    try:
        result = LLMMigrator(
            sandbox_config=SandboxConfig.from_env()
        ).migrate(request.source, filename=request.filename)
    except SourceParseError as exc:
        raise _syntax_error(exc)
    return result.to_dict()


# --- verification & equivalence ------------------------------------------------------------


@router.post("/verify")
def verify(request: VerifyRequest) -> dict:
    """Phase-6 equivalence estimate (includes Phase-5 verification)."""
    if SAFE_MODE:
        raise HTTPException(
            status_code=403,
            detail=(
                "code execution is disabled in safe mode; equivalence "
                "estimates are static-only in this deployment"
            ),
        )
    try:
        report = compute_equivalence(
            request.original,
            request.migrated,
            filename=request.filename,
            run_tests=request.run_tests,
            sandbox_config=SandboxConfig.from_env(),
        )
    except SourceParseError as exc:
        raise _syntax_error(exc)
    return report.to_dict()


@router.post("/pipeline")
def pipeline(request: PipelineRequest) -> dict:
    """One-shot demo endpoint: analyze -> deterministic migrate ->
    re-analyze -> equivalence estimate (sandboxed tests unless safe mode)."""
    try:
        analysis = analyze_source(request.source, filename=request.filename)
        findings_before = collect_all_findings(analysis, request.filename)
        migration = TransformationEngine().transform_source(
            request.source, filename=request.filename
        )
        equivalence = compute_equivalence(
            request.source,
            migration.migrated_source,
            filename=request.filename,
            run_tests=request.run_tests and not SAFE_MODE,
            sandbox_config=SandboxConfig.from_env(),
        )
        migrated_analysis = analyze_source(
            migration.migrated_source, filename=request.filename
        )
        findings_after = collect_all_findings(
            migrated_analysis, request.filename
        )
    except SourceParseError as exc:
        raise _syntax_error(exc)
    analysis_payload = analysis.to_dict()
    analysis_payload["findings"] = [f.to_dict() for f in findings_before]
    return {
        "filename": request.filename,
        "analysis": analysis_payload,
        "migration": migration.to_dict(),
        "equivalence": equivalence.to_dict(),
        "findings_before": len(findings_before),
        "findings_after": len(findings_after),
    }


# --- comparison & experiments ------------------------------------------------------------


@router.post("/diff")
def diff(request: DiffRequest) -> dict:
    """Line-level diff rows for the side-by-side comparison view."""
    rows, summary = line_diff(request.original, request.migrated)
    return {"rows": rows, "summary": summary}


@router.get("/experiments")
def experiments() -> dict:
    """Stored experiment results (written by the Phase-10 evaluation).

    Returns an empty list when none exist -- results are never fabricated.
    """
    results_dir = Path(
        os.environ.get("CODEMORPH_RESULTS_DIR", "") or DEFAULT_RESULTS_DIR
    )
    path = results_dir / _EXPERIMENTS_FILE
    if not path.is_file():
        return {
            "experiments": [],
            "note": (
                "no stored experiment results; run the Phase-10 evaluation "
                "to generate them"
            ),
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"could not read experiment results: {exc}",
        )
    experiments_list = data if isinstance(data, list) else [data]
    return {"experiments": experiments_list}