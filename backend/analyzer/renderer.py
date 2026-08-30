"""Human-readable unicode-tree rendering of a :class:`ModuleInfo`."""
from __future__ import annotations

from .models import ClassInfo, FunctionInfo, ModuleInfo, ParameterInfo

_MAX_INLINE = 8  # cap for long inline lists in the rendered tree


def render_structure(module: ModuleInfo) -> str:
    """Render the structural summary ("AST view") of a module as a tree."""
    lines: list[str] = [f"Module: {module.filename}"]

    def emit(prefix: str, text: str, last: bool = False) -> None:
        connector = "└── " if last else "├── "
        lines.append(f"{prefix}{connector}{text}")

    def separator() -> None:
        lines.append("│")

    sections = ["imports", "variables", "functions", "classes"]
    if module.dependencies:
        sections.append("dependencies")

    for index, section in enumerate(sections):
        is_last_section = index == len(sections) - 1
        child_prefix = "    " if is_last_section else "│   "

        if section == "imports":
            emit("", f"Imports ({len(module.imports)})", last=is_last_section)
            for i, imp in enumerate(module.imports):
                emit(
                    child_prefix,
                    f"{imp.statement}  [line {imp.lineno}]",
                    last=(i == len(module.imports) - 1),
                )

        elif section == "variables":
            names = ", ".join(module.module_variables) or "(none)"
            emit(
                "",
                f"Module variables ({len(module.module_variables)}): {names}",
                last=is_last_section,
            )

        elif section == "functions":
            emit("", f"Functions ({len(module.functions)})", last=is_last_section)
            for i, fn in enumerate(module.functions):
                fn_last = i == len(module.functions) - 1
                fn_prefix = child_prefix + ("    " if fn_last else "│   ")
                emit(child_prefix, _function_header(fn), last=fn_last)
                _emit_function_details(emit, fn_prefix, fn)

        elif section == "classes":
            emit("", f"Classes ({len(module.classes)})", last=is_last_section)
            for i, cls in enumerate(module.classes):
                cls_last = i == len(module.classes) - 1
                cls_prefix = child_prefix + ("    " if cls_last else "│   ")
                emit(child_prefix, _class_header(cls), last=cls_last)
                emit(cls_prefix, f"Bases: {', '.join(cls.bases) or '(none)'}")
                emit(cls_prefix, f"Decorators: {', '.join(cls.decorators) or '(none)'}")
                emit(
                    cls_prefix,
                    f"Methods ({len(cls.methods)}): {', '.join(cls.methods) or '(none)'}",
                )
                emit(
                    cls_prefix,
                    f"Class variables: {', '.join(cls.class_variables) or '(none)'}",
                    last=True,
                )

        elif section == "dependencies":
            emit("", f"Internal dependencies ({len(module.dependencies)})", last=True)
            items = sorted(module.dependencies.items())
            for i, (caller, callees) in enumerate(items):
                emit(
                    "    ",
                    f"{caller} → {', '.join(callees)}",
                    last=(i == len(items) - 1),
                )

        if not is_last_section:
            separator()

    return "\n".join(lines)


def _function_header(fn: FunctionInfo) -> str:
    tags = []
    if fn.is_method:
        tags.append("method")
    if fn.is_nested:
        tags.append("nested")
    if fn.is_async:
        tags.append("async")
    suffix = f" [{', '.join(tags)}]" if tags else ""
    return (
        f"Function: {fn.qualified_name}{suffix} "
        f"(lines {fn.lineno}-{fn.end_lineno}, {fn.length} lines)"
    )


def _class_header(cls: ClassInfo) -> str:
    return f"Class: {cls.qualified_name} (lines {cls.lineno}-{cls.end_lineno})"


def _emit_function_details(emit, prefix: str, fn: FunctionInfo) -> None:
    def detail(text: str, last: bool = False) -> None:
        emit(prefix, text, last=last)

    params = ", ".join(_format_param(p) for p in fn.params) or "(none)"
    detail(f"Parameters: {params}")
    detail(f"Decorators: {', '.join(fn.decorators) or '(none)'}")
    detail(f"Variables: {_inline(fn.variables)}")
    detail(f"Conditions: {fn.num_conditions}")
    loops = ", ".join(f"{loop.kind} (line {loop.lineno})" for loop in fn.loops)
    detail(f"Loops: {loops or '(none)'}")
    handlers = (
        ", ".join(
            f"{', '.join(h.exception_types) if h.exception_types else 'bare'}"
            f" (line {h.lineno})"
            for h in fn.exception_handlers
        )
        or "(none)"
    )
    detail(f"Exceptions handled: {handlers}")
    detail(f"Exceptions raised: {', '.join(fn.raises) or '(none)'}")
    calls = ", ".join(f"{c.name} (line {c.lineno})" for c in fn.calls[:_MAX_INLINE])
    if len(fn.calls) > _MAX_INLINE:
        calls += f", … (+{len(fn.calls) - _MAX_INLINE} more)"
    detail(f"Calls: {calls or '(none)'}")
    returns = ", ".join(str(r.lineno) for r in fn.returns)
    returns_text = (
        f" ({'line' if len(fn.returns) == 1 else 'lines'} {returns})"
        if fn.returns
        else ""
    )
    detail(f"Returns: {len(fn.returns)}{returns_text}", last=True)


def _format_param(param: ParameterInfo) -> str:
    prefix = {"vararg": "*", "kwarg": "**"}.get(param.kind, "")
    text = f"{prefix}{param.name}"
    if param.annotation:
        text += f": {param.annotation}"
    if param.default is not None:
        text += f" = {param.default}"
    return text


def _inline(items) -> str:
    if not items:
        return "(none)"
    if len(items) > _MAX_INLINE:
        return ", ".join(items[:_MAX_INLINE]) + f", … (+{len(items) - _MAX_INLINE} more)"
    return ", ".join(items)