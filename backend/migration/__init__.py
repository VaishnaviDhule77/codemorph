"""CodeMorph migration engines (deterministic now; LLM-assisted in Phase 7)."""
from .deterministic import (
    MigrationConfig,
    MigrationResult,
    Risk,
    Transformation,
    TransformationEngine,
    TransformKind,
    structural_guard,
)

__all__ = [
    "MigrationConfig", "MigrationResult", "Risk", "Transformation",
    "TransformationEngine", "TransformKind", "structural_guard",
]