"""Repository-level analysis (Phase 8).

Discovers Python files under a root directory, analyzes each with the full
Phase 1-3 pipeline, aggregates findings (Phase 2 lexical + Phase 3
flow-sensitive via ``collect_all_findings``), resolves import statements
into cross-file dependency edges, ranks files by a documented risk
heuristic, and renders a repository-level report.

Discovery
---------
``os.walk`` over the root; only ``*.py`` files; directories in
``EXCLUDED_DIRS`` (virtualenvs, caches, build output, VCS metadata) are
pruned; directories are visited and results returned in sorted order for
determinism. Symlinks are not followed. Files that cannot be read
(encoding/permission) or parsed get ``read_error``/``parse_error``
statuses and are excluded from metrics and ranking -- an unparseable file
is reported in the errors section: it is its own alarm.

Cross-file dependencies (documented approximation)
---------------------------------------------------
Import statements are resolved against the *discovered file set* by name:
absolute imports resolve the dotted module path against the importing
file's directory and the repository root; relative imports resolve against
the file's directory per their level. ``<mod>.py``, ``<mod>/__init__.py``,
and all ancestor package ``__init__`` files are considered (importing
``a.b`` executes ``a/__init__.py`` too). There is no ``sys.path`` or
installed-package knowledge -- an import matching nothing in the
repository simply produces no edge. Edges are deduplicated by
(source, target); fan-in/fan-out count distinct files.

Risk heuristic
--------------
``risk = 3*HIGH + 2*MEDIUM + 1*LOW + max_function_complexity + fan_in``
per file (findings, worst function complexity, and how many other repo
files depend on it). Every component is per-file and documented.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

from ..analyzer.ast_analyzer import SourceParseError
from ..analyzer.complexity import ComplexityReport
from ..analyzer.findings import Finding, severity_counts
from ..analyzer.metrics import MetricsReport
from ..analyzer.service import analyze_source
from ..migration.llm_migrator import collect_all_findings


EXCLUDED_DIRS = {
    ".eggs", ".git", ".hg", ".mypy_cache", ".nox", ".pytest_cache",
    ".ruff_cache", ".svn", ".tox", ".venv", "__pycache__", "build",
    "dist", "env", "node_modules", "site-packages", "venv",
}

_SEVERITY_WEIGHTS = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


class RepositoryError(Exception):
    """Raised when the repository root is missing or not a directory."""


# --- discovery ----------------------------------------------------------------


def discover_python_files(root: "str | Path") -> list[Path]:
    """Discover analyzable ``*.py`` files under ``root``, deterministically."""
    root = Path(root)
    if not root.exists():
        raise RepositoryError(f"repository root does not exist: {root}")
    if not root.is_dir():
        raise RepositoryError(f"repository root is not a directory: {root}")
    discovered: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        for name in sorted(filenames):
            if name.endswith(".py"):
                discovered.append(Path(dirpath) / name)
    return sorted(discovered)


# --- models --------------------------------------------------------------------


@dataclass(frozen=True)
class RepoDependency:
    """One internal dependency edge: ``source`` imports ``target``."""

    source: str   # repo-relative posix path of the importing file
    target: str   # repo-relative posix path of the imported file
    module: str   # module name as written in the import statement

    def to_dict(self) -> dict:
        return {
            "source": self.source, "target": self.target, "module": self.module,
        }


@dataclass
class FileSummary:
    """Everything the repository report knows about one file."""

    path: str
    status: str                                    # ok | parse_error | read_error
    error: "str | None"
    metrics: "MetricsReport | None"
    complexity: "ComplexityReport | None"
    findings: "list[Finding]"
    fan_in: int = 0
    fan_out: int = 0
    risk_score: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def max_complexity(self) -> int:
        if self.complexity is not None and self.complexity.max_function:
            return self.complexity.max_function.complexity
        return 0

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "error": self.error,
            "metrics": asdict(self.metrics) if self.metrics else None,
            "max_complexity": self.max_complexity,
            "findings": [f.to_dict() for f in self.findings],
            "fan_in": self.fan_in,
            "fan_out": self.fan_out,
            "risk_score": self.risk_score,
        }


@dataclass
class RepoAnalysis:
    """Aggregated repository report (Phase 8)."""

    root: str
    files: "list[FileSummary]"
    dependencies: "list[RepoDependency]"

    # -- counts -----------------------------------------------------------

    @property
    def files_discovered(self) -> int:
        return len(self.files)

    @property
    def files_analyzed(self) -> int:
        return sum(1 for f in self.files if f.ok)

    @property
    def files_with_errors(self) -> int:
        return sum(1 for f in self.files if not f.ok)

    # -- aggregates (ok files only; error files are reported separately) --

    @property
    def total_functions(self) -> int:
        return sum(f.metrics.num_functions for f in self.files if f.ok)

    @property
    def total_methods(self) -> int:
        return sum(f.metrics.num_methods for f in self.files if f.ok)

    @property
    def total_classes(self) -> int:
        return sum(f.metrics.num_classes for f in self.files if f.ok)

    @property
    def total_imports(self) -> int:
        return sum(f.metrics.num_imports for f in self.files if f.ok)

    @property
    def total_lines(self) -> int:
        return sum(f.metrics.total_lines for f in self.files if f.ok)

    @property
    def total_code_lines(self) -> int:
        return sum(f.metrics.code_lines for f in self.files if f.ok)

    @property
    def findings(self) -> "list[Finding]":
        return [finding for f in self.files for finding in f.findings]

    def severity_counts(self) -> dict:
        return severity_counts(self.findings)

    # -- lookups and rankings ----------------------------------------------

    def file(self, path: str) -> FileSummary:
        for f in self.files:
            if f.path == path:
                return f
        raise KeyError(path)

    def highest_complexity(self) -> "tuple[str, str, int] | None":
        """(file, qualified function name, complexity) of the repo maximum."""
        best = None
        for f in self.files:
            if not f.ok or f.complexity is None:
                continue
            for fc in f.complexity.functions:
                if best is None or fc.complexity > best[2]:
                    best = (f.path, fc.qualified_name, fc.complexity)
        return best

    def high_risk_files(self, limit: int = 5) -> "list[FileSummary]":
        ok_files = [f for f in self.files if f.ok]
        ranked = sorted(ok_files, key=lambda f: (-f.risk_score, f.path))
        return ranked[:limit]

    def to_dict(self) -> dict:
        highest = self.highest_complexity()
        return {
            "root": self.root,
            "files_discovered": self.files_discovered,
            "files_analyzed": self.files_analyzed,
            "files_with_errors": self.files_with_errors,
            "totals": {
                "functions": self.total_functions,
                "methods": self.total_methods,
                "classes": self.total_classes,
                "imports": self.total_imports,
                "lines_total": self.total_lines,
                "lines_code": self.total_code_lines,
                "findings": len(self.findings),
            },
            "severity_counts": self.severity_counts(),
            "dependencies": [d.to_dict() for d in self.dependencies],
            "highest_complexity": (
                {"file": highest[0], "function": highest[1],
                 "complexity": highest[2]}
                if highest else None
            ),
            "high_risk_files": [
                {"path": f.path, "risk_score": f.risk_score}
                for f in self.high_risk_files(limit=len(self.files))
            ],
            "files": [f.to_dict() for f in self.files],
        }


# --- dependency resolution --------------------------------------------------------


def _module_candidates(base: Path, module: str) -> list[Path]:
    """Filesystem candidates for a dotted module name under ``base``."""
    if not module:
        return []
    parts = module.split(".")
    candidates = [
        base.joinpath(*parts[:-1], parts[-1] + ".py"),
        base.joinpath(*parts, "__init__.py"),
    ]
    for i in range(1, len(parts)):          # ancestor package inits
        candidates.append(base.joinpath(*parts[:i], "__init__.py"))
    return candidates


def _resolve_import(imp, file_path: Path, root: Path, known: set) -> list:
    """(target_path, module_as_written) pairs for one import statement."""
    file_dir = file_path.parent
    results: "list[tuple[Path, str]]" = []
    if imp.is_from:
        if imp.level > 0:
            base = file_dir
            for _ in range(imp.level - 1):
                base = base.parent
            bases = [base]
        else:
            bases = [file_dir, root]
        if imp.module:
            for base in bases:
                for candidate in _module_candidates(base, imp.module):
                    if candidate in known:
                        results.append((candidate, imp.module))
        else:
            # ``from . import name``: each name is a module in its own right
            for name, _alias in imp.names:
                if name == "*":
                    continue
                for base in bases:
                    for candidate in _module_candidates(base, name):
                        if candidate in known:
                            results.append((candidate, name))
    else:
        for name, _alias in imp.names:
            if name == "*":
                continue
            for base in (file_dir, root):
                for candidate in _module_candidates(base, name):
                    if candidate in known:
                        results.append((candidate, name))
    return results


# --- the analysis -------------------------------------------------------------------


def analyze_repository(root: "str | Path") -> RepoAnalysis:
    """Analyze every discovered Python file and aggregate the report.

    Raises:
        RepositoryError: if ``root`` does not exist or is not a directory.
    """
    root = Path(root)
    discovered = discover_python_files(root)
    known = set(discovered)

    summaries: "list[FileSummary]" = []
    imports_by_file: "dict[str, list]" = {}
    file_paths: "dict[str, Path]" = {}
    for path in discovered:
        rel = path.relative_to(root).as_posix()
        file_paths[rel] = path
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            summaries.append(FileSummary(
                path=rel, status="read_error", error=str(exc),
                metrics=None, complexity=None, findings=[],
            ))
            continue
        try:
            analysis = analyze_source(source, filename=rel)
        except SourceParseError as exc:
            summaries.append(FileSummary(
                path=rel, status="parse_error", error=exc.message,
                metrics=None, complexity=None, findings=[],
            ))
            continue
        summaries.append(FileSummary(
            path=rel, status="ok", error=None,
            metrics=analysis.metrics, complexity=analysis.complexity,
            findings=collect_all_findings(analysis, rel),
        ))
        imports_by_file[rel] = analysis.module.imports

    # Cross-file dependency edges, deduplicated by (source, target).
    dependencies: "list[RepoDependency]" = []
    seen: set = set()
    for rel in sorted(imports_by_file):
        for imp in imports_by_file[rel]:
            for target, module in _resolve_import(
                imp, file_paths[rel], root, known
            ):
                target_rel = target.relative_to(root).as_posix()
                if target_rel == rel or (rel, target_rel) in seen:
                    continue
                seen.add((rel, target_rel))
                dependencies.append(RepoDependency(rel, target_rel, module))

    fan_in: "dict[str, int]" = {}
    fan_out: "dict[str, int]" = {}
    for dep in dependencies:
        fan_out[dep.source] = fan_out.get(dep.source, 0) + 1
        fan_in[dep.target] = fan_in.get(dep.target, 0) + 1

    for summary in summaries:
        summary.fan_in = fan_in.get(summary.path, 0)
        summary.fan_out = fan_out.get(summary.path, 0)
        if summary.ok:
            summary.risk_score = (
                sum(_SEVERITY_WEIGHTS[f.severity.value] for f in summary.findings)
                + summary.max_complexity
                + summary.fan_in
            )

    return RepoAnalysis(root=str(root), files=summaries, dependencies=dependencies)


# --- rendering -----------------------------------------------------------------------


def render_repository(report: RepoAnalysis, top: int = 5) -> str:
    """Human-readable repository-level report."""
    lines = [f"Repository Analysis: {report.root}"]
    if report.files_discovered == 0:
        lines.append("  No Python files discovered.")
        return "\n".join(lines)
    lines.append(
        f"  Files: {report.files_discovered} discovered | "
        f"{report.files_analyzed} analyzed | "
        f"{report.files_with_errors} with errors"
    )
    lines.append(
        f"  Functions: {report.total_functions} "
        f"({report.total_methods} methods) | "
        f"Classes: {report.total_classes} | Imports: {report.total_imports}"
    )
    lines.append(
        f"  Lines: {report.total_lines} total | {report.total_code_lines} code"
    )
    counts = report.severity_counts()
    lines.append(
        f"  Findings: {len(report.findings)} total "
        f"({counts['HIGH']} high, {counts['MEDIUM']} medium, {counts['LOW']} low)"
    )
    lines.append(f"  Internal dependencies: {len(report.dependencies)} edges")
    for dep in report.dependencies:
        lines.append(f"    {dep.source} -> {dep.target} ({dep.module})")

    highest = report.highest_complexity()
    lines.append("")
    lines.append("Highest complexity:")
    if highest:
        lines.append(f"  {highest[0]} :: {highest[1]} (complexity {highest[2]})")
    else:
        lines.append("  (no functions found)")

    lines.append("")
    lines.append(
        "High-risk files "
        "(risk = 3xHIGH + 2xMEDIUM + 1xLOW + max complexity + fan-in):"
    )
    for rank, summary in enumerate(report.high_risk_files(limit=top), start=1):
        lines.append(f"  {rank}. {summary.path} (risk {summary.risk_score})")

    errors = [f for f in report.files if not f.ok]
    lines.append("")
    if errors:
        lines.append("Files with errors:")
        for summary in errors:
            lines.append(f"  {summary.path}: {summary.status}: {summary.error}")
    else:
        lines.append("Files with errors: none")
    return "\n".join(lines)