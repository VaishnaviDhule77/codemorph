"""AST analysis: turns Python source into a structural :class:`ModuleInfo`.

Phase 1 scope (full supported-subset statement in the README): functions,
methods, nested functions, classes, imports, parameters, calls, returns,
conditionals, loops, exception handling, bound variables, and a name-based
internal call graph.
"""
from __future__ import annotations

import ast
from collections import defaultdict

from ._ast_utils import iter_class_defs, iter_function_defs, max_nesting_depth
from .models import (
    CallInfo,
    ClassInfo,
    ExceptInfo,
    FunctionInfo,
    ImportInfo,
    LoopInfo,
    ModuleInfo,
    ParameterInfo,
    ReturnInfo,
)


class SourceParseError(Exception):
    """Raised when source code cannot be parsed; carries location info."""

    def __init__(self, filename: str, message: str, lineno: int, offset: int) -> None:
        super().__init__(f"{filename}:{lineno}:{offset}: {message}")
        self.filename = filename
        self.message = message
        self.lineno = lineno
        self.offset = offset


def parse_source(source: str, filename: str = "<string>") -> ast.Module:
    """Parse ``source`` into an AST, raising :class:`SourceParseError` on failure."""
    try:
        return ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise SourceParseError(
            filename=filename,
            message=exc.msg or "invalid syntax",
            lineno=exc.lineno or 0,
            offset=exc.offset or 0,
        ) from exc


class ASTAnalyzer:
    """Builds a :class:`ModuleInfo` structural model from a parsed module."""

    def analyze(self, tree: ast.Module, filename: str = "<string>") -> ModuleInfo:
        functions = self._collect_functions(tree)
        return ModuleInfo(
            filename=filename,
            docstring=ast.get_docstring(tree),
            imports=self._collect_imports(tree),
            module_variables=self._collect_module_variables(tree),
            functions=functions,
            classes=self._collect_classes(tree),
            dependencies=self._build_dependencies(functions),
            max_nesting_depth=max_nesting_depth(tree),
        )

    # -- functions -----------------------------------------------------------

    @staticmethod
    def _collect_functions(tree: ast.Module) -> list[FunctionInfo]:
        functions: list[FunctionInfo] = []
        for scoped in iter_function_defs(tree):
            node = scoped.node
            details = _FunctionDetails()
            for stmt in node.body:  # nested defs are skipped by the visitor
                details.visit(stmt)
            functions.append(
                FunctionInfo(
                    name=node.name,
                    qualified_name=scoped.qualified_name,
                    lineno=node.lineno,
                    end_lineno=node.end_lineno,
                    is_async=isinstance(node, ast.AsyncFunctionDef),
                    is_method=scoped.is_method,
                    is_nested=scoped.is_nested,
                    decorators=tuple(ast.unparse(d) for d in node.decorator_list),
                    params=_parameters(node),
                    docstring=ast.get_docstring(node),
                    returns=tuple(details.returns),
                    calls=tuple(details.calls),
                    variables=tuple(details.variables),
                    num_conditions=details.num_conditions,
                    loops=tuple(details.loops),
                    exception_handlers=tuple(details.handlers),
                    raises=tuple(details.raises),
                    max_nesting=max_nesting_depth(node),
                )
            )
        functions.sort(key=lambda fn: (fn.lineno, fn.qualified_name))
        return functions

    # -- imports --------------------------------------------------------------

    @staticmethod
    def _collect_imports(tree: ast.Module) -> list[ImportInfo]:
        """Collect every import statement (module-level and function-local)."""
        imports: list[ImportInfo] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.append(
                    ImportInfo(
                        is_from=False,
                        module=node.names[0].name if len(node.names) == 1 else None,
                        level=0,
                        names=tuple((a.name, a.asname) for a in node.names),
                        lineno=node.lineno,
                    )
                )
            elif isinstance(node, ast.ImportFrom):
                imports.append(
                    ImportInfo(
                        is_from=True,
                        module=node.module,
                        level=node.level or 0,
                        names=tuple((a.name, a.asname) for a in node.names),
                        lineno=node.lineno,
                    )
                )
        imports.sort(key=lambda imp: imp.lineno)
        return imports

    # -- module-level variables -------------------------------------------------

    @staticmethod
    def _collect_module_variables(tree: ast.Module) -> list[str]:
        collector = _FunctionDetails()
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # their internals are analyzed separately
            collector.visit(stmt)
        return collector.variables

    # -- classes ------------------------------------------------------------------

    @staticmethod
    def _collect_classes(tree: ast.Module) -> list[ClassInfo]:
        classes: list[ClassInfo] = []
        for node, qualified in iter_class_defs(tree):
            methods = tuple(
                stmt.name
                for stmt in node.body
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            class_variables: list[str] = []
            for stmt in node.body:
                targets: list[ast.expr] = []
                if isinstance(stmt, ast.Assign):
                    targets = list(stmt.targets)
                elif isinstance(stmt, ast.AnnAssign):
                    targets = [stmt.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        class_variables.append(target.id)
            classes.append(
                ClassInfo(
                    name=node.name,
                    qualified_name=qualified,
                    lineno=node.lineno,
                    end_lineno=node.end_lineno,
                    bases=tuple(ast.unparse(base) for base in node.bases),
                    decorators=tuple(ast.unparse(d) for d in node.decorator_list),
                    docstring=ast.get_docstring(node),
                    methods=methods,
                    class_variables=tuple(class_variables),
                )
            )
        classes.sort(key=lambda cls: (cls.lineno, cls.qualified_name))
        return classes

    # -- call graph ------------------------------------------------------------------

    @staticmethod
    def _build_dependencies(functions: list[FunctionInfo]) -> dict[str, list[str]]:
        """Name-based internal call graph.

        A call is resolved to a module-internal function when its simple name
        (last dotted segment) matches a known function/method name. This is an
        intentional *over-approximation*: attribute calls such as ``obj.add()``
        cannot be resolved without type information (documented limitation;
        refined when data-flow analysis lands in Phase 3).
        """
        by_simple_name: dict[str, list[FunctionInfo]] = defaultdict(list)
        for fn in functions:
            by_simple_name[fn.name].append(fn)

        dependencies: dict[str, list[str]] = {}
        for fn in functions:
            callees: set[str] = set()
            for call in fn.calls:
                base = call.name.rsplit(".", 1)[-1]
                for candidate in by_simple_name.get(base, ()):
                    callees.add(candidate.qualified_name)
            if callees:
                dependencies[fn.qualified_name] = sorted(callees)
        return dependencies


# --- parameter extraction --------------------------------------------------------


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[ParameterInfo, ...]:
    args = node.args
    params: list[ParameterInfo] = []

    positional = list(args.posonlyargs) + list(args.args)
    n_positional = len(positional)
    first_default = n_positional - len(args.defaults)
    for index, arg in enumerate(positional):
        default = (
            ast.unparse(args.defaults[index - first_default])
            if index >= first_default
            else None
        )
        params.append(
            ParameterInfo(
                name=arg.arg,
                annotation=ast.unparse(arg.annotation) if arg.annotation else None,
                default=default,
                kind="positional",
            )
        )

    if args.vararg is not None:
        params.append(
            ParameterInfo(
                name=args.vararg.arg,
                annotation=ast.unparse(args.vararg.annotation)
                if args.vararg.annotation
                else None,
                kind="vararg",
            )
        )
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        params.append(
            ParameterInfo(
                name=arg.arg,
                annotation=ast.unparse(arg.annotation) if arg.annotation else None,
                default=ast.unparse(default) if default is not None else None,
                kind="kwonly",
            )
        )
    if args.kwarg is not None:
        params.append(
            ParameterInfo(
                name=args.kwarg.arg,
                annotation=ast.unparse(args.kwarg.annotation)
                if args.kwarg.annotation
                else None,
                kind="kwarg",
            )
        )
    return tuple(params)


# --- per-scope detail collector ----------------------------------------------------


class _FunctionDetails(ast.NodeVisitor):
    """Collects structural facts from one scope's body.

    Nested ``def`` statements are *not* descended into — they are analyzed as
    their own functions. Class bodies are skipped as well.
    """

    def __init__(self) -> None:
        self.calls: list[CallInfo] = []
        self.returns: list[ReturnInfo] = []
        self.loops: list[LoopInfo] = []
        self.handlers: list[ExceptInfo] = []
        self.raises: list[str] = []
        self.variables: list[str] = []
        self.num_conditions: int = 0

    # -- scope boundaries -----------------------------------------------------
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return None

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return None

    # -- conditions & loops -----------------------------------------------------
    def visit_If(self, node: ast.If) -> None:
        self.num_conditions += 1
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.num_conditions += 1
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self.loops.append(LoopInfo(kind="for", lineno=node.lineno))
        self._bind_target(node.target)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.loops.append(LoopInfo(kind="async-for", lineno=node.lineno))
        self._bind_target(node.target)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self.loops.append(LoopInfo(kind="while", lineno=node.lineno))
        self.generic_visit(node)

    # -- assignments ---------------------------------------------------------------
    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._bind_target(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._bind_target(node.target)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._bind_target(node.target)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                self._bind_target(item.optional_vars)
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    # -- calls, returns, exceptions ----------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(
            CallInfo(name=_dotted_name(node.func) or "<expr>", lineno=node.lineno)
        )
        self.generic_visit(node)  # nested calls such as f(g(x)) are recorded too

    def visit_Return(self, node: ast.Return) -> None:
        self.returns.append(
            ReturnInfo(
                lineno=node.lineno,
                value=ast.unparse(node.value) if node.value is not None else None,
            )
        )
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        for handler in node.handlers:
            self.handlers.append(
                ExceptInfo(
                    lineno=handler.lineno,
                    exception_types=_exception_names(handler.type),
                )
            )
        self.generic_visit(node)

    visit_TryStar = visit_Try  # Python 3.11+; never dispatched on older versions

    def visit_Raise(self, node: ast.Raise) -> None:
        # The raised expression is captured in ``raises``; we deliberately do
        # not descend into it so that ``raise ValueError(x)`` is not also
        # double-counted as an ordinary call site.
        if node.exc is None:
            self.raises.append("<re-raise>")
        else:
            self.raises.append(_raised_name(node.exc))
        return None

    # -- helpers ----------------------------------------------------------------------
    def _bind_target(self, target: ast.expr) -> None:
        """Record names bound by an assignment / loop / with target."""
        if isinstance(target, ast.Name):
            if target.id not in self.variables:
                self.variables.append(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._bind_target(element)
        elif isinstance(target, ast.Starred):
            self._bind_target(target.value)
        # Attribute/Subscript targets (self.x = ..., a[i] = ...) mutate an
        # existing object rather than binding a name: not recorded in Phase 1.


def _dotted_name(node: ast.AST) -> str | None:
    """Best-effort dotted name for a call target (``a.b.c``), else ``None``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base is not None else None
    if isinstance(node, ast.Call):
        return _dotted_name(node.func)
    return None


def _raised_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _dotted_name(node.func) or "<expr>"
    return _dotted_name(node) or "<expr>"


def _exception_names(type_node: ast.expr | None) -> tuple[str, ...]:
    if type_node is None:
        return ()  # bare ``except:``
    if isinstance(type_node, ast.Tuple):
        return tuple(_dotted_name(elt) or "<expr>" for elt in type_node.elts)
    return (_dotted_name(type_node) or "<expr>",)