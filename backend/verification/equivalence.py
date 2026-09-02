"""Semantic-equivalence estimation (Phase 6).

This is the research core of CodeMorph: given an original and a migrated
source file, estimate how far apart their observable behavior is by
combining four independent signals:

* structural similarity   -- Phase-1 module model (function sets, signatures,
  classes, module variables)
* control-flow similarity -- Phase-3 CFGs (node-kind / edge-kind multisets
  and statement counts, per function)
* data-flow similarity    -- Phase-3 reaching-definitions reports
  (parameters, defined/used variables, producer->consumer edges, external
  inputs, per function)
* test behavior           -- Phase-5 sandboxed differential testing
  (PASS fraction over decisive cases; ERROR cases are excluded and noted)

The aggregate is a weighted mean over the *available* signals (default:
equal weights). Per-function signals are means over the union of function
names; a function present in only one version scores 0 -- additions and
removals are penalized, not ignored.

IMPORTANT -- what this is and is not
------------------------------------
This is an EMPIRICAL, HEURISTIC estimate of behavioral similarity. It is
NOT a formal proof of semantic equivalence and cannot be one:

* static signals are blind to structure-preserving changes (constant
  changes, swapped operators -- pinned by tests in this suite);
* the test signal covers only signature-derived inputs, compares value
  reprs / exception types / stdout, and cannot prove equivalence over the
  input space;
* a failing test signal can mean the migration broke the code OR that the
  original never ran on this interpreter (Python 2 idioms) -- the Phase-4
  transformation registry is required to interpret failures.

Every report carries this disclaimer in ``notes``.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

from ..analyzer.service import analyze_source
from .sandbox import SandboxConfig
from .syntax_checker import check_syntax
from .test_runner import verify_migration

if TYPE_CHECKING:
    from ..analyzer.control_flow import FunctionCFG

from ..analyzer.data_flow import DataFlowReport  # noqa: E402  (type-only use)

# The two imports above are re-stated cleanly below; TYPE_CHECKING guards
# the heavier model types.
if TYPE_CHECKING:
    from ..analyzer.control_flow import FunctionCFG
    from ..analyzer.models import FunctionInfo, ModuleInfo
    from .test_runner import VerificationResult


# --- small math helpers --------------------------------------------------------


def _jaccard(a: set, b: set) -> float:
    """Set Jaccard; two empty sets count as identical (1.0)."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _multiset_jaccard(a: Counter, b: Counter) -> float:
    """Counter Jaccard using min/max element counts."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return sum((a & b).values()) / sum((a | b).values())


def _ratio(a: int, b: int) -> float:
    """min/max ratio; 0/0 counts as identical (1.0)."""
    if a == 0 and b == 0:
        return 1.0
    return min(a, b) / max(a, b)


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 1.0


# --- signal 1: structural similarity ---------------------------------------------


def _signature_similarity(fa: "FunctionInfo", fb: "FunctionInfo") -> float:
    """Fraction of 7 signature features that match.

    Features: parameter names, kinds, default presence, annotations,
    async flag, decorators, method flag. Deliberately coarse and
    explainable -- it answers "did the interface change?".
    """
    features = (
        tuple(p.name for p in fa.params) == tuple(p.name for p in fb.params),
        tuple(p.kind for p in fa.params) == tuple(p.kind for p in fb.params),
        tuple(p.has_default for p in fa.params)
        == tuple(p.has_default for p in fb.params),
        tuple(p.annotation for p in fa.params)
        == tuple(p.annotation for p in fb.params),
        fa.is_async == fb.is_async,
        tuple(fa.decorators) == tuple(fb.decorators),
        fa.is_method == fb.is_method,
    )
    return sum(features) / len(features)


def structural_similarity(
    module_a: "ModuleInfo", module_b: "ModuleInfo"
) -> "tuple[float, dict]":
    """Compare declarations: function sets + signatures, classes, variables."""
    infos_a = {fn.qualified_name: fn for fn in module_a.functions}
    infos_b = {fn.qualified_name: fn for fn in module_b.functions}
    fn_jaccard = _jaccard(set(infos_a), set(infos_b))
    matched = set(infos_a) & set(infos_b)
    if not infos_a and not infos_b:
        signature_mean = 1.0
    elif matched:
        signature_mean = _mean(
            _signature_similarity(infos_a[name], infos_b[name])
            for name in sorted(matched)
        )
    else:
        signature_mean = 0.0
    function_component = 0.5 * fn_jaccard + 0.5 * signature_mean

    classes_a = {c.qualified_name: c for c in module_a.classes}
    classes_b = {c.qualified_name: c for c in module_b.classes}
    class_jaccard = _jaccard(set(classes_a), set(classes_b))
    matched_classes = set(classes_a) & set(classes_b)
    if matched_classes:
        method_mean = _mean(
            _jaccard(set(classes_a[n].methods), set(classes_b[n].methods))
            for n in sorted(matched_classes)
        )
        class_component = 0.5 * class_jaccard + 0.5 * method_mean
    else:
        class_component = class_jaccard

    variable_jaccard = _jaccard(
        set(module_a.module_variables), set(module_b.module_variables)
    )

    overall = (function_component + class_component + variable_jaccard) / 3
    detail = {
        "function_name_jaccard": fn_jaccard,
        "signature_similarity": signature_mean,
        "class_score": class_component,
        "variable_jaccard": variable_jaccard,
    }
    return overall, detail


# --- signal 2: control-flow similarity ----------------------------------------------


def _statement_count(cfg: "FunctionCFG") -> int:
    return sum(len(n.statements) for n in cfg.nodes if n.kind == "basic")


def _cfg_similarity(cfg_a: "FunctionCFG", cfg_b: "FunctionCFG") -> float:
    """Node-kind multiset, edge-kind multiset, and statement-count ratios.

    Exact graph isomorphism is deliberately NOT attempted: CFG node ids are
    not stable across rewrites, and kind/edge/statement distributions catch
    the divergences that matter (added branches, removed loops, collapsed
    statements) at a fraction of the cost. Documented approximation.
    """
    node_similarity = _multiset_jaccard(
        Counter(n.kind for n in cfg_a.nodes),
        Counter(n.kind for n in cfg_b.nodes),
    )
    edge_similarity = _multiset_jaccard(
        Counter(e.kind for e in cfg_a.edges),
        Counter(e.kind for e in cfg_b.edges),
    )
    statement_similarity = _ratio(
        _statement_count(cfg_a), _statement_count(cfg_b)
    )
    return (node_similarity + edge_similarity + statement_similarity) / 3


def control_flow_similarity(
    cfgs_a: "list[FunctionCFG]", cfgs_b: "list[FunctionCFG]"
) -> "tuple[float, dict]":
    by_name_a = {cfg.qualified_name: cfg for cfg in cfgs_a}
    by_name_b = {cfg.qualified_name: cfg for cfg in cfgs_b}
    names = set(by_name_a) | set(by_name_b)
    if not names:
        return 1.0, {"functions": {}}
    per_function = {}
    for name in sorted(names):
        if name in by_name_a and name in by_name_b:
            per_function[name] = _cfg_similarity(
                by_name_a[name], by_name_b[name]
            )
        else:
            per_function[name] = 0.0
    return _mean(per_function.values()), {"functions": per_function}


# --- signal 3: data-flow similarity -----------------------------------------------------


def _flow_similarity(ra: "DataFlowReport", rb: "DataFlowReport") -> float:
    params = _jaccard(
        {p.variable for p in ra.parameters},
        {p.variable for p in rb.parameters},
    )
    definitions = _jaccard(
        {d.variable for d in ra.definitions},
        {d.variable for d in rb.definitions},
    )
    uses = _jaccard(
        {u.variable for u in ra.uses}, {u.variable for u in rb.uses}
    )
    flows = _jaccard(
        {(e.producer, e.consumer) for e in ra.flow_edges},
        {(e.producer, e.consumer) for e in rb.flow_edges},
    )
    externals = _jaccard(set(ra.external_inputs), set(rb.external_inputs))
    return (params + definitions + uses + flows + externals) / 5


def data_flow_similarity(
    flows_a: "list[DataFlowReport]", flows_b: "list[DataFlowReport]"
) -> "tuple[float, dict]":
    by_name_a = {f.qualified_name: f for f in flows_a}
    by_name_b = {f.qualified_name: f for f in flows_b}
    names = set(by_name_a) | set(by_name_b)
    if not names:
        return 1.0, {"functions": {}}
    per_function = {}
    for name in sorted(names):
        if name in by_name_a and name in by_name_b:
            per_function[name] = _flow_similarity(
                by_name_a[name], by_name_b[name]
            )
        else:
            per_function[name] = 0.0
    return _mean(per_function.values()), {"functions": per_function}


# --- signal 4: test behavior ----------------------------------------------------------


def _test_behavior_signal(
    verification: "VerificationResult",
) -> "tuple[SignalScore | None, list[str]]":
    """PASS fraction over decisive cases; ERROR cases are excluded."""
    notes: list[str] = []
    decisive = verification.passed + verification.failed
    if verification.total == 0:
        notes.append(
            "test behavior unavailable: no test cases were generated "
            "(no testable functions) -- static signals only"
        )
        return None, notes
    if decisive == 0:
        notes.append(
            "test behavior unavailable: all test cases errored "
            "(infrastructure) -- static signals only"
        )
        return None, notes
    if verification.failed:
        notes.append(
            f"{verification.failed} test case(s) FAILED: behavior diverged "
            f"between original and migrated code"
        )
    if verification.errors:
        notes.append(
            f"{verification.errors} test case(s) errored (sandbox/"
            f"infrastructure) and were excluded from the test-behavior score"
        )
    detail = {
        "passed": verification.passed,
        "failed": verification.failed,
        "errors": verification.errors,
        "total": verification.total,
    }
    return (
        SignalScore(
            name="test_behavior",
            score=verification.passed / decisive,
            available=True,
            detail=detail,
        ),
        notes,
    )


# --- models and aggregation ---------------------------------------------------------------


@dataclass(frozen=True)
class SignalScore:
    """One equivalence signal: name, score (0..1), availability, detail."""

    name: str
    score: float
    available: bool
    detail: dict

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "score": round(self.score, 4),
            "available": self.available,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class EquivalenceWeights:
    """Aggregation weights per signal (defaults: equal)."""

    structural: float = 1.0
    control_flow: float = 1.0
    data_flow: float = 1.0
    test_behavior: float = 1.0


_WEIGHT_ATTR = {
    "structural": "structural",
    "control_flow": "control_flow",
    "data_flow": "data_flow",
    "test_behavior": "test_behavior",
}


def _aggregate(signals: "list[SignalScore]", weights: EquivalenceWeights) -> float:
    weighted = [
        (getattr(weights, _WEIGHT_ATTR[s.name]), s.score) for s in signals
    ]
    total = sum(w for w, _ in weighted)
    if total == 0:
        return 0.0
    return sum(w * score for w, score in weighted) / total


def _label_for(score: float) -> str:
    if score >= 0.95:
        return "very-high"
    if score >= 0.80:
        return "high"
    if score >= 0.60:
        return "moderate"
    if score >= 0.40:
        return "low"
    return "very-low"


@dataclass(frozen=True)
class EquivalenceReport:
    """The Phase-6 semantic-equivalence estimate for one migration."""

    filename: str
    score: float                                  # 0.0 .. 1.0 (raw)
    label: str
    signals: tuple = ()                           # available signals only
    notes: tuple = ()
    verification: "VerificationResult | None" = None

    @property
    def score_percent(self) -> int:
        return round(self.score * 100)

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "score": self.score_percent,
            "label": self.label,
            "estimate": True,
            "disclaimer": (
                "Estimated behavioral similarity from multiple signals; "
                "NOT a formal proof of semantic equivalence."
            ),
            "signals": [s.to_dict() for s in self.signals],
            "notes": list(self.notes),
            "verification": (
                self.verification.to_dict() if self.verification else None
            ),
        }


_DISCLAIMER = (
    "Empirical behavioral estimate from multiple signals -- NOT a formal "
    "proof of semantic equivalence."
)


def compute_equivalence(
    original_source: str,
    migrated_source: str,
    filename: str = "<string>",
    *,
    run_tests: bool = True,
    sandbox_config: "SandboxConfig | None" = None,
    weights: "EquivalenceWeights | None" = None,
) -> EquivalenceReport:
    """Estimate behavioral equivalence between original and migrated code.

    Pipeline: syntax gate -> static signals (structure, CFGs, data flow)
    -> optional sandboxed differential tests -> weighted aggregate.

    Args:
        run_tests: include the Phase-5 test-behavior signal (spawns
            sandboxed subprocesses). ``False`` gives a fast static-only
            estimate, explicitly noted as such.
        weights: aggregation weights; default equal weights.

    Raises:
        SourceParseError: if ``original_source`` is not valid Python (the
            original is assumed pre-analyzed).
    """
    weights = weights if weights is not None else EquivalenceWeights()
    notes: list[str] = [_DISCLAIMER]

    syntax = check_syntax(migrated_source, filename=filename)
    if not syntax.valid:
        notes.append(
            f"migrated source failed syntax validation (line "
            f"{syntax.error_line}: {syntax.error_message}); equivalence "
            f"score set to 0%"
        )
        return EquivalenceReport(
            filename=filename,
            score=0.0,
            label="invalid",
            signals=(),
            notes=tuple(notes),
            verification=None,
        )

    analysis_a = analyze_source(original_source, filename=filename)
    analysis_b = analyze_source(migrated_source, filename=filename)

    structural, structural_detail = structural_similarity(
        analysis_a.module, analysis_b.module
    )
    control_flow, control_flow_detail = control_flow_similarity(
        analysis_a.cfgs, analysis_b.cfgs
    )
    data_flow, data_flow_detail = data_flow_similarity(
        analysis_a.flows, analysis_b.flows
    )
    signals: "list[SignalScore]" = [
        SignalScore("structural", structural, True, structural_detail),
        SignalScore("control_flow", control_flow, True, control_flow_detail),
        SignalScore("data_flow", data_flow, True, data_flow_detail),
    ]

    verification = None
    if run_tests:
        verification = verify_migration(
            original_source,
            migrated_source,
            filename=filename,
            sandbox_config=sandbox_config,
        )
        test_signal, test_notes = _test_behavior_signal(verification)
        notes.extend(test_notes)
        if test_signal is not None:
            signals.append(test_signal)
    else:
        notes.append(
            "test behavior not requested (run_tests=False): score computed "
            "from static signals only"
        )

    score = _aggregate(signals, weights)
    return EquivalenceReport(
        filename=filename,
        score=score,
        label=_label_for(score),
        signals=tuple(signals),
        notes=tuple(notes),
        verification=verification,
    )


# --- rendering ---------------------------------------------------------------------------


def render_equivalence(report: EquivalenceReport) -> str:
    """Human-readable estimate with the per-signal breakdown."""
    lines = [
        f"Semantic Equivalence Estimate: {report.score_percent}% "
        f"({report.label})",
        "  [empirical multi-signal estimate -- NOT a formal proof of "
        "equivalence]",
    ]
    for signal in report.signals:
        percent = round(signal.score * 100)
        suffix = ""
        if signal.name == "test_behavior":
            detail = signal.detail
            suffix = (
                f"  ({detail['passed']}/{detail['passed'] + detail['failed']}"
                f" cases passed)"
            )
        lines.append(f"  {signal.name:<18} {percent:3d}%{suffix}")
    if report.notes:
        lines.append("notes:")
        for note in report.notes:
            lines.append(f"  - {note}")
    return "\n".join(lines)