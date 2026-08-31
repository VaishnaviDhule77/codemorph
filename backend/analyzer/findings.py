"""Static-analysis findings engine (Phase 2): code smells & risky constructs.

Consumes a Phase-1 :class:`~backend.analyzer.service.FileAnalysis` and emits
:class:`Finding` records -- one per detected issue -- each carrying file,
line, category, severity, message, and a suggested improvement.

Design policy: every rule is deliberately conservative where precision would
require information the tool does not have yet (types, interprocedural data
flow). The false-positive direction of each rule is documented in the README.
Data-flow analysis (Phase 3) will refine several of these rules.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Iterator

from ._ast_utils import first_excessive_nesting_line, iter_function_defs

if TYPE_CHECKING:  # avoid a runtime import cycle with the service layer
    from .service import FileAnalysis


class Severity(str, Enum):
    """Finding severity levels (str-mixin keeps JSON serialization simple)."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Category:
    """Stable finding-category codes used by the API, CLI, and tests."""

    UNUSED_IMPORT = "UNUSED_IMPORT"
    UNUSED_VARIABLE = "UNUSED_VARIABLE"
    LONG_FUNCTION = "LONG_FUNCTION"
    DEEP_NESTING = "DEEP_NESTING"
    HIGH_COMPLEXITY = "HIGH_COMPLEXITY"
    EXCESSIVE_BRANCHING = "EXCESSIVE_BRANCHING"
    DUPLICATED_PATTERN = "DUPLICATED_PATTERN"
    MISSING_ERROR_HANDLING = "MISSING_ERROR_HANDLING"
    BARE_EXCEPT = "BARE_EXCEPT"
    DANGEROUS_EVAL = "DANGEROUS_EVAL"
    DANGEROUS_EXEC = "DANGEROUS_EXEC"


@dataclass(frozen=True)
class Finding:
    """One analyzer finding -- the exact schema required by the spec."""

    file: str
    line: int
    category: str
    severity: Severity
    message: str
    suggestion: str

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "category": self.category,
            "severity": self.severity.value,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass(frozen=True)
class FindingsConfig:
    """Thresholds for the smell rules (tunable; defaults are documented)."""

    long_function_lines: int = 50       # LONG_FUNCTION fires above this
    deep_nesting_depth: int = 3         # DEEP_NESTING fires above this
    high_complexity: int = 10           # HIGH_COMPLEXITY MEDIUM above this
    critical_complexity: int = 20       # HIGH_COMPLEXITY HIGH above this
    excessive_branching: int = 8        # EXCESSIVE_BRANCHING above this
    duplicate_run_length: int = 3       # identical consecutive statements


def severity_counts(findings: list[Finding]) -> dict[str, int]:
    """Aggregate findings by severity (used by the CLI and later the API)."""
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for finding in findings:
        counts[finding.severity.value] += 1
    return counts


# --- risky / dangerous call tables --------------------------------------------

_IO_RISKY: dict[str, str] = {
    "open": "OSError",
    "os.remove": "OSError",
    "os.rename": "OSError",
    "os.rmdir": "OSError",
    "os.makedirs": "OSError",
    "shutil.rmtree": "OSError",
}
_PARSE_RISKY: dict[str, str] = {
    "int": "ValueError",
    "float": "ValueError",
    "json.loads": "json.JSONDecodeError",
    "json.load": "json.JSONDecodeError",
}
_DANGEROUS_NAMES: dict[str, str] = {
    "eval": Category.DANGEROUS_EVAL,
    "exec": Category.DANGEROUS_EXEC,
}

_TRY_NODES: tuple[type, ...] = (ast.Try,) + (
    (ast.TryStar,) if hasattr(ast, "TryStar") else ()
)


# --- name-usage analysis (backbone of the unused-name rules) -------------------


@dataclass
class _Binding:
    """A name bound in a scope: where, and what kind of binding it is."""

    line: int
    kind: str  # "variable" | "param" | "import" | "def" | "class"


class _ScopeUsage:
    """Names bound and read within one lexical scope."""

    def __init__(self, qualified_name: str) -> None:
        self.qualified_name = qualified_name
        self.bindings: dict[str, _Binding] = {}
        self.reads: set[str] = set()


class _UsageVisitor(ast.NodeVisitor):
    """Collects per-scope bindings and reads for unused-name detection.

    Name-resolution model (an approximation of Python's LEGB rules):

    * A read of ``x`` is credited to the innermost enclosing scope that binds
      ``x`` and *stops there* -- outer, shadowed bindings are not credited.
      This is what makes ``import os`` + a function-local ``os = 1`` resolve
      correctly (the import stays unused).
    * ``import`` names always bind at module scope regardless of where the
      import statement appears (documented approximation).
    * Parameters, definitions, and imports are bound but are reported by
      their own dedicated rules (or deliberately not at all).
    * ``global``/``nonlocal`` declarations conservatively mark the name as
      used in every visible scope.
    * Comprehension targets bind in the enclosing scope (documented
      approximation). Comprehensions are traversed in Python's evaluation
      order -- iterable, then target, then filters, then the element
      expressions -- so reads inside the element resolve against the target
      binding instead of evaporating (see ``_visit_comprehension``).
    """

    def __init__(self) -> None:
        self.module_scope = _ScopeUsage("<module>")
        self.scopes: list[_ScopeUsage] = [self.module_scope]
        self.all_scopes: list[_ScopeUsage] = [self.module_scope]
        self._prefix_stack: list[str] = [""]  # mirrors Phase-1 qualified names

    # -- binding / reading ---------------------------------------------------

    def _bind(self, name: str, line: int, kind: str, scope: "_ScopeUsage | None" = None) -> None:
        target = scope if scope is not None else self.scopes[-1]
        if name not in target.bindings:  # first binding wins (reporting line)
            target.bindings[name] = _Binding(line=line, kind=kind)

    def _read(self, name: str) -> None:
        for scope in reversed(self.scopes):
            if name in scope.bindings:
                scope.reads.add(name)
                return
        # No binder in the scope chain: a builtin (len, print, ...) -- nothing
        # to credit.

    # -- name contexts ----------------------------------------------------------

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._read(node.id)
        elif isinstance(node.ctx, ast.Store):
            self._bind(node.id, node.lineno, "variable")
        else:  # ast.Del -- deletion requires a prior binding; count as usage
            self._read(node.id)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Name):
            self._read(node.target.id)  # read-modify-write counts as a use
        self.generic_visit(node)
        if isinstance(node.target, ast.Name) and node.target.id == "__all__":
            for name in _exported_names(node.value):
                self.module_scope.reads.add(name)

    def visit_Assign(self, node: ast.Assign) -> None:
        # ``__all__ = [...]`` re-exports names: they count as used.
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__all__"
        ):
            for name in _exported_names(node.value):
                self.module_scope.reads.add(name)
        self.generic_visit(node)

    # -- imports (always bound at module scope; documented approximation) -----

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name != "*":
                bound = alias.asname or alias.name.split(".")[0]
                self._bind(bound, node.lineno, "import", scope=self.module_scope)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name != "*":
                self._bind(alias.asname or alias.name, node.lineno, "import",
                           scope=self.module_scope)

    # -- scopes -----------------------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: "ast.FunctionDef | ast.AsyncFunctionDef") -> None:
        # Decorators, defaults, and annotations evaluate in the ENCLOSING scope.
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in _defaults_of(node.args):
            self.visit(default)
        for arg in _iter_arg_nodes(node.args):
            if arg.annotation is not None:
                self.visit(arg.annotation)
        if node.returns is not None:
            self.visit(node.returns)
        self._bind(node.name, node.lineno, "def")

        qualified = self._prefix_stack[-1] + node.name
        scope = _ScopeUsage(qualified)
        self.scopes.append(scope)
        self.all_scopes.append(scope)
        self._prefix_stack.append(qualified + ".")
        for arg in _iter_arg_nodes(node.args):
            self._bind(arg.arg, node.lineno, "param", scope=scope)
        for stmt in node.body:
            self.visit(stmt)
        self._prefix_stack.pop()
        self.scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._bind(node.name, node.lineno, "class")

        qualified = self._prefix_stack[-1] + node.name
        scope = _ScopeUsage(qualified)
        self.scopes.append(scope)
        self.all_scopes.append(scope)
        self._prefix_stack.append(qualified + ".")
        for stmt in node.body:
            self.visit(stmt)
        self._prefix_stack.pop()
        self.scopes.pop()

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name is not None:  # ``except E as err:`` binds err
            self._bind(node.name, node.lineno, "variable")
        for stmt in node.body:
            self.visit(stmt)

    # -- explicit scope declarations ----------------------------------------------

    def visit_Global(self, node: ast.Global) -> None:
        # Conservative: assume the name escapes this scope and is used.
        for name in node.names:
            for scope in self.scopes:
                scope.reads.add(name)

    visit_Nonlocal = visit_Global

    # -- comprehensions (evaluation-order-sensitive) ------------------------------

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node, parts=[node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node, parts=[node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node, parts=[node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node, parts=[node.key, node.value])

    def _visit_comprehension(
        self,
        node: "ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp",
        parts: list[ast.expr],
    ) -> None:
        """Visit a comprehension in Python's evaluation order.

        For each ``for`` clause the *iterable* is evaluated BEFORE the target
        is bound (in real Python, ``[x for x in x]`` reads the outer ``x``),
        then the ``if`` filters, and finally the element/key/value
        expressions. AST field order lists ``elt`` before ``generators``, so
        the generic traversal recorded the element's read of the target
        before the target was bound -- falsely flagging e.g. ``v`` in
        ``[clamp(v) for v in values]`` as unused (regression test in
        test_findings.py).
        """
        for comp in node.generators:
            self.visit(comp.iter)
            self.visit(comp.target)
            for if_clause in comp.ifs:
                self.visit(if_clause)
        for part in parts:
            self.visit(part)


def _collect_usage(tree: ast.Module) -> _UsageVisitor:
    visitor = _UsageVisitor()
    visitor.visit(tree)
    return visitor


# --- small AST helpers ------------------------------------------------------------


def _iter_arg_nodes(args: ast.arguments) -> Iterator[ast.arg]:
    for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
        yield arg
    if args.vararg is not None:
        yield args.vararg
    if args.kwarg is not None:
        yield args.kwarg


def _defaults_of(args: ast.arguments) -> list[ast.expr]:
    defaults = list(args.defaults)
    defaults.extend(d for d in args.kw_defaults if d is not None)
    return defaults


def _exported_names(node: ast.expr) -> list[str]:
    """String constants of an ``__all__`` list/tuple/set literal."""
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [
            elt.value
            for elt in node.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        ]
    return []


def _call_name(func: ast.expr) -> "str | None":
    """Best-effort dotted name of a call target, e.g. ``json.loads``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = _call_name(func.value)
        return f"{base}.{func.attr}" if base is not None else None
    if isinstance(func, ast.Call):
        return _call_name(func.func)
    return None


def _iter_statement_lists(node: ast.AST) -> Iterator[list[ast.stmt]]:
    """Yield every statement list: module body, function bodies, blocks."""
    for _, value in ast.iter_fields(node):
        if isinstance(value, list) and value and all(
            isinstance(item, ast.stmt) for item in value
        ):
            yield value
    for child in ast.iter_child_nodes(node):
        yield from _iter_statement_lists(child)


# --- the engine ---------------------------------------------------------------------


class FindingsEngine:
    """Runs all Phase-2 rules over one :class:`FileAnalysis`."""

    def __init__(self, config: "FindingsConfig | None" = None) -> None:
        self.config = config if config is not None else FindingsConfig()

    def analyze(self, analysis: "FileAnalysis") -> list[Finding]:
        """Return all findings for one file, sorted by (line, category)."""
        usage = _collect_usage(analysis.tree)
        function_nodes = {
            scoped.qualified_name: scoped.node
            for scoped in iter_function_defs(analysis.tree)
        }

        findings: list[Finding] = []
        findings.extend(self._unused_imports(analysis, usage))
        findings.extend(self._unused_variables(analysis, usage))
        findings.extend(self._size_and_complexity(analysis, function_nodes))
        findings.extend(self._bare_excepts(analysis))
        findings.extend(self._dangerous_calls(analysis))
        findings.extend(self._missing_error_handling(analysis))
        findings.extend(self._duplicated_patterns(analysis))
        findings.sort(key=lambda f: (f.line, f.category, f.message))
        return findings

    # -- rule: UNUSED_IMPORT ----------------------------------------------------

    def _unused_imports(self, analysis: "FileAnalysis", usage: _UsageVisitor) -> list[Finding]:
        findings: list[Finding] = []
        for imp in analysis.module.imports:
            if imp.is_from and imp.module == "__future__":
                continue  # compiler directives -- never read by design
            for name in imp.bound_names:
                if name == "*" or name in usage.module_scope.reads:
                    continue
                if imp.is_from:
                    message = (
                        f"Imported name '{name}' from module "
                        f"'{imp.module}' is never used in this module."
                    )
                else:
                    message = f"Import '{name}' is never used in this module."
                findings.append(
                    Finding(
                        file=analysis.filename,
                        line=imp.lineno,
                        category=Category.UNUSED_IMPORT,
                        severity=Severity.MEDIUM,
                        message=message,
                        suggestion="Remove the import; if it is re-exported "
                                   "on purpose, list the name in __all__.",
                    )
                )
        return findings

    # -- rule: UNUSED_VARIABLE -----------------------------------------------------

    def _unused_variables(self, analysis: "FileAnalysis", usage: _UsageVisitor) -> list[Finding]:
        findings: list[Finding] = []
        for scope in usage.all_scopes:
            for name, binding in sorted(scope.bindings.items()):
                if binding.kind != "variable":
                    continue  # params/imports/defs have their own rules
                if name.startswith("_"):
                    continue  # '_'-prefix signals intentional non-use
                if name in scope.reads:
                    continue
                if scope.qualified_name == "<module>":
                    message = (
                        f"Module-level variable '{name}' is assigned but never read."
                    )
                else:
                    message = (
                        f"Variable '{name}' in function "
                        f"'{scope.qualified_name}' is assigned but never read."
                    )
                findings.append(
                    Finding(
                        file=analysis.filename,
                        line=binding.line,
                        category=Category.UNUSED_VARIABLE,
                        severity=Severity.LOW,  # false-positive-prone by nature
                        message=message,
                        suggestion="Remove the assignment or use the value; "
                                   "prefix the name with '_' if the non-use "
                                   "is intentional.",
                    )
                )
        return findings

    # -- rules: LONG_FUNCTION / DEEP_NESTING / HIGH_COMPLEXITY / EXCESSIVE_BRANCHING

    def _size_and_complexity(
        self, analysis: "FileAnalysis", function_nodes: dict
    ) -> list[Finding]:
        cfg = self.config
        findings: list[Finding] = []

        # Cyclomatic complexity reuses the Phase-1 complexity report directly.
        for fc in analysis.complexity.functions:
            if fc.complexity > cfg.critical_complexity:
                severity = Severity.HIGH
            elif fc.complexity > cfg.high_complexity:
                severity = Severity.MEDIUM
            else:
                continue
            findings.append(
                Finding(
                    file=analysis.filename,
                    line=fc.lineno,
                    category=Category.HIGH_COMPLEXITY,
                    severity=severity,
                    message=f"Function '{fc.qualified_name}' has high "
                            f"cyclomatic complexity ({fc.complexity}).",
                    suggestion="Consider splitting conditional branches into "
                               "smaller functions.",
                )
            )

        for fn in analysis.module.functions:
            if fn.length > cfg.long_function_lines:
                findings.append(
                    Finding(
                        file=analysis.filename,
                        line=fn.lineno,
                        category=Category.LONG_FUNCTION,
                        severity=Severity.MEDIUM,
                        message=f"Function '{fn.qualified_name}' is {fn.length} "
                                f"lines long (threshold {cfg.long_function_lines}).",
                        suggestion="Split the function into smaller, focused "
                                   "units with a single responsibility.",
                    )
                )
            if fn.num_conditions > cfg.excessive_branching:
                findings.append(
                    Finding(
                        file=analysis.filename,
                        line=fn.lineno,
                        category=Category.EXCESSIVE_BRANCHING,
                        severity=Severity.MEDIUM,
                        message=f"Function '{fn.qualified_name}' has "
                                f"{fn.num_conditions} conditional branches.",
                        suggestion="Replace long if/elif chains with a lookup "
                                   "table, match statement, or polymorphism.",
                    )
                )
            if fn.max_nesting > cfg.deep_nesting_depth:
                node = function_nodes.get(fn.qualified_name)
                line = (
                    first_excessive_nesting_line(node, cfg.deep_nesting_depth)
                    if node is not None
                    else fn.lineno
                )
                findings.append(
                    Finding(
                        file=analysis.filename,
                        line=line,
                        category=Category.DEEP_NESTING,
                        severity=Severity.MEDIUM,
                        message=f"Code nested {fn.max_nesting} levels deep in "
                                f"function '{fn.qualified_name}' (threshold "
                                f"{cfg.deep_nesting_depth}).",
                        suggestion="Reduce nesting with early returns, guard "
                                   "clauses, or helper functions.",
                    )
                )
        return findings

    # -- rule: BARE_EXCEPT ------------------------------------------------------------

    def _bare_excepts(self, analysis: "FileAnalysis") -> list[Finding]:
        findings: list[Finding] = []
        for node in ast.walk(analysis.tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                findings.append(
                    Finding(
                        file=analysis.filename,
                        line=node.lineno,
                        category=Category.BARE_EXCEPT,
                        severity=Severity.MEDIUM,
                        message="Bare 'except:' catches every exception, "
                                "including SystemExit and KeyboardInterrupt.",
                        suggestion="Catch specific exception types, e.g. "
                                   "'except ValueError:'.",
                    )
                )
        return findings

    # -- rules: DANGEROUS_EVAL / DANGEROUS_EXEC ------------------------------------------

    def _dangerous_calls(self, analysis: "FileAnalysis") -> list[Finding]:
        findings: list[Finding] = []
        for node in ast.walk(analysis.tree):
            if not isinstance(node, ast.Call):
                continue
            # Plain Name calls only: ``obj.eval()`` (e.g. torch's
            # ``model.eval()``) is a method call, not the builtin -- flagging
            # it would be a false positive.
            if not isinstance(node.func, ast.Name):
                continue
            category = _DANGEROUS_NAMES.get(node.func.id)
            if category is None:
                continue
            name = node.func.id
            suggestion = (
                "Avoid eval(); parse trusted data with ast.literal_eval() instead."
                if name == "eval"
                else "Avoid exec(); move the logic into real functions or "
                     "importable modules."
            )
            findings.append(
                Finding(
                    file=analysis.filename,
                    line=node.lineno,
                    category=category,
                    severity=Severity.HIGH,
                    message=f"Use of {name}() executes arbitrary code with the "
                            f"program's privileges.",
                    suggestion=suggestion,
                )
            )
        return findings

    # -- rule: MISSING_ERROR_HANDLING ------------------------------------------------------

    def _missing_error_handling(self, analysis: "FileAnalysis") -> list[Finding]:
        """Flag risky calls that are not inside any ``try`` body.

        Guard semantics follow real Python: only the ``try`` **body** is
        protected. Exceptions raised in ``except`` handlers, the ``else``
        clause, or ``finally`` are NOT caught by the same ``try``. Guards are
        intraprocedural: a function defined inside a ``try`` body only binds
        there; its body runs whenever the function is called.
        """
        findings: list[Finding] = []

        def visit(node: ast.AST, guarded: bool) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    visit(decorator, guarded)
                for default in _defaults_of(node.args):
                    visit(default, guarded)
                for arg in _iter_arg_nodes(node.args):
                    if arg.annotation is not None:
                        visit(arg.annotation, guarded)
                if node.returns is not None:
                    visit(node.returns, guarded)
                for stmt in node.body:
                    visit(stmt, False)  # guard resets at scope boundary
                return
            if isinstance(node, ast.ClassDef):
                # A class body executes where the class statement appears.
                for decorator in node.decorator_list:
                    visit(decorator, guarded)
                for base in node.bases:
                    visit(base, guarded)
                for keyword in node.keywords:
                    visit(keyword.value, guarded)
                for stmt in node.body:
                    visit(stmt, guarded)
                return
            if isinstance(node, _TRY_NODES):
                for stmt in node.body:
                    visit(stmt, True)
                for handler in node.handlers:
                    if handler.type is not None:
                        visit(handler.type, guarded)
                    for stmt in handler.body:
                        visit(stmt, guarded)
                for stmt in node.orelse:
                    visit(stmt, guarded)
                for stmt in node.finalbody:
                    visit(stmt, guarded)
                return
            if isinstance(node, ast.Call):
                if not guarded:
                    name = _call_name(node.func)
                    if name in _IO_RISKY:
                        findings.append(
                            self._risk_finding(analysis, node, name,
                                               _IO_RISKY[name], Severity.MEDIUM)
                        )
                    elif name in _PARSE_RISKY:
                        findings.append(
                            self._risk_finding(analysis, node, name,
                                               _PARSE_RISKY[name], Severity.LOW)
                        )
                for child in ast.iter_child_nodes(node):
                    visit(child, guarded)
                return
            for child in ast.iter_child_nodes(node):
                visit(child, guarded)

        visit(analysis.tree, False)
        return findings

    def _risk_finding(
        self,
        analysis: "FileAnalysis",
        node: ast.Call,
        name: str,
        exception: str,
        severity: Severity,
    ) -> Finding:
        return Finding(
            file=analysis.filename,
            line=node.lineno,
            category=Category.MISSING_ERROR_HANDLING,
            severity=severity,
            message=f"Call to '{name}()' can raise {exception} and is not "
                    f"inside a try block.",
            suggestion=f"Handle {exception} (or the failure it represents) "
                       f"explicitly, or document why it cannot occur.",
        )

    # -- rule: DUPLICATED_PATTERN ------------------------------------------------------------

    def _duplicated_patterns(self, analysis: "FileAnalysis") -> list[Finding]:
        """Detect runs of identical consecutive statements (trivial copy-paste).

        Statements are compared with ``ast.dump`` (no line information), so
        formatting differences are ignored. Only *consecutive* duplicates are
        reported; non-adjacent repetition is future work.
        """
        findings: list[Finding] = []
        for body in _iter_statement_lists(analysis.tree):
            index = 0
            while index < len(body):
                signature = ast.dump(body[index])
                end = index + 1
                while end < len(body) and ast.dump(body[end]) == signature:
                    end += 1
                run_length = end - index
                if run_length >= self.config.duplicate_run_length:
                    findings.append(
                        Finding(
                            file=analysis.filename,
                            line=body[index].lineno,
                            category=Category.DUPLICATED_PATTERN,
                            severity=Severity.LOW,
                            message=f"{run_length} consecutive identical "
                                    f"statements (copy-paste pattern).",
                            suggestion="Replace the repetition with a loop, "
                                       "comprehension, or a helper function.",
                        )
                    )
                index = end
        return findings