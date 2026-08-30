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