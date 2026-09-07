CodeMorph
CodeMorph is an AI-assisted code migration and semantic-equivalenceanalysis tool. It analyzes legacy source code, modernizes it withdeterministic transformations and (optionally) an LLM, and then estimates —through structure, data flow, and executed tests — whether the transformedcode preserves the original behavior.

Research question. Can static program analysis combined withLLM-assisted code transformation improve the reliability of automated codemigration compared with LLM-only transformation?

The hypothesis, variables, and benchmark methodology will be documented asthe evaluation phases land. Semantic equivalence in CodeMorph is anempirical behavioral estimate, never a formal proof.

CodeMorph is built phase by phase. Phase 1 is complete: a real AST-basedanalysis engine producing a structural model of a Python module, codemetrics, and McCabe cyclomatic complexity.

Phase tracker
Phase	Scope	Status
1	AST parser, structural model, code metrics, complexity	done
2	Static-analysis rules (code smells, findings)	next
3	Control-flow & data-flow analysis	planned
4	Deterministic transformations	planned
5	Syntax validation + sandboxed test execution	planned
6	Behavioral/semantic equivalence estimation	planned
7	LLM integration (provider abstraction)	planned
8	Repository-level analysis	planned
9	React + FastAPI interface	planned
10	Research evaluation framework & benchmark	planned
11	Hardening, docs, coverage	planned
Installation
Python 3.9+ (uses ast.unparse and end_lineno).

python -m venv .venv && source .venv/bin/activatepip install -r requirements-dev.txtpytest
Usage
bash

python -m backend.analyzer tests/fixtures/calculator.py          # tree + metrics
python -m backend.analyzer tests/fixtures/calculator.py --json   # machine-readable
python

from backend.analyzer import analyze_source

report = analyze_source(source, filename="legacy.py")
print(report.structure)       # unicode tree of the module structure
print(report.metrics)         # LOC, counts, function lengths, nesting
print(report.complexity)      # per-function McCabe complexity + ranks
Architecture (Phase 1)
text

backend/analyzer/
├── models.py       serializable model: ModuleInfo, FunctionInfo, ...
├── _ast_utils.py   scope-aware AST traversal shared by all analyses
├── ast_analyzer.py parse -> structural model + internal call graph
├── metrics.py      lines, counts, function lengths
├── complexity.py   McCabe cyclomatic complexity (+ A-F ranks)
├── renderer.py     unicode tree rendering
├── service.py      analyze_source(): one-call composition
└── __main__.py     CLI (python -m backend.analyzer)
Design decisions:

Parse once, share the tree. The source is parsed a single time; every
analyzer works on the same ast.Module.
One traversal contract. _ast_utils.iter_function_defs is the single
source of truth for how functions are discovered and named, so the
structural analyzer and the complexity calculator can never disagree about
scope boundaries.
No AST nodes in the model. ModuleInfo is pure data: JSON-ready for
the future API, and directly comparable between original and migrated code
in the Phase-6 equivalence analysis.
Standard library only. Zero runtime dependencies in Phase 1.
Metric definitions
Metric
Definition
total lines	physical lines (splitlines)
blank / comment lines	whitespace-only / first non-whitespace char is #; docstrings count as code (runtime expressions)
code lines	total − blank − comment
functions / classes / imports	every def (incl. methods and nested), every class, every import statement
function length	end_lineno − lineno + 1 (physical lines)
max nesting depth	control-flow blocks per scope; elif/else chains do not deepen; function/class bodies restart at 0
cyclomatic complexity	1 + decision points: each if/elif, ternary, for/async for, while, except handler, assert, n−1 per and/or chain of n operands, each comprehension for clause and if filter, each match case

Supported Python subset (Phase 1)
Handled: module/function/class structure (methods, nested functions,
decorators, bases, class attributes); all import forms (relative, aliased);
if/elif/else, for, async for, while, loop else, try/except/ finally, with; ternaries; boolean chains; comprehensions (decision
points); lambdas (attributed to the enclosing scope); the full parameter
model (positional, positional-only, defaults, annotations, *args,
keyword-only, **kwargs).

Not yet modeled (documented limitations, improved in later phases):
scope/type-resolved call graph (currently name-based, hence an
over-approximation), comprehension-scoped variables, global/nonlocal,
attribute/subscript assignment targets (self.x = ...), names bound by
nested def/class statements, implicit returns, match subject structure.

Security
Phase 1 is static only: analyzed code is parsed, never executed. Sandboxed
execution (isolated subprocess, timeouts, restricted environment) arrives with
Phase 5 and will be documented together with its limitations.

text


---

## 5. Running the tests

From the repository root (`codemorph/`):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -v
Expected output (41 tests — I cannot execute code in this chat, so I derived every expected value by hand from the fixture: line numbers, blank-line count (19), complexity values, variable orders, dependency graph):

text

tests/test_ast_analyzer.py .......................      [ 56%]
tests/test_complexity.py .......                      [ 73%]
tests/test_metrics.py ....                            [ 83%]
tests/test_service.py .......                         [100%]

======================== 41 passed ========================
Example CLI run (python -m backend.analyzer tests/fixtures/calculator.py), elided in the middle:

text

Module: calculator.py
│
├── Imports (2)
│   ├── import math  [line 8]
│   └── from typing import List, Optional  [line 9]
│
├── Module variables (2): DISCOUNT_THRESHOLD, TAX_RATE
│
├── Functions (8)
│   ├── Function: validate_amount (lines 16-20, 5 lines)
│   │   ├── Parameters: amount: float
│   │   ├── Decorators: (none)
│   │   ├── Variables: (none)
│   │   ├── Conditions: 1
│   │   ├── Loops: (none)
│   │   ├── Exceptions handled: (none)
│   │   ├── Exceptions raised: ValueError
│   │   ├── Calls: (none)
│   │   └── Returns: 1 (line 20)
│   ├── Function: calculate_total (lines 28-42, 15 lines)
│   │   ├── Parameters: amount: float, items: Optional[List[str]] = None
│   │   ├── Variables: validated, tax, total, items, item, receipt
│   │   ├── Conditions: 2
│   │   ├── Loops: for (line 35)
│   │   ├── Exceptions handled: TypeError, ValueError (line 40)
│   │   ├── Exceptions raised: (none)
│   │   ├── Calls: validate_amount (line 30), calculate_tax (line 31), len (line 36), math.fsum (line 39)
│   │   └── Returns: 1 (line 42)
│   ├── Function: Calculator.add [method] (lines 51-56, 6 lines)          …
│   ├── Function: Calculator._run_nested.clamp [nested] (lines 59-62, …)  …
│   ├── Function: main (lines 66-71, 6 lines)                             …
├── Classes (1)
│   └── Class: Calculator (lines 45-63)
│       ├── Bases: (none)
│       ├── Decorators: (none)
│       ├── Methods (3): __init__, add, _run_nested
│       └── Class variables: (none)
└── Internal dependencies (3)
    ├── Calculator._run_nested → clamp
    ├── calculate_total → calculate_tax, validate_amount
    └── main → Calculator.add, calculate_total

Metrics
  Lines: 75 total | 56 code | 19 blank | 0 comment
  Functions: 8 (3 methods) | Classes: 1 | Imports: 2
  Max nesting depth: 2 | Function length: max 15, avg 5.88

Cyclomatic complexity (McCabe)
  Module level: 2 | Total: 19 | Functions: 8
  calculate_total: 5 (rank A)
  validate_amount: 2 (rank A)
  …
6. What was implemented — design notes
Scope-aware traversal as the single contract. The subtle part of AST analysis over Python is scoping: methods, nested functions, and module-level code all interleave. Instead of each module re-implementing "what is a function?", _ast_utils.iter_function_defs walks the tree once with a parent_kind (module/class/function) and yields every def with its qualified name (Calculator._run_nested.clamp), is_method, and is_nested flags. The structural analyzer and the complexity calculator consume the same iterator — they cannot disagree.

Per-function facts exclude nested scopes. _FunctionDetails deliberately does not descend into nested defs (they are analyzed as their own functions) — so clamp's if is counted in clamp, not in _run_nested. This is exactly the semantics the later data-flow and equivalence phases need.

Deliberate, documented judgment calls (all tested):

elif chains do not increase nesting depth (they're branch alternatives, not nesting), but do count toward complexity — matching how the same code reads to a human.
raise ValueError(x) is recorded in raises, not double-counted as a call.
Loop targets (for item in …) and with … as x bindings count as variables; attribute/subscript stores (self.x = …) do not (mutations, not bindings).
Docstrings count as code lines (they are runtime expressions); comments are a line-level heuristic.
Correctness-critical detail: positional defaults align to the end of the positional parameter list (first_default = n_positional − len(defaults)), which is how CPython stores them — tested with mixed signatures.

The call graph is honest about being approximate. It resolves calls by simple-name matching (last dotted segment), which over-approximates (obj.add() matches any add). This is documented rather than hidden; Phase 3's data-flow analysis refines it.

Forward-looking structure: ModuleInfo contains zero AST references, so FileAnalysis.to_dict() is directly JSON-serializable (tested) — ready for the FastAPI layer (Phase 9) and for structural diffing between original and migrated code (Phase 6).

1. Phase tracker — change row 2 to:

| 2 | Static-analysis rules (code smells, findings engine) | done |

2. Architecture tree — add after the complexity.py line:

├── findings.py code-smell rules engine: Finding, FindingsEngine

3. New section — insert after "Metric definitions":

Static-analysis findings (Phase 2)
FindingsEngine consumes a FileAnalysis and emits Finding records withthe exact schema file / line / category / severity / message / suggestion.Run via python -m backend.analyzer file.py --findings orrun_findings(analysis). Thresholds live in FindingsConfig.

Category	Severity	Fires when
UNUSED_IMPORT	MEDIUM	imported name never read anywhere (per-name granularity)
UNUSED_VARIABLE	LOW	binding never read in its scope
LONG_FUNCTION	MEDIUM	function exceeds 50 lines
DEEP_NESTING	MEDIUM	nesting exceeds 3 (finding points at the offending line)
HIGH_COMPLEXITY	MEDIUM / HIGH	cyclomatic complexity > 10 / > 20
EXCESSIVE_BRANCHING	MEDIUM	more than 8 conditionals in a function
DUPLICATED_PATTERN	LOW	≥ 3 identical consecutive statements
MISSING_ERROR_HANDLING	MEDIUM (I/O) / LOW (parse)	risky call outside any try body
BARE_EXCEPT	MEDIUM	except: without a type
DANGEROUS_EVAL / _EXEC	HIGH	builtin eval() / exec() call
Documented false-positive directions (by design)
Unused names: reads are credited to the innermost binding scope —shadowed imports resolve correctly — but x += 1 / del x count as uses,_-prefixed names are exempt, parameters are never reported, andglobal/nonlocal conservatively mark names used. Unused functions arenot reported (library code defines APIs it never calls itself).
Missing error handling is intraprocedural: a risky call inside a helperis flagged even when every current caller wraps the call. Guard semanticsmatch Python exactly — try bodies guard; else clauses, handlers, andfinally do not; guards reset at function boundaries.
Exemptions that prevent noise: __future__ imports, star imports,__all__ re-exports, and method calls named eval/exec (e.g. PyTorch'smodel.eval()).
Quoted (string) type annotations are not parsed, so imports used onlyinside them may be falsely flagged.
Phase 3's data-flow analysis refines the unused-variable rule with properdef-use chains.

In the phase tracker, change row 3 to | 3 | Control-flow & data-flow analysis | **done** |.

In the architecture tree, after complexity.py:

├── control_flow.py per-function CFGs (blocks, branches, loops, handlers)├── data_flow.py reaching definitions, def-use chains, dead stores

Add CLI usage examples and these two sections (after "Static-analysis findings"):

Control-flow analysis (Phase 3)
build_cfgs(tree) builds one CFG per function: entry/exit, basicblocks, condition nodes (if/while tests), loop headers (for/while),handler nodes, match nodes. Edge kinds: normal, true, false,case, loop_back, break, continue, exception, return.Correct-by-construction details: break skips the loop's else clause;continue targets the loop header; loop else runs only on normaltermination; falling off the end becomes an implicit return edge;uncaught raise becomes an exception edge to exit; unreachable nodes arereported as dead code.

Documented approximations: exception edges are conservative (every nodeinside a try body may jump to any handler of that try); raise connectsonly to the innermost enclosing handlers; exceptional flow throughfinally is not modeled (finally runs on the normal and handler paths);with is transparent; the CFG is intraprocedural.

CLI: --flow prints text renderings; --dot out.dot exports Graphviz.

Data-flow analysis (Phase 3)
build_data_flows(cfgs, module) runs classic reaching-definitions(gen/kill fixpoint over the CFG) and produces per function: definitions(params, assignments, loop targets, except-bindings, imports, deletes),uses with the definitions that may reach them (def-use chains), producer →consumer chains (amount -> validated -> tax -> total -> return),external inputs (module-level and builtin names), dead stores (definitionsreaching no use), and possibly-undefined uses (uses with no reachingdefinition on any path). flow_findings() exposes the last two asFinding records (POSSIBLY_UNDEFINED_USE / DEAD_STORE), separate fromthe Phase-2 lexical engine — flow analysis finds what lexical rules cannot(e.g. x = compute(); x = 5 is a dead store, and an unused self is astaticmethod candidate).

Limitations: may-analysis (use-before-def under-reported); closure readsare approximated (loads inside nested scopes minus names they bind);comprehension targets bind in the enclosing scope; del is modeled as akilling definition. These feed the Phase-6 equivalence comparison.

Phase tracker row 5 → | 5 | Syntax validation + sandboxed test execution | **done** |. Architecture tree, after the migration/ block:

├── verification/│ ├── sandbox.py isolated subprocess execution (Phase 5)│ ├── syntax_checker.py compile()-based syntax gate│ ├── test_generator.py signature-driven test inputs│ └── test_runner.py differential execution + comparison

New section after "Deterministic transformations":

Sandboxed verification (Phase 5)
backend/verification/ adds the execution half of the pipeline:

syntax_checker — check_syntax() compiles generated code and returnsa structured result. compile() (not ast.parse) is used deliberately:it also rejects misplaced __future__ imports and other post-parseerrors that would fail at import time.
test_generator — derives deterministic test inputs per function:normal / boundary / empty / invalid / default cases. Types come fromplain-builtin annotations, else documented name heuristics, else int.Methods, nested and async functions, _-prefixed names and main areskipped.
sandbox — every execution happens in a separate python -I processwith a near-empty environment (host env vars and API keys never passthrough), a fresh temporary working directory, a wall-clock timeout(CODEMORPH_EXEC_TIMEOUT, default 5s), and best-effort POSIX rlimits(memory, CPU, file size) plus setsid. The program is fed via stdin,never argv.
test_runner — verify_migration(original, migrated) runs bothversions on the same generated cases and compares return values (repr),raised exceptions (type), and captured stdout (observable side effect).Per case: PASS / FAIL / ERROR.
CLI: python -m backend.analyzer file.py --verify

Security limitations (documented, not hidden)
Process-level isolation, not a container: absolute-path filesystemaccess and network access remain possible inside the sandbox; only thedirect child is killed on timeout; Windows skips rlimits. The comparisonis heuristic: repr equality (address-bearing reprs compared structurally),exception type equality, stdout equality. This is differential testing,not proof.

Interpretation caveat (found in testing)
Legacy code that does not execute on the current runtime (Python 2 idiomssuch as dict.has_key) cannot pass differential comparison: the originalcrashes while the migration returns a value, so every case reports adivergence — even though the migration repaired the code. FAIL outcomesmust be read together with the Phase-4 transformation registry; this isprecisely the static-analysis-plus-testing combination CodeMorph studies.

Phase tracker row 6 → | 6 | Behavioral/semantic-equivalence estimation | **done** |. Architecture tree: add │ └── equivalence.py multi-signal equivalence estimate (Phase 6) under verification/. New section after "Sandboxed verification":

Semantic-equivalence estimation (Phase 6)
compute_equivalence(original, migrated) fuses four signals into oneestimated score (0–100%) with a per-signal breakdown:

Signal	Source	Compares
structural	Phase-1 module model	function-name Jaccard, 7-feature signature similarity, classes/methods, module variables
control_flow	Phase-3 CFGs	node-kind & edge-kind multiset Jaccard, statement-count ratio, per function
data_flow	Phase-3 def-use chains	params, defined/used variables, producer→consumer edges, external inputs, per function
test_behavior	Phase-5 sandboxed tests	PASS fraction over decisive cases (ERRORs excluded and noted)
Aggregate = weighted mean over available signals (default equal weights;run_tests=False gives a fast static-only estimate, explicitly noted).Functions present in only one version score 0. Labels: very-high ≥95,high ≥80, moderate ≥60, low ≥40, very-low below; invalid for syntaxfailures (score 0).

CLI: python -m backend.analyzer file.py --equivalence

This is an estimate, not a proof
Two blind spots are pinned by tests in the suite:

Static signals are blind to structure-preserving changes —x + 1 → x + 2 scores 100% on all three static signals; only thetest signal catches it (aggregate drops to 83%).
Tests cannot distinguish "migration broke the code" from "the originalnever ran here" — a Python 2 idiom in the original fails every testeven when the migration repaired it (static 100%, aggregate 75%).FAIL outcomes must be interpreted with the Phase-4 transformationregistry.
Additional limitations: signature-derived test inputs cover a shallowinput space; comparison is repr/exception-type/stdout based; CFGsimilarity uses kind distributions, not isomorphism. Every report carriesthe disclaimer in notes and to_dict().

Phase tracker row 7 → | 7 | LLM integration (provider abstraction) | **done** |
Architecture tree, under migration/: add │ ├── llm_migrator.py provider abstraction + gated LLM pipeline (Phase 7) and │ └── __main__.py CLI: python -m backend.migration
New section after "Semantic-equivalence estimation":
LLM-assisted migration (Phase 7)
backend/migration/llm_migrator.py adds the optional LLM layer. The promptis built from static analysis (source, metrics, findings, structuresummary) plus seven machine-checkable constraints — the model neverrewrites blind. Its output passes the same gates as any generated code:fenced-code extraction → compile() syntax gate → the Phase-4 structuralguard → re-analysis (findings before/after) → Phase-5 sandboxeddifferential tests → the Phase-6 equivalence estimate. Rejections returnthe original source unchanged with a recorded reason; accepted-but-weakresults are flagged (failed cases / low equivalence), never silentlytrusted.

Providers: none (default, no network) and openai (anyOpenAI-compatible chat-completions endpoint). Configuration isenvironment-only: CODEMORPH_LLM_PROVIDER, CODEMORPH_LLM_API_KEY,CODEMORPH_LLM_MODEL, CODEMORPH_LLM_BASE_URL, CODEMORPH_LLM_TIMEOUT.The API key is held in memory and never written to results, prompts, orlogs (tested). New providers plug in by implementing the LLMProviderprotocol. CLI: python -m backend.migration file.py [--llm] [--json];with no flags it prints the prompt without calling any provider.

Limitations: the request carries source code to the configured endpoint;no retry logic; temperature pinned to 0.

Implementation notes — the decisions that matter
Every gate from the spec is a distinct, testable status. NOT_CONFIGURED / PROVIDER_ERROR / NO_CODE / INVALID_SYNTAX / STRUCTURAL_REJECTION / ACCEPTED — with the original returned unchanged on every rejection, and flagged on accepted-but-suspicious. The behavior-change test proves the "flag, don't trust" path: x + 1 → x + 2 is accepted (signatures intact) but flagged with 2 failed cases and an 83% estimate.
The prompt is a deterministic pure function of the analysis — same module, same prompt, byte-for-byte (tested). That matters for Phase 10: reproducible prompts mean reproducible experiments.
Constraints are enforced, not just requested. The seven numbered constraints in the prompt map directly onto the Phase-4 structural guard that runs afterwards — the LLM is told exactly what will reject it.
Key hygiene is tested, not claimed: the key never appears in the result JSON, the prompt, the raw response, or error strings (a dedicated test injects a secret-shaped key and fails the HTTP call to prove it).
Cross-phase reuse as promised: Phase-4 guard, Phase-5 syntax checker and differential tests, Phase-6 equivalence — the LLM layer is a composition of the existing verification stack, ~80 lines of glue on top of 5 phases of machinery.

Repository-level analysis (Phase 8)
analyze_repository(root) discovers Python files (sorted, deterministic;virtualenvs, caches, build output, and VCS metadata pruned), runs the fullPhase 1-3 analysis per file, aggregates lexical + flow-sensitive findings,resolves imports into cross-file dependency edges (name-based approximationdocumented in the module docstring: <mod>.py, <mod>/__init__.py, andancestor package inits; no sys.path knowledge), computes fan-in/fan-out,and ranks files by risk = 3*HIGH + 2*MEDIUM + 1*LOW + max_complexity + fan_in. Unreadable/unparseable files get read_error/parse_errorstatuses and are excluded from metrics and ranking, reported separately.

CLI: `python -m backend.repository ./path/to/repo [--json] [--top N]

Implementation notes — the decisions that matter
Error files are first-class citizens, not crashes. A repo with a SyntaxError or a non-UTF-8 file is normal; discovery keeps them, analysis records parse_error/read_error with the message, and the report has a dedicated errors section (an unparseable file is its own alarm — so it's excluded from risk ranking rather than silently scored).
The dependency resolver is honest about being name-based. It resolves against the discovered set only — file's directory and repo root for absolute imports, per-level for relative ones — counting ancestor __init__.py files (importing pkg.mod executes pkg/__init__.py). No sys.path, no installed packages: import os simply yields no edge. All documented, all tested, including the from ..models import X level-2 case.
The risk heuristic is one documented formula — 3H + 2M + L + max complexity + fan-in — so every score in the report is reproducible by hand (the tests do exactly that: services = 2·2+1 + 4 + 1 = 10).
Composition over reimplementation: per-file findings reuse collect_all_findings (Phase 2 + Phase 3), metrics and complexity come from Phase 1 — the repository layer is pure aggregation, ~60 lines of glue over the existing stack.
Determinism everywhere: sorted walk, sorted results, sorted edge construction, path tie-breaks in rankings — same repo, same report, byte-for-byte (that's also what makes Phase 10's benchmark reproducible).

Phase tracker row 9 → | 9 | Web interface — 9a FastAPI backend **done**; 9b React frontend next | **in progress** |. Architecture tree gains ├── api/ (app.py, routes.py, diffing.py). New section after repository analysis:

Web API (Phase 9a — backend)
FastAPI backend exposing the pipeline over HTTP; zero analysis logic inthe API layer — every endpoint composes existing phases:

Endpoint	Method	Composes
/api/health	GET	provider factory (status only, never the key)
/api/analyze	POST	Phases 1-3 + all findings
/api/analyze/upload	POST	validated upload (.py, ≤1 MB, UTF-8)
/api/repository	POST	Phase 8 (local path, read-only)
/api/migrate	POST	Phase 4 deterministic migration
/api/llm-migrate	POST	Phase 7 gated LLM migration
/api/verify	POST	Phases 5-6 equivalence + per-case outcomes
/api/pipeline	POST	one-shot: analyze → migrate → verify → score
/api/diff	POST	aligned line diff for the comparison view
/api/experiments	GET	stored Phase-10 results (empty until run; never fabricated)
Run: uvicorn backend.api.app:app --reload → docs at /docs. Security:uploads validated before analysis; all execution goes through the Phase-5sandbox, never in-process; /api/repository reads a caller-supplied pathand must not be exposed publicly; the LLM key is never echoed (tested).CORS defaults to localhost:5173; extend via CODEMORPH_CORS_ORIGINS.

Implementation notes — the decisions that matter
The API is a composition layer, not a reimplementation. Every endpoint maps to one or two existing functions — analyze_source, transform_source, compute_equivalence, analyze_repository — so the web layer can never disagree with the CLI about results. That was the point of keeping everything in pure, serializable models since Phase 1.
Validation happens before analysis. Wrong extension → 400; oversize → 413; non-UTF-8 → 400; syntax errors → structured 422 with line/offset (the SourceParseError location contract from Phase 1, now surfaced over HTTP).
Security is enforced and tested, not claimed. The key-leak test runs at the HTTP level (inject sk-api-secret-42, force a network failure, assert it appears nowhere in the raw response text). Execution only ever routes through the sandbox. The one honest caveat — /api/repository analyzes an arbitrary local path — is documented as a localhost-only surface.
/api/experiments refuses to fabricate. It returns an explicit empty-with-note until Phase 10 writes real results — spec §12's "never fabricate research results" enforced in code.
/api/diff is a real difflib.SequenceMatcher diff with row-wise pairing inside replace hunks — the documented approximation that keeps the side-by-side view honest without token-level diffing.

Phase tracker row 9 → | 9 | Web interface (FastAPI backend + React frontend) | **done** |. Add to the architecture tree: └── frontend/ (Vite + React; build output served by the API at /). New short section after the API section:

Web frontend (Phase 9b)
Vite + React app with zero extra npm dependencies, served by the FastAPIbackend from frontend/dist (build with cd frontend && npm run build,then restart uvicorn). Four tabs mirror the pipeline: Analyze (sourceupload/editor, metrics dashboard, findings, AST structure, repositoryanalysis), Migrate (deterministic + LLM migrations with traceabletransformation cards), Verify & Compare (equivalence score with signalbars, sandboxed test outcomes, side-by-side line diff withremoved/changed/added highlighting), and Research (hypothesis, design,and experiment results — empty until Phase 10 runs, never fabricated).Frontend verification is the production build plus a served-app smoketest; the 299-test backend suite remains the regression gate.

Implementation notes
One origin, zero CORS friction. The app was designed to build into the backend: app.py's frontend/dist mount (written in Phase 9a, unused until now) serves the SPA at / while all /api/* routes keep matching first — that's why no test from Phase 9a changed.
The workflow mirrors the pipeline. Shared state in App.jsx carries source → analysis → migration across tabs; Verify & compare → buttons complete the journey, and the Verify tab pre-fills the migrated source. The demo sample (unused import, %-format, triple-increment) exercises every transformation rule in three clicks.
The Research panel refuses to lie. It renders the empty state until Phase 10 writes real measurements — the same never-fabricate constraint enforced in /api/experiments — and renders whatever columns Phase 10 actually produces, so the contract can't drift.
The diff view is honest about pairing — row-wise within replace hunks, exactly the documented approximation from diffing.py, colored red/yellow/green with stable line numbers on both panes.
No router, no UI library, no state library — tabs are useState, styling is one CSS file, the API client is ~60 lines. Every dependency the frontend has, the scaffold brought.

Phase tracker row 10 → | 10 | Research evaluation framework | **done** |. New section:

Research evaluation (Phase 10)
python -m backend.evaluation [--provider scripted|openai] runs thespec's experiment: llm_only (task sentence + source, output shippedas-is) vs codemorph (analysis-context prompt + constraints + fullrejection pipeline), same provider and model, over an 8-task benchmarkof small modernization tasks. Measured per condition: syntax success,migration success, differential test pass rate, average equivalenceestimate, introduced defects (final output fails to compile or divergeson any decisive test; safe fallback to the original is NOT a defect —that is the safety property under test), rejection rate, processingtime. Results persist to benchmark/results/ (experiments.json is whatthe Research panel reads; timestamped full details under runs/).

Honesty model: --provider scripted is a deterministic identityprovider that validates the measurement harness end-to-end; its resultsare labeled SCRIPTED CALIBRATION and are never model behavior. Realexperiments require --provider openai with environment configuration.Numbers are only ever written from executed runs. Limitations: one runper task, temperature 0, a single model, 8 small tasks — a pilot studydesign, documented as such.

Implementation notes — the decisions that matter
The independent variable is isolated by construction. Both conditions share the provider instance, the task sentence, and the output-format instruction; only the analysis context, constraints, and gates differ. Any measured divergence is attributable to the harness — the cleanest possible operationalization of the hypothesis.
The defect definition is where the research lives. LLM-only ships whatever it gets (broken syntax and behavior changes both become defects); CodeMorph's rejection-with-fallback is defined as not a defect — so introduced_defects measures exactly the safety property under test. The definition is documented in the module docstring, not buried.
Uniform measurement: both conditions' final outputs go through the same compute_equivalence (with sandboxed differential tests) — LLM-only's raw output is measured, just never gated. Fair comparison, different philosophy.
The divergence test is the thesis in miniature, with exact hand-derived numbers: scripted flaws (one syntax error, one return -1 vs return 0 behavior change) produce llm_only = 66.7% syntax / 42.9% tests / 2 defects vs codemorph = 100% syntax / 60.0% tests / 1 defect — and the one defect codemorph does ship is precisely the one its pipeline flagged for review. Gates catch breakage; tests catch subtlety; neither catches everything alone.
The calibration run is a self-test of the instrument. Identity responses must produce perfect metrics — any deviation would mean the measurement is broken, not the migration. That's the evaluation-framework equivalent of a control experiment.

CodeMorph
AI-assisted code migration & semantic-equivalence analysis — an applied-research prototype.

CodeMorph analyzes Python source with real static analysis (AST, codesmells, control-flow and data-flow graphs), modernizes it with traceabledeterministic transformations and a gated LLM layer, and estimates whetherbehavior was preserved by executing both versions in a sandbox and fusingfour independent signals into one score.

Research question. Can static program analysis combined withLLM-assisted code transformation improve the reliability of automatedcode migration compared with LLM-only transformation?

Motivation
LLM code migration is fluent but unverified: a rewrite can compile, lookright, and silently change behavior. Classical program analysis isverifiable but can't write code. CodeMorph tests the thesis that thecombination beats either alone: static analysis constrains generation(analysis-context prompts, machine-checkable constraints) and validatesit (syntax gate, structural guard, sandboxed differential testing,multi-signal equivalence estimation). Rejected generations return theoriginal unchanged; accepted-but-suspicious ones are flagged, neversilently trusted.

Architecture
Source → Parser → AST Analysis → Static Analysis (findings)
→ Control/Data Flow → Transformation (deterministic | LLM-gated)
→ Syntax Validation → Sandboxed Tests → Behavior Comparison
→ Semantic Equivalence Report

text

Enter your code here...
backend/
├── analyzer/ Phases 1-3: structure, metrics, complexity,
│ ├── ast_analyzer.py findings, per-function CFGs, reaching definitions
│ ├── control_flow.py
│ ├── data_flow.py
│ └── ...
├── migration/ Phase 4 + 7: deterministic rules; LLM pipeline
├── verification/ Phases 5-6: sandbox, test generation, equivalence
├── repository/ Phase 8: repo-level aggregation
├── evaluation/ Phase 10: benchmark + two-condition experiment
└── api/ Phase 9: FastAPI backend
frontend/ Phase 9: Vite + React (built into backend at /)
tests/ 320 tests across 12 files
benchmark/ 8 migration tasks + measured results
docs/ SECURITY.md

text


## Features

* **Static analysis** (Phases 1-3): AST structural model, code metrics,
  McCabe complexity, 11 lexical + 2 flow-sensitive finding categories,
  per-function CFGs (break/else/exception semantics), reaching-definitions
  data flow (def-use chains, dead stores, possibly-undefined uses).
* **Deterministic migration** (Phase 4): 7 traceable, syntax-preserving
  rewrites with a SAFE/REVIEW risk model, overlap resolution, structural
  guard, idempotency.
* **Sandboxed verification** (Phase 5): isolated `python -I` subprocess,
  near-empty environment, private cwd, timeouts, rlimits; deterministic
  test generation; differential PASS/FAIL/ERROR comparison.
* **Equivalence estimation** (Phase 6): structural, control-flow,
  data-flow, and test-behavior signals → weighted score with breakdown.
  **An empirical estimate — never a formal proof.**
* **LLM integration** (Phase 7): provider abstraction (`none`/`openai`),
  analysis-context prompts, full rejection pipeline, key never echoed.
* **Repository analysis** (Phase 8): discovery, cross-file dependencies,
  fan-in/out, documented risk heuristic, high-risk ranking.
* **Web interface** (Phase 9): FastAPI + React; dashboard, upload,
  side-by-side diff with change highlighting, research panel.
* **Evaluation framework** (Phase 10): benchmark, two-condition runner,
  measured metrics, JSON/CSV persistence.

## Installation

Python 3.10+ (developed on 3.12).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest                      # 320 passed
cd frontend && npm install && npm run build && cd ..   # web frontend
Usage
bash

python -m backend.analyzer file.py [--findings] [--flow] [--dot out.dot]
                                    [--migrate] [--verify] [--equivalence]
python -m backend.migration file.py [--llm]           # prompt-only by default
python -m backend.repository . [--json]
python -m backend.evaluation [--provider scripted|openai] [--tasks ...]
uvicorn backend.api.app:app --reload                  # app at /, docs at /docs
python

from backend.analyzer import analyze_source, run_findings
from backend.migration import TransformationEngine
from backend.verification import compute_equivalence

analysis = analyze_source(source, filename="legacy.py")
migration = TransformationEngine().transform_source(source, "legacy.py")
report = compute_equivalence(source, migration.migrated_source)
print(report.score_percent, report.label)   # e.g. 98 very-high
Example analysis
python -m backend.analyzer legacy.py --equivalence on a module with an
unused import, %-formatting, and a triple increment prints: the
structural tree, metrics, findings, 2 traceable transformations
(each with location, before/after, reason), 7/7 sandboxed differential
cases passed, and:

text

Semantic Equivalence Estimate: 98% (very-high)
  [empirical multi-signal estimate -- NOT a formal proof of equivalence]
  structural           100%
  control_flow          92%
  data_flow            100%
  test_behavior        100%  (7/7 cases passed)
Migration workflow
Deterministic rules (risk = safety classification, see
migration/deterministic.py for the full caveat table):
HAS_KEY_TO_IN (SAFE), FORMAT_TO_FSTRING (SAFE),
PERCENT_TO_FSTRING, BARE_EXCEPT_TYPING, AUG_ASSIGN_MODERNIZE,
DUPLICATE_RUN_COLLAPSE, UNUSED_IMPORT_REMOVAL (REVIEW). Every applied
rewrite is recorded (type, line, original, replacement, reason); the
migrated source must re-parse and pass the structural guard or the
original is returned unchanged. The LLM layer adds the same contract on
top of a model: prompt from analysis context, then extraction → syntax
gate → structural guard → sandboxed tests → equivalence, with statuses
NOT_CONFIGURED / PROVIDER_ERROR / NO_CODE / INVALID_SYNTAX /
STRUCTURAL_REJECTION / ACCEPTED (+ flagged).

Semantic-equivalence methodology
Four signals: structural (function/class/variable sets, 7-feature
signature similarity), control-flow (per-function node-kind/edge-kind
multiset Jaccard + statement-count ratio — deliberately not graph
isomorphism), data-flow (params, defs, uses, producer→consumer edges,
external inputs), test-behavior (PASS fraction over decisive
differential cases). Aggregate = weighted mean over available signals
(default: equal). Blind spots are pinned by tests: static signals are
blind to x+1 → x+2; differential tests cannot distinguish "migration
broke it" from "the original never ran here" (Python 2 idioms) — FAIL
outcomes must be read with the transformation registry.
The score is an empirical behavioral estimate, not a formal proof.

Experimental methodology
Independent variable: migration approach — llm_only (task sentence +
source; output ships as-is) vs codemorph (analysis-context prompt,
constraints, full rejection pipeline). Same provider instance, model,
and 8-task benchmark. Dependent variables: syntax success, migration
success, test pass rate, average equivalence, introduced defects
(final output fails to compile or diverges on any decisive test; a safe
rejection falling back to the original is not a defect — that fallback
is the safety property under test), rejection rate, processing time.
Results persist to benchmark/results/ (the Research panel reads
experiments.json). Numbers come only from executed runs — the
scripted provider is a deterministic identity generator labeled
SCRIPTED CALIBRATION (instrument validation), never model behavior.

Research findings
Instrument validation (run python -m backend.evaluation):
identity responses measure 100% on every metric, both conditions —
the measurement harness is sound.
Controlled divergence study (engineered provider flaws: one
syntax error, one behavior change; measured in CI):
Method
Syntax
Tests
Equiv.
Defects
Rejections
llm_only	66.7%	42.9%	56.9%	2	0%
codemorph	100%	60.0%	90.3%	1	33.3%

The gates eliminated the syntax defect (safe fallback); the one
defect CodeMorph shipped was flagged for review by the differential
tests. Gates catch breakage, tests catch subtlety — neither suffices
alone, which is the thesis in miniature.
Live-model pilot: pending an API key —
export CODEMORPH_LLM_PROVIDER=openai; export CODEMORPH_LLM_API_KEY=...
then python -m backend.evaluation --provider openai. Scale caveat:
8 small tasks, one run each, temperature 0, one model — a pilot
design, reported as such.
Limitations
Supported Python subset documented per module (name-based call graph,
conservative CFG exception edges, may-analysis data flow, comprehension
scope approximation). Equivalence is heuristic (repr/exception-type/
stdout comparison over signature-derived inputs). The sandbox is
process-level isolation, not a container (see docs/SECURITY.md).
The LLM prompt carries source code to the configured endpoint. Pilot
scale. See each module docstring for the full, honest list.

Future work
Tree-sitter multi-language parsing; container-based sandbox; token-level
diff alignment; interprocedural data flow and type inference; more
providers; larger benchmark with statistical replication; repair loops
(feeding differential failures back to the LLM).

Development process
Built in 11 phase-gated increments; every phase shipped with tests that
had to pass before the next began. The gates caught every defect —
including ~6 engine bugs and roughly two dozen hand-derived-expectation
or delivery errors — before any could compound. The consistent pattern:
the analysis machinery passed first contact at high rates; failures
clustered in hand-written expectations, which is the project's own
thesis turned inward: execution beats authorial confidence.

320 tests. Standard-library analysis core; fastapi/uvicorn are the
only runtime dependencies added by the web layer.

text


## Step 4 — `docs/SECURITY.md` (terminal `mkdir -p docs`, then editor paste)

```markdown
# Security Model & Limitations

CodeMorph analyzes and (for verification) executes source code. This
document states what is enforced and — equally important — what is not.

## Enforced

* **Upload validation before analysis**: `.py` only, ≤1 MB, UTF-8;
  structured 422 for syntax errors; no analysis of rejected input.
* **Sandboxed execution** (`verification/sandbox.py`): analyzed and
  generated code runs in a separate `python -I` process with a
  near-empty environment (host env vars — including any API key — never
  reach it; tested), a private temporary cwd, a wall-clock timeout
  (`CODEMORPH_EXEC_TIMEOUT`, default 5 s), best-effort POSIX rlimits
  (address space, CPU, file size) and `setsid`. Programs are delivered
  via stdin, never argv.
* **API-key hygiene**: the key is read from the environment, held in
  memory, and never written to results, prompts, logs, or error strings
  (tested at unit and HTTP level).
* **No fabricated results**: the evaluation store is populated only by
  executed runs; the API and UI render an explicit empty state otherwise.

## Not enforced (documented limitations)

* **Process isolation, not a container.** With absolute paths the child
  can read the host filesystem; `import os` works inside the sandbox.
  Full isolation requires a container/jail runner (future work).
* **Network access is not blocked** inside the sandbox.
* Only the direct child is killed on timeout; grandchildren are not
  tracked (`setsid` limits the blast radius, not the guarantee).
* Non-POSIX platforms skip rlimits.
* **`POST /api/repository` analyzes a caller-supplied local path.**
  Read-only, but it discloses repository structure to the caller — do
  not expose the server on an untrusted network.
* **LLM requests carry your source code** to the configured endpoint;
  do not point CodeMorph at third-party endpoints with proprietary code.
* The Codespaces port-forwarding workflow (Public visibility) exposes
  the demo server to anyone holding the URL — keep the port Private
  outside active demos, and never with a real API key configured.

## Recommended deployment

Treat CodeMorph as a localhost research tool. If it must be shared, put
it behind authentication, keep the LLM key server-side only, and
restrict `/api/repository`.