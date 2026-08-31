"""Data-flow analysis (Phase 3): reaching definitions over the CFG.

For every function this module computes:

* **Definitions** (assignments, parameters, loop targets, except-bindings,
  imports, deletes) located on CFG nodes.
* **Uses** and, via a reaching-definitions fixpoint over the CFG, the set of
  definitions that may reach each use (def-use chains).
* **Variable flow chains** (``amount -> validated -> tax -> total -> return``)
  pairing each defining statement's local inputs with the variable it defines.
* **Dead stores** (definitions that reach no use on any path) and
  **possibly-undefined uses** (uses no live definition reaches and that are
  neither module-level names nor builtins).

Documented approximations
-------------------------
* Reaching definitions is a *may* analysis: a use reached by a definition on
  some path counts as defined; use-before-def is therefore under-reported
  (conservative).  Branch merges, loop back edges, and exception paths all
  contribute reaching definitions.
* Nested ``def``/``class``/lambda contribute *closure uses*: names loaded
  inside them minus names they bind themselves.  Shadowed locals can create
  false uses (conservative direction: fewer dead stores / fewer undefined
  flags).  Function-local imports are modeled as defs.
* Comprehension targets bind in the enclosing scope (consistent with the
  Phase-1/2 model) and are visited before the element expression.
* ``del x`` is modeled as a "delete" definition: a use reached only by it is
  flagged possibly-undefined.
"""
from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass
from typing import Iterator, NamedTuple

from .control_flow import FunctionCFG
from .findings import Finding, Severity
from .models import FunctionInfo, ModuleInfo


# --- public model -----------------------------------------------------------


@dataclass(frozen=True)
class Definition:
    id: str
    variable: str
    node: str          # CFG node id
    line: int
    kind: str          # param | assignment | aug-assign | loop-target |
                       # except-binding | import | delete | function | class


@dataclass(frozen=True)
class Use:
    variable: str
    node: str
    line: int
    kind: str          # read | return
    reaching: tuple[str, ...]   # definition ids
    status: str        # ok | external | possibly-undefined


@dataclass(frozen=True)
class FlowEdge:
    """Producer-to-consumer data edge; consumer "return" means a return."""

    producer: str
    consumer: str
    line: int


@dataclass(frozen=True)
class ReturnSummary:
    line: int
    value: str | None
    used_variables: tuple[str, ...]


@dataclass
class DataFlowReport:
    qualified_name: str
    parameters: tuple[Definition, ...]
    definitions: tuple[Definition, ...]
    uses: tuple[Use, ...]
    flow_edges: tuple[FlowEdge, ...]
    returns: tuple[ReturnSummary, ...]
    external_inputs: tuple[str, ...]
    dead_stores: tuple[Definition, ...]
    possibly_undefined_uses: tuple[Use, ...]

    def definition(self, def_id: str) -> Definition:
        for d in self.definitions:
            if d.id == def_id:
                return d
        raise KeyError(def_id)

    def to_dict(self) -> dict:
        return {
            "qualified_name": self.qualified_name,
            "parameters": [vars(p) for p in self.parameters],
            "definitions": [vars(d) for d in self.definitions],
            "uses": [vars(u) for u in self.uses],
            "flow_edges": [
                {"producer": e.producer, "consumer": e.consumer, "line": e.line}
                for e in self.flow_edges
            ],
            "returns": [vars(r) for r in self.returns],
            "external_inputs": list(self.external_inputs),
            "dead_stores": [vars(d) for d in self.dead_stores],
            "possibly_undefined_uses": [vars(u) for u in self.possibly_undefined_uses],
        }


# --- flow-sensitive findings (separate from the Phase-2 lexical engine) ------

FLOW_CATEGORY_UNDEFINED_USE = "POSSIBLY_UNDEFINED_USE"
FLOW_CATEGORY_DEAD_STORE = "DEAD_STORE"


def flow_findings(reports: list[DataFlowReport], filename: str) -> list[Finding]:
    """Convert flow-sensitive results into Finding records.

    Deliberately separate from :class:`FindingsEngine` (lexical rules): these
    categories require the CFG and reaching-definitions analysis.
    """
    findings: list[Finding] = []
    for report in reports:
        for use in report.possibly_undefined_uses:
            findings.append(
                Finding(
                    file=filename,
                    line=use.line,
                    category=FLOW_CATEGORY_UNDEFINED_USE,
                    severity=Severity.HIGH,
                    message=(
                        f"Variable '{use.variable}' may be used before any "
                        f"definition reaches this point in function "
                        f"'{report.qualified_name}'."
                    ),
                    suggestion=(
                        "Initialize the variable on every path, or "
                        "correct the name if it is a typo."
                    ),
                )
            )
        for d in report.dead_stores:
            if d.kind == "param":
                message = (
                    f"Parameter '{d.variable}' of function "
                    f"'{report.qualified_name}' is never used."
                )
                suggestion = (
                    "Remove the parameter, or prefix it with '_' to "
                    "document the intentional non-use."
                )
            else:
                message = (
                    f"Value assigned to '{d.variable}' at line {d.line} in "
                    f"function '{report.qualified_name}' is never read."
                )
                suggestion = (
                    "Remove the assignment if the value is not "
                    "needed, or use it."
                )
            findings.append(
                Finding(
                    file=filename,
                    line=d.line,
                    category=FLOW_CATEGORY_DEAD_STORE,
                    severity=Severity.LOW,
                    message=message,
                    suggestion=suggestion,
                )
            )
    findings.sort(key=lambda f: (f.line, f.category, f.message))
    return findings


# --- event extraction -----------------------------------------------------------


class _Event(NamedTuple):
    kind: str                    # "use" | "def"
    name: str
    line: int
    use_kind: str = "read"
    definition: "Definition | None" = None


def _iter_args(args: ast.arguments) -> Iterator[ast.arg]:
    for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
        yield arg
    if args.vararg is not None:
        yield args.vararg
    if args.kwarg is not None:
        yield args.kwarg


def _collect_names(node: ast.AST) -> tuple[set[str], set[str]]:
    """All names loaded / stored anywhere inside ``node``."""
    loads: set[str] = set()
    stores: set[str] = set()

    class _Visitor(ast.NodeVisitor):
        def visit_Name(self, n: ast.Name) -> None:
            if isinstance(n.ctx, ast.Load):
                loads.add(n.id)
            elif isinstance(n.ctx, ast.Store):
                stores.add(n.id)

    _Visitor().visit(node)
    return loads, stores


class _ExprWalker(ast.NodeVisitor):
    """Ordered use/def events for one expression (evaluation-order aware)."""

    def __init__(self, sink: "_FunctionFlowBuilder") -> None:
        self.sink = sink

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.sink.use(node.id, node.lineno)
        elif isinstance(node.ctx, ast.Store):  # defensive; handled explicitly
            self.sink.define(node.id, node.lineno, "assignment")

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)  # value evaluated before the target binds
        self.sink.define(node.target.id, node.lineno, "assignment")

    def visit_Lambda(self, node: ast.Lambda) -> None:
        params = {a.arg for a in _iter_args(node.args)}
        loads, stores = _collect_names(node.body)
        for name in sorted(loads - stores - params):
            self.sink.use(name, node.lineno)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._comprehension(node, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._comprehension(node, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._comprehension(node, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._comprehension(node, [node.key, node.value])

    def _comprehension(self, node, parts) -> None:
        """iter -> bind target -> filters -> element (Python's order)."""
        for gen in node.generators:
            self.visit(gen.iter)
            self._bind(gen.target)
            for if_clause in gen.ifs:
                self.visit(if_clause)
        for part in parts:
            self.visit(part)

    def _bind(self, target: ast.expr) -> None:
        if isinstance(target, ast.Name):
            self.sink.define(target.id, target.lineno, "loop-target")
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._bind(elt)
        elif isinstance(target, ast.Starred):
            self._bind(target.value)


_MATCH = getattr(ast, "Match", None)
_MATCH_AS = getattr(ast, "MatchAs", None)


class _FunctionFlowBuilder:
    """Extracts ordered events, statement summaries, and returns per node."""

    def __init__(self, cfg: FunctionCFG) -> None:
        self.cfg = cfg
        self.events: dict[str, list[_Event]] = {n.id: [] for n in cfg.nodes}
        self._summaries: list[tuple[int, list[str], list[str], bool]] = []
        self._return_records: list[tuple[int, "str | None", list[str]]] = []
        self.all_defs: list[Definition] = []
        self._def_counter = 0
        self._node_id = ""
        self._uses: list[str] = []
        self._defs: list[str] = []
        self._in_return = False

    # -- sink API used by walkers -----------------------------------------

    def use(self, name: str, line: int) -> None:
        self.events[self._node_id].append(
            _Event("use", name, line,
                   "return" if self._in_return else "read")
        )
        self._uses.append(name)

    def define(self, name: str, line: int, kind: str) -> None:
        self._def_counter += 1
        definition = Definition(
            id=f"d{self._def_counter}", variable=name,
            node=self._node_id, line=line, kind=kind,
        )
        self.all_defs.append(definition)
        self.events[self._node_id].append(
            _Event("def", name, line, definition=definition)
        )
        self._defs.append(name)

    def _expr(self, expr: "ast.expr | None") -> None:
        if expr is not None:
            _ExprWalker(self).visit(expr)

    # -- node extraction ------------------------------------------------------

    def run(self):
        for node in self.cfg.nodes:
            self._node_id = node.id
            self._extract(node)
        return self.events, self._summaries, self._return_records, self.all_defs

    def _extract(self, node) -> None:
        obj = self.cfg.ast_nodes.get(node.id)
        if node.kind == "entry":
            func = obj
            for arg in _iter_args(func.args):
                self.define(arg.arg, func.lineno, "param")
            return
        if node.kind == "exit":
            return
        if node.kind == "condition":
            self._begin()
            self._expr(obj)
            self._end(node.lineno)
            return
        if node.kind == "loop":
            self._begin()
            if isinstance(obj, (ast.For, ast.AsyncFor)):
                self._expr(obj.iter)
                self._bind_target(obj.target)
            else:  # While (walrus in the test binds here)
                self._expr(obj.test)
            self._end(obj.lineno)
            return
        if node.kind == "handler":
            if obj.type is not None:
                self._expr(obj.type)
            if obj.name is not None:
                self.define(obj.name, obj.lineno, "except-binding")
            return
        if node.kind == "match" and obj is not None:
            self._begin()
            self._expr(obj.subject)
            for case in obj.cases:
                self._walk_pattern(case.pattern)
                if case.guard is not None:
                    self._expr(case.guard)
            self._end(obj.lineno)
            return
        # basic block: obj is the statement list
        for stmt in obj:
            self._statement(stmt)

    def _walk_pattern(self, pattern) -> None:
        if _MATCH_AS is not None and isinstance(pattern, _MATCH_AS):
            if pattern.pattern is not None:
                self._walk_pattern(pattern.pattern)
            if pattern.name is not None:
                self.define(pattern.name, pattern.lineno, "assignment")
            return
        for child in ast.iter_child_nodes(pattern):
            if isinstance(child, ast.expr):
                self._expr(child)
            else:
                self._walk_pattern(child)

    # -- statement dispatch ------------------------------------------------------

    def _begin(self) -> None:
        self._uses = []
        self._defs = []
        self._in_return = False

    def _end(self, line: int) -> None:
        if self._defs or self._in_return:
            self._summaries.append((
                line,
                list(dict.fromkeys(self._uses)),
                list(dict.fromkeys(self._defs)),
                self._in_return,
            ))
        self._begin()

    def _statement(self, stmt: ast.stmt) -> None:
        self._begin()
        self._dispatch(stmt)
        self._end(stmt.lineno)

    def _dispatch(self, stmt: ast.stmt) -> None:
        if isinstance(stmt, ast.Assign):
            self._expr(stmt.value)
            for target in stmt.targets:
                self._assign_target(target)
        elif isinstance(stmt, ast.AnnAssign):
            self._expr(stmt.annotation)
            if stmt.value is not None:
                self._expr(stmt.value)
            if stmt.value is not None and isinstance(stmt.target, ast.Name):
                self.define(stmt.target.id, stmt.lineno, "assignment")
            elif not isinstance(stmt.target, ast.Name):
                self._expr(stmt.target)
        elif isinstance(stmt, ast.AugAssign):
            if isinstance(stmt.target, ast.Name):
                self.use(stmt.target.id, stmt.lineno)  # read-modify-write
                self.define(stmt.target.id, stmt.lineno, "aug-assign")
            else:
                self._expr(stmt.target)
            self._expr(stmt.value)
        elif isinstance(stmt, ast.Return):
            self._in_return = True
            value = ast.unparse(stmt.value) if stmt.value is not None else None
            self._expr(stmt.value)
            self._return_records.append((stmt.lineno, value, list(self._uses)))
        elif isinstance(stmt, ast.Raise):
            self._expr(stmt.exc)
            self._expr(stmt.cause)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:  # the body is flattened into the block
                self._expr(item.context_expr)
                if item.optional_vars is not None:
                    self._assign_target(item.optional_vars)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            loads, stores = _collect_names(stmt)  # closure capture
            params = {a.arg for a in _iter_args(stmt.args)}
            for name in sorted(loads - stores - params):
                self.use(name, stmt.lineno)
            self.define(stmt.name, stmt.lineno, "function")
        elif isinstance(stmt, ast.ClassDef):
            loads, stores = _collect_names(stmt)
            for name in sorted(loads - stores):
                self.use(name, stmt.lineno)
            self.define(stmt.name, stmt.lineno, "class")
        elif isinstance(stmt, ast.Delete):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    self.define(target.id, target.lineno, "delete")
                else:
                    self._expr(target)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for alias in stmt.names:
                if alias.name == "*":
                    continue
                bound = (
                    alias.asname or alias.name
                    if isinstance(stmt, ast.ImportFrom)
                    else alias.asname or alias.name.split(".")[0]
                )
                self.define(bound, stmt.lineno, "import")
        elif isinstance(stmt, ast.Assert):
            self._expr(stmt.test)
            self._expr(stmt.msg)
        elif isinstance(stmt, ast.Expr):
            self._expr(stmt.value)
        # Pass / Global / Nonlocal / Break / Continue: no data-flow events.

    def _assign_target(self, target: ast.expr) -> None:
        if isinstance(target, ast.Name):
            self.define(target.id, target.lineno, "assignment")
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._assign_target(elt)
        elif isinstance(target, ast.Starred):
            self._assign_target(target.value)
        else:  # Attribute / Subscript stores: the base is a use, not a def
            self._expr(target)

    def _bind_target(self, target: ast.expr) -> None:
        if isinstance(target, ast.Name):
            self.define(target.id, target.lineno, "loop-target")
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._bind_target(elt)
        elif isinstance(target, ast.Starred):
            self._bind_target(target.value)
        else:
            self._expr(target)


# --- reaching definitions -----------------------------------------------------


def _reaching(cfg: FunctionCFG, events: dict[str, list[_Event]],
              all_defs: list[Definition]) -> dict[str, set[str]]:
    """Classic gen/kill fixpoint; returns IN sets per node id."""
    defs_of_var: dict[str, list[str]] = {}
    for d in all_defs:
        defs_of_var.setdefault(d.variable, []).append(d.id)

    gen: dict[str, dict[str, str]] = {}
    kill: dict[str, set[str]] = {}
    for node in cfg.nodes:
        last: dict[str, str] = {}
        for event in events[node.id]:
            if event.kind == "def":
                last[event.name] = event.definition.id
        gen[node.id] = last
        killed: set[str] = set()
        for var, keep in last.items():
            for def_id in defs_of_var.get(var, ()):
                if def_id != keep:
                    killed.add(def_id)
        kill[node.id] = killed

    preds: dict[str, list[str]] = {n.id: [] for n in cfg.nodes}
    for edge in cfg.edges:
        preds[edge.target].append(edge.source)

    in_sets = {n.id: set() for n in cfg.nodes}
    out_sets = {n.id: set() for n in cfg.nodes}
    changed = True
    while changed:
        changed = False
        for node in cfg.nodes:
            in_new: set[str] = set()
            for p in preds[node.id]:
                in_new |= out_sets[p]
            out_new = set(gen[node.id].values()) | (in_new - kill[node.id])
            if in_new != in_sets[node.id] or out_new != out_sets[node.id]:
                in_sets[node.id] = in_new
                out_sets[node.id] = out_new
                changed = True
    return in_sets


def _resolve_uses(cfg, events, in_sets, all_defs, external_names):
    defs_by_id = {d.id: d for d in all_defs}
    uses: list[Use] = []
    for node in cfg.nodes:
        current: dict[str, set[str]] = {}
        for def_id in in_sets[node.id]:
            d = defs_by_id[def_id]
            current.setdefault(d.variable, set()).add(def_id)
        for event in events[node.id]:
            if event.kind == "use":
                reaching = tuple(sorted(current.get(event.name, ())))
                if reaching:
                    if any(defs_by_id[r].kind != "delete" for r in reaching):
                        status = "ok"
                    else:  # only delete-definitions reach this use
                        status = "possibly-undefined"
                else:
                    status = (
                        "external"
                        if event.name in external_names
                        else "possibly-undefined"
                    )
                uses.append(
                    Use(
                        variable=event.name, node=node.id, line=event.line,
                        kind=event.use_kind, reaching=reaching, status=status,
                    )
                )
            else:
                current[event.name] = {event.definition.id}
    return tuple(uses)


# --- top-level API -----------------------------------------------------------------


def _module_bound_names(module: ModuleInfo) -> set[str]:
    names = set(module.module_variables)
    for imp in module.imports:
        names.update(imp.bound_names)
    for fn in module.functions:
        names.add(fn.name)
    for cls in module.classes:
        names.add(cls.name)
    names.update(dir(builtins))
    return names


def build_data_flows(cfgs: list[FunctionCFG],
                     module: ModuleInfo) -> list[DataFlowReport]:
    """One data-flow report per CFG, matched by qualified name."""
    external_names = _module_bound_names(module)
    fn_infos = {fn.qualified_name: fn for fn in module.functions}
    reports = []
    for cfg in cfgs:
        fn_info = fn_infos.get(cfg.qualified_name)
        reports.append(_analyze_one(cfg, fn_info, external_names))
    return reports


def _analyze_one(cfg: FunctionCFG, fn_info: "FunctionInfo | None",
                 external_names: set[str]) -> DataFlowReport:
    events, summaries, return_records, all_defs = _FunctionFlowBuilder(cfg).run()

    parameters = tuple(d for d in all_defs if d.kind == "param")
    bound = {p.variable for p in parameters}
    bound.update(d.variable for d in all_defs)
    if fn_info is not None:
        bound.update(fn_info.variables)
        bound.update(p.name for p in fn_info.params)

    flow_edges: list[FlowEdge] = []
    for line, uses, defs, is_return in summaries:
        producers = sorted(set(uses) & bound)
        if is_return:
            for producer in producers:
                flow_edges.append(FlowEdge(producer, "return", line))
        for consumer in defs:
            for producer in producers:
                flow_edges.append(FlowEdge(producer, consumer, line))

    returns = tuple(
        ReturnSummary(line, value, tuple(sorted(set(uses) & bound)))
        for line, value, uses in return_records
    )

    in_sets = _reaching(cfg, events, all_defs)
    uses = _resolve_uses(cfg, events, in_sets, all_defs, external_names)

    reached: set[str] = set()
    for use in uses:
        reached.update(use.reaching)
    dead_stores = tuple(
        d for d in all_defs
        if d.id not in reached and d.kind != "delete"
    )
    possibly_undefined = tuple(
        u for u in uses if u.status == "possibly-undefined"
    )
    external_inputs = tuple(
        sorted({u.variable for u in uses if u.status == "external"})
    )

    return DataFlowReport(
        qualified_name=cfg.qualified_name,
        parameters=parameters,
        definitions=tuple(all_defs),
        uses=uses,
        flow_edges=tuple(flow_edges),
        returns=returns,
        external_inputs=external_inputs,
        dead_stores=dead_stores,
        possibly_undefined_uses=possibly_undefined,
    )


# --- rendering ---------------------------------------------------------------------


def render_data_flow(report: DataFlowReport) -> str:
    lines = [f"Data flow: {report.qualified_name}"]
    params = ", ".join(p.variable for p in report.parameters) or "(none)"
    lines.append(f"  Parameters: {params}")
    lines.append("  Chains:")
    if report.flow_edges:
        for edge in report.flow_edges:
            lines.append(f"    {edge.producer} -> {edge.consumer}   "
                         f"[line {edge.line}]")
    else:
        lines.append("    (none)")
    externals = ", ".join(report.external_inputs) or "(none)"
    lines.append(f"  External inputs: {externals}")
    dead = ", ".join(
        f"{d.variable}@L{d.line}" for d in report.dead_stores
    ) or "(none)"
    lines.append(f"  Dead stores: {dead}")
    undefined = ", ".join(
        f"{u.variable}@L{u.line}" for u in report.possibly_undefined_uses
    ) or "(none)"
    lines.append(f"  Possibly undefined uses: {undefined}")
    return "\n".join(lines)