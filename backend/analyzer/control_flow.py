"""Control-flow analysis (Phase 3): per-function control-flow graphs.

Builds a real (small-scale) CFG for every function in a module: basic
blocks, condition nodes, loop headers, exception-handler nodes, and a single
entry/exit, over the supported Python subset.

Model and documented approximations
-----------------------------------
* Basic blocks are maximal runs of non-branching statements.
* ``if``/``elif`` tests and loop headers are dedicated branch nodes with
  ``true``/``false`` edges. ``for`` headers model "advance iterator" (true)
  vs "exhausted" (false).
* ``break`` connects to the point after the whole loop and skips the loop's
  ``else`` clause (correct Python semantics); ``continue`` connects to the
  header; ``else`` runs only on normal loop termination.
* ``try``: every node created inside the try *body* gets a conservative
  ``exception`` edge to each handler entry of that try. Exceptions raised
  inside handlers/``else``/``finally`` propagate out and are not routed to
  sibling handlers. ``finally`` is modeled as executing on the normal and
  handler paths only; exceptional flow *through* finally is not modeled.
* ``raise`` connects to the innermost enclosing handlers (if any), else to
  the function exit with an ``exception`` edge.
* ``with`` is transparent: its body is inlined into the enclosing block.
* ``match`` is a multi-way branch (one ``case`` edge per case + ``false``).
* Falling off the end of a function becomes a ``return`` edge labeled
  ``implicit``; uncaught ``raise`` becomes an ``exception`` edge to exit.
* The CFG is intraprocedural: nested ``def`` statements are ordinary
  statements here and get their own CFGs.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field

from ._ast_utils import iter_function_defs

_MATCH = getattr(ast, "Match", None)  # Python 3.10+
_TRY_NODES: tuple[type, ...] = (ast.Try,) + (
    (ast.TryStar,) if hasattr(ast, "TryStar") else ()
)

EDGE_KINDS = (
    "normal", "true", "false", "case", "loop_back",
    "break", "continue", "exception", "return",
)


@dataclass(frozen=True)
class CFGNode:
    """One CFG node. ``statements`` hold unparsed source text."""

    id: str
    kind: str  # entry | exit | basic | condition | loop | handler | match
    statements: tuple[str, ...] = ()
    condition: str | None = None
    description: str | None = None
    lineno: int | None = None
    end_lineno: int | None = None


@dataclass(frozen=True)
class CFGEdge:
    source: str
    target: str
    kind: str
    label: str | None = None


@dataclass
class FunctionCFG:
    """Control-flow graph of one function (serializable; AST kept aside)."""

    qualified_name: str
    is_async: bool
    lineno: int
    nodes: list[CFGNode]
    edges: list[CFGEdge]
    ast_nodes: dict = field(default_factory=dict, repr=False, compare=False)

    # -- lookups ----------------------------------------------------------

    def node(self, node_id: str) -> CFGNode:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    def successor_edges(self, node_id: str) -> list[CFGEdge]:
        return [e for e in self.edges if e.source == node_id]

    def predecessor_edges(self, node_id: str) -> list[CFGEdge]:
        return [e for e in self.edges if e.target == node_id]

    def nodes_of_kind(self, kind: str) -> list[CFGNode]:
        return [n for n in self.nodes if n.kind == kind]

    def node_with_statement(self, text: str) -> CFGNode:
        """The basic node whose statements include ``text`` (test helper)."""
        for node in self.nodes:
            if any(text in stmt for stmt in node.statements):
                return node
        raise ValueError(f"no node contains {text!r}")

    # -- derived views ------------------------------------------------------

    @property
    def exception_edges(self) -> list[CFGEdge]:
        return [e for e in self.edges if e.kind == "exception"]

    @property
    def return_edges(self) -> list[CFGEdge]:
        return [e for e in self.edges if e.target == "exit" and e.kind == "return"]

    def reachable_ids(self) -> set[str]:
        pending = ["entry"]
        seen = {"entry"}
        while pending:
            for edge in self.successor_edges(pending.pop()):
                if edge.target not in seen:
                    seen.add(edge.target)
                    pending.append(edge.target)
        return seen

    def dead_code_ids(self) -> list[str]:
        """Nodes unreachable from entry (dead code); exit is excluded."""
        reached = self.reachable_ids()
        return [n.id for n in self.nodes if n.id not in reached and n.id != "exit"]

    def to_dict(self) -> dict:
        return {
            "qualified_name": self.qualified_name,
            "is_async": self.is_async,
            "lineno": self.lineno,
            "nodes": [
                {
                    "id": n.id, "kind": n.kind,
                    "statements": list(n.statements),
                    "condition": n.condition, "description": n.description,
                    "lineno": n.lineno, "end_lineno": n.end_lineno,
                }
                for n in self.nodes
            ],
            "edges": [
                {"source": e.source, "target": e.target,
                 "kind": e.kind, "label": e.label}
                for e in self.edges
            ],
        }


# --- builder -----------------------------------------------------------------


class _LoopContext:
    def __init__(self, header: str) -> None:
        self.header = header
        self.breaks: list[tuple[str, str, str | None]] = []


class _CFGBuilder:
    """Builds one function's CFG. Traversal state:

    * ``_current``  -- the basic block still accepting statements.
    * ``_pending``  -- fallthrough edges waiting for the next node:
      (node_id, edge_kind, label).  Pendings with kind "false" come from
      if/loop tests, "break" from loop breaks; "normal" is plain fallthrough.
    * ``_exception_stack`` -- handler-node ids of enclosing ``try`` bodies.
    * ``_loop_stack`` -- innermost-loop context for break/continue.
    """

    def __init__(self, qualified_name: str, func_node) -> None:
        self._qualified_name = qualified_name
        self._func_node = func_node
        self._states: dict[str, dict] = {}
        self._order: list[str] = []
        self._ast: dict = {}
        self._edges: list[tuple[str, str, str, str | None]] = []
        self._edge_keys: set[tuple[str, str, str]] = set()
        self._counter = 0
        self._current: str | None = None
        self._pending: list[tuple[str, str, str | None]] = []
        self._exception_stack: list[list[str]] = []
        self._loop_stack: list[_LoopContext] = []

    # -- node / edge primitives -------------------------------------------

    def _new_node(self, kind: str, *, condition=None, description=None,
                  lineno=None, ast_obj=None) -> str:
        self._counter += 1
        node_id = f"n{self._counter}"
        self._states[node_id] = {
            "kind": kind, "statements": [], "ast": [],
            "condition": condition, "description": description,
            "lineno": lineno, "end_lineno": lineno,
        }
        self._order.append(node_id)
        if ast_obj is not None:
            self._ast[node_id] = ast_obj
        return node_id

    def _add_special(self, node_id: str, kind: str, ast_obj=None) -> None:
        self._states[node_id] = {
            "kind": kind, "statements": [], "ast": [],
            "condition": None, "description": None,
            "lineno": None, "end_lineno": None,
        }
        self._order.append(node_id)
        if ast_obj is not None:
            self._ast[node_id] = ast_obj

    def _edge(self, source: str, target: str, kind: str, label=None) -> None:
        key = (source, target, kind)
        if key in self._edge_keys:
            return  # dedupe: identical (src, dst, kind) keeps first label
        self._edge_keys.add(key)
        self._edges.append((source, target, kind, label))

    def _attach_pending(self, target: str) -> None:
        for source, kind, label in self._pending:
            self._edge(source, target, kind, label)
        self._pending = []

    def _start_block(self) -> str:
        node_id = self._new_node("basic")
        self._attach_pending(node_id)
        self._current = node_id
        return node_id

    def _append(self, stmt: ast.stmt, display: str) -> None:
        state = self._states[self._current]
        state["statements"].append(display)
        state["ast"].append(stmt)
        if state["lineno"] is None or stmt.lineno < state["lineno"]:
            state["lineno"] = stmt.lineno
        if state["end_lineno"] is None or (stmt.end_lineno or 0) > state["end_lineno"]:
            state["end_lineno"] = stmt.end_lineno

    def _flush(self) -> None:
        if self._current is not None:
            self._pending.append((self._current, "normal", None))
            self._current = None

    # -- statement dispatch --------------------------------------------------

    def _visit_stmt(self, stmt: ast.stmt) -> str:
        """Process one statement; return its entry-node id."""
        if isinstance(stmt, ast.If):
            return self._visit_if(stmt)
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            return self._visit_loop(stmt)
        if isinstance(stmt, _TRY_NODES):
            return self._visit_try(stmt)
        if _MATCH is not None and isinstance(stmt, _MATCH):
            return self._visit_match(stmt)
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            return self._visit_with(stmt)
        if isinstance(stmt, ast.Return):
            return self._terminator(stmt, self._return_edge)
        if isinstance(stmt, ast.Break):
            return self._terminator(stmt, self._break_edge)
        if isinstance(stmt, ast.Continue):
            return self._terminator(stmt, self._continue_edge)
        if isinstance(stmt, ast.Raise):
            return self._terminator(stmt, self._raise_edge)
        # simple statement: extends the current block
        if self._current is None:
            self._start_block()
        self._append(stmt, ast.unparse(stmt))
        return self._current

    def _terminator(self, stmt, edge_builder) -> str:
        if self._current is None:
            self._start_block()
        self._append(stmt, ast.unparse(stmt))
        entry = self._current
        self._current = None
        edge_builder(entry)
        return entry

    def _return_edge(self, block: str) -> None:
        self._edge(block, "exit", "return")

    def _break_edge(self, block: str) -> None:
        if self._loop_stack:
            self._loop_stack[-1].breaks.append((block, "break", None))

    def _continue_edge(self, block: str) -> None:
        if self._loop_stack:
            self._edge(block, self._loop_stack[-1].header, "continue")

    def _raise_edge(self, block: str) -> None:
        for handlers in reversed(self._exception_stack):
            if handlers:
                for handler in handlers:
                    self._edge(block, handler, "exception", "raise")
                return
        self._edge(block, "exit", "exception")

    # -- compound statements ----------------------------------------------------

    def _visit_if(self, stmt: ast.If) -> str:
        self._flush()
        cond = self._new_node(
            "condition", condition=ast.unparse(stmt.test),
            lineno=stmt.lineno, ast_obj=stmt.test,
        )
        self._attach_pending(cond)
        then_entry, then_exits = self._walk_seq(stmt.body)
        self._edge(cond, then_entry, "true")
        if stmt.orelse:
            else_entry, else_exits = self._walk_seq(stmt.orelse)
            self._edge(cond, else_entry, "false")
            self._pending = then_exits + else_exits
        else:
            self._pending = then_exits + [(cond, "false", None)]
        return cond

    def _visit_loop(self, stmt) -> str:
        self._flush()
        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            description = (
                f"for {ast.unparse(stmt.target)} in {ast.unparse(stmt.iter)}"
            )
            header = self._new_node(
                "loop", description=description,
                lineno=stmt.lineno, ast_obj=stmt,
            )
        else:  # While
            header = self._new_node(
                "loop", condition=ast.unparse(stmt.test),
                lineno=stmt.lineno, ast_obj=stmt,
            )
        self._attach_pending(header)
        context = _LoopContext(header)
        self._loop_stack.append(context)
        body_entry, body_exits = self._walk_seq(stmt.body)
        self._edge(header, body_entry, "true")
        for source, _kind, _label in body_exits:
            self._edge(source, header, "loop_back")
        self._loop_stack.pop()
        if stmt.orelse:
            # else runs on normal termination only; break skips it
            _, else_exits = self._walk_seq(
                stmt.orelse, entry_pendings=[(header, "false", None)]
            )
            self._pending = else_exits + context.breaks
        else:
            self._pending = [(header, "false", None)] + context.breaks
        return header

    def _visit_try(self, stmt) -> str:
        self._flush()
        handler_nodes = [
            self._new_node(
                "handler",
                description=("except:" if h.type is None
                             else f"except {ast.unparse(h.type)}:"),
                lineno=h.lineno, ast_obj=h,
            )
            for h in stmt.handlers
        ]
        incoming = self._pending
        self._pending = []
        if handler_nodes:
            self._exception_stack.append(handler_nodes)
        region_start = len(self._order)
        body_entry, body_exits = self._walk_seq(
            stmt.body, entry_pendings=incoming
        )
        region_ids = list(self._order[region_start:])
        if handler_nodes:
            self._exception_stack.pop()
            for node_id in region_ids:  # conservative: any node may raise
                for handler in handler_nodes:
                    self._edge(node_id, handler, "exception")
        handler_exits: list[tuple[str, str, str | None]] = []
        for handler_node, handler in zip(handler_nodes, stmt.handlers):
            _, exits = self._walk_seq(
                handler.body, entry_pendings=[(handler_node, "normal", None)]
            )
            handler_exits.extend(exits)
        if stmt.orelse:
            _, orelse_exits = self._walk_seq(
                stmt.orelse, entry_pendings=body_exits
            )
            normal_exits = orelse_exits
        else:
            normal_exits = body_exits
        if stmt.finalbody:
            _, final_exits = self._walk_seq(
                stmt.finalbody, entry_pendings=normal_exits + handler_exits
            )
            self._pending = final_exits
        else:
            self._pending = normal_exits + handler_exits
        return body_entry

    def _visit_match(self, stmt) -> str:
        self._flush()
        node = self._new_node(
            "match", description=f"match {ast.unparse(stmt.subject)}",
            lineno=stmt.lineno, ast_obj=stmt,
        )
        self._attach_pending(node)
        case_exits: list[tuple[str, str, str | None]] = []
        for index, case in enumerate(stmt.cases):
            entry, exits = self._walk_seq(case.body)
            self._edge(node, entry, "case", f"case {index + 1}")
            case_exits.extend(exits)
        self._pending = case_exits + [(node, "false", "no-match")]
        return node

    def _visit_with(self, stmt) -> str:
        """``with`` is transparent: header + body share the enclosing block."""
        if self._current is None:
            self._start_block()
        entry = self._current
        prefix = "async with " if isinstance(stmt, ast.AsyncWith) else "with "
        parts = []
        for item in stmt.items:
            text = ast.unparse(item.context_expr)
            if item.optional_vars is not None:
                text += f" as {ast.unparse(item.optional_vars)}"
            parts.append(text)
        self._append(stmt, prefix + ", ".join(parts) + ":")
        for child in stmt.body:
            self._visit_stmt(child)
        return entry

    # -- sequences ---------------------------------------------------------------

    def _walk_seq(self, stmts, entry_pendings=()):
        """Walk a statement sequence from a fresh entry.

        Returns ``(entry_node_id, exit_pendings)``. The first node created
        attaches ``entry_pendings``; the sequence's fallthrough is returned
        as pendings for the caller to connect.
        """
        self._pending = list(entry_pendings)
        self._current = None
        first: str | None = None
        for stmt in stmts:
            entry = self._visit_stmt(stmt)
            if first is None:
                first = entry
        self._flush()
        exits = self._pending
        self._pending = []
        assert first is not None, "statement sequence must create a node"
        return first, exits

    # -- assembly --------------------------------------------------------------------

    def build(self) -> FunctionCFG:
        self._add_special("entry", "entry", ast_obj=self._func_node)
        _, exits = self._walk_seq(
            self._func_node.body, entry_pendings=[("entry", "normal", None)]
        )
        self._add_special("exit", "exit")
        for source, kind, label in exits:
            if kind == "normal":
                self._edge(source, "exit", "return", "implicit")
            else:
                self._edge(source, "exit", kind, label)
        self._validate()
        nodes = [
            CFGNode(
                id=node_id,
                kind=state["kind"],
                statements=tuple(state["statements"]),
                condition=state["condition"],
                description=state["description"],
                lineno=state["lineno"],
                end_lineno=state["end_lineno"],
            )
            for node_id, state in ((nid, self._states[nid]) for nid in self._order)
        ]
        edges = [CFGEdge(s, t, k, lbl) for s, t, k, lbl in self._edges]
        # Basic blocks carry their statement ASTs in the internal state (via
        # _append), NOT in self._ast (which holds only entry/condition/loop/
        # handler/match objects). The data-flow extractor consumes basic
        # blocks through ast_nodes, so they must be exported here.
        ast_nodes = dict(self._ast)
        for node_id in self._order:
            state = self._states[node_id]
            if state["kind"] == "basic" and state["ast"]:
                ast_nodes[node_id] = list(state["ast"])
        return FunctionCFG(
            qualified_name=self._qualified_name,
            is_async=isinstance(self._func_node, ast.AsyncFunctionDef),
            lineno=self._func_node.lineno,
            nodes=nodes,
            edges=edges,
            ast_nodes=ast_nodes,
        )

    def _validate(self) -> None:
        ids = set(self._states)
        for source, target, _, _ in self._edges:
            assert source in ids and target in ids, f"bad edge {source}->{target}"
        assert not any(target == "entry" for _, target, _, _ in self._edges)
        assert not any(source == "exit" for source, _, _, _ in self._edges)


def build_cfgs(tree: ast.Module) -> list[FunctionCFG]:
    """Build one CFG per function (incl. methods and nested functions)."""
    return [
        _CFGBuilder(scoped.qualified_name, scoped.node).build()
        for scoped in iter_function_defs(tree)
    ]


# --- rendering ------------------------------------------------------------------


def render_cfg(cfg: FunctionCFG) -> str:
    """Human-readable, deterministic text rendering of one CFG."""
    lines = [
        f"CFG: {cfg.qualified_name} "
        f"({len(cfg.nodes)} nodes, {len(cfg.edges)} edges)"
    ]
    for node in cfg.nodes:
        header = f"  {node.id}  [{node.kind}"
        if node.lineno is not None:
            header += f", L{node.lineno}"
        header += "]"
        if node.condition:
            header += f"  {node.condition}"
        elif node.description:
            header += f"  {node.description}"
        lines.append(header)
        for stmt in node.statements[:3]:
            lines.append(f"      {stmt}")
        if len(node.statements) > 3:
            lines.append(f"      ... (+{len(node.statements) - 3} more)")
        for edge in cfg.successor_edges(node.id):
            label = f", {edge.label}" if edge.label else ""
            lines.append(f"    -> {edge.target} [{edge.kind}{label}]")
    dead = cfg.dead_code_ids()
    if dead:
        lines.append(f"  unreachable: {', '.join(dead)}")
    return "\n".join(lines)


def cfgs_to_dot(cfgs: list[FunctionCFG]) -> str:
    """Graphviz DOT export (one cluster per function)."""
    def safe(name: str) -> str:
        return "".join(c if c.isalnum() else "_" for c in name)

    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    lines = ["digraph codemorph {"]
    for cfg in cfgs:
        prefix = f"{safe(cfg.qualified_name)}."
        lines.append(f"  subgraph cluster_{safe(cfg.qualified_name)} {{")
        lines.append(f'    label="{esc(cfg.qualified_name)}";')
        for node in cfg.nodes:
            label = f"{node.id} [{node.kind}]"
            if node.condition:
                label += f" {node.condition}"
            elif node.description:
                label += f" {node.description}"
            lines.append(f'    "{prefix}{node.id}" [label="{esc(label)}"];')
        for edge in cfg.edges:
            lines.append(
                f'    "{prefix}{edge.source}" -> "{prefix}{edge.target}" '
                f'[label="{edge.kind}"];'
            )
        lines.append("  }")
    lines.append("}")
    return "\n".join(lines)