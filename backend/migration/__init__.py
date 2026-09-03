"""CodeMorph migration engines: deterministic (Phase 4), LLM-assisted (Phase 7)."""
from .deterministic import (
    MigrationConfig,
    MigrationResult,
    Risk,
    Transformation,
    TransformationEngine,
    TransformKind,
    structural_guard,
)
from .llm_migrator import (
    LLMMigrationResult,
    LLMMigrationStatus,
    LLMProvider,
    LLMMigrator,
    NullProvider,
    OpenAIProvider,
    ProviderResponse,
    build_migration_prompt,
    collect_all_findings,
    create_provider,
    extract_code,
)

__all__ = [
    "MigrationConfig", "MigrationResult", "Risk", "Transformation",
    "TransformationEngine", "TransformKind", "structural_guard",
    "LLMMigrationResult", "LLMMigrationStatus", "LLMProvider", "LLMMigrator",
    "NullProvider", "OpenAIProvider", "ProviderResponse",
    "build_migration_prompt", "collect_all_findings", "create_provider",
    "extract_code",
]