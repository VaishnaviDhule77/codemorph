"""FastAPI application factory (Phase 9a).

Run from the repository root::

    uvicorn backend.api.app:app --reload

Interactive docs: http://127.0.0.1:8000/docs

If ``frontend/dist`` exists (Phase 9b build output), it is served at ``/``
so a single server hosts the whole tool.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .routes import router

_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _cors_origins() -> "list[str]":
    """Dev-frontend origins; extend via CODEMORPH_CORS_ORIGINS (comma-
    separated) -- e.g. a Codespaces-forwarded Vite URL."""
    raw = os.environ.get("CODEMORPH_CORS_ORIGINS", "")
    if raw:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    return ["http://localhost:5173", "http://127.0.0.1:5173"]


def create_app() -> FastAPI:
    app = FastAPI(
        title="CodeMorph API",
        version="0.1.0",
        description=(
            "AI-assisted code migration & semantic-equivalence analysis: "
            "static analysis, deterministic + LLM migration, sandboxed "
            "verification, equivalence estimation."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    if _DIST.is_dir():
        app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")
    return app


app = create_app()
