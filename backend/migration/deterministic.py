"""Deterministic transformation engine (Phase 4).

Traceable, syntax-preserving source migrations for the supported Python
subset. Every change is recorded as a :class:`Transformation` (type,
location, original code, new code, reason, risk class), and the migrated
source is validated before it is returned:

1. Edits are applied as exact text replacements at AST-derived (line,
   column) spans, so comments, formatting, and untouched code are preserved
   byte-for-byte.
2. The migrated source must re-parse.
3. A structural guard verifies that the module's functions, signatures,
   classes, and module-level variables are unchanged.

Risk model
----------
SAFE   -- behavior-preserving by construction for any input where the
          original does not already crash.
REVIEW -- behavior-preserving for the common types the pattern implies;
          documented exotic divergences exist, so verification with the
          Phase-5/6 test & equivalence pipeline is recommended.

The engine never fabricates rewrites it cannot justify: rules fire only on
conservative AST matches, overlapping candidates are resolved
deterministically (larger span first), and the whole migration is
idempotent.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import Enum

from ..analyzer.ast_analyzer import parse_source
# Same-project reuse of the Phase-2 scope machinery: one source of truth for
# "which module-level names are read anywhere" and "every statement list" --
# the transformation engine must never disagree with the findings engine.
from ..analyzer.findings import _collect_usage, _iter_statement_lists
from ..analyzer.service import analyze_source


class Risk(str, Enum):
    """Classification of a transformation's behavioral confidence."""

    SAFE = "SAFE"
    REVIEW = "REVIEW"


class TransformKind:
    """Stable transformation-type codes."""

    HAS_KEY_TO_IN = "HAS_KEY_TO_IN"
    PERCENT_TO_FSTRING = "PERCENT_TO_FSTRING"
    FORMAT_TO_FSTRING = "FORMAT_TO_FSTRING"
    BARE_EXCEPT = "BARE_EXCEPT_TYPING"
    AUG_ASSIGN = "AUG_ASSIGN_MODERNIZE"
    DUPLICATE_COLLAPSE = "DUPLICATE_RUN_COLLAPSE"
    UNUSED_IMPORT = "UNUSED_IMPORT_REMOVAL"


@dataclass(frozen=True)
class Transformation:
    """One applied (or proposed) transformation -- the spec's traceability
    record: type, location, original code, new code, reason."""

    file: str
    line: int
    kind: str
    risk: Risk
    original: str
    replacement: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "kind": self.kind,
            "risk": self.risk.value,
            "original": self.original,
            "replacement": self.replacement,
            "reason": self.reason,
        }


@dataclass
class MigrationResult:
    """Outcome of one deterministic migration run."""

    filename: str
    original_source: str
    migrated_source: str
    transformations: list[Transformation] = field(default_factory=list)
    applied: bool = False
    syntax_valid: bool = True
    structural_guard_passed: bool = True
    rejected_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "applied": self.applied,
            "syntax_valid": self.syntax_valid,
            "structural_guard_passed": self.structural_guard_passed,
            "rejected_reason": self.rejected_reason,
            "transformation_count": len(self.transformations),
            "transformations": [t.to_dict() for t in self.transformations],
            "migrated_source": self.migrated_source,
        }


@dataclass(frozen=True)
class MigrationConfig:
    """Engine tunables."""

    min_duplicate_run: int = 3  # matches the Phase-2 DUPLICATED_PATTERN threshold


@dataclass(frozen=True)
class _Edit:
    start: tuple[int, int]   # (line 1-based, col 0-based), inclusive
    end: tuple[int, int]     # exclusive
    replacement: str


@dataclass(frozen=True)
class _Candidate:
    edit: _Edit
    kind: str
    risk: Risk
    line: int
    reason: str


class _SourceMap:
    """Exact source-text extraction at AST (line, column) spans."""

    def __init__(self, source: str) -> None:
        self.lines: list[str] = source.splitlines(keepends=True)

    def text(self, node: ast.AST) -> str:
        return self.extract(
            node.lineno, node.col_offset, node.end_lineno, node.end_col_offset
        )

    def extract(self, sl: int, sc: int, el: int, ec: int) -> str:
        n = len(self.lines)
        if sl > n:
            return ""
        if el > n:  # clamp to end of file
            el, ec = n, len(self.lines[n - 1])
        if sl == el:
            return self.lines[sl - 1][sc:ec]
        parts = [self.lines[sl - 1][sc:]]
        if el - 1 > sl:
            parts.extend(self.lines[sl:el - 1])
        parts.append(self.lines[el - 1][:ec])
        return "".join(parts)


# --- shared expression helpers ------------------------------------------------


def _is_numeric_constant(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    )


def _is_simple_embed(node: ast.AST) -> bool:
    """Names, dotted attributes, and numeric constants only.

    Deliberately conservative: f-string expressions cannot contain the
    delimiter quote, backslashes, or newlines before Python 3.12, and this
    whitelist sidesteps every such edge case.
    """
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.Attribute):
        return _is_simple_embed(node.value)
    if isinstance(node, ast.Constant):
        return _is_numeric_constant(node)
    return False


def _choose_quote(text: str) -> "str | None":
    """Pick an f-string delimiter the text never contains, if possible."""
    if '"' not in text:
        return '"'
    if "'" not in text:
        return "'"
    return None


def _escape_fstring_literal(text: str, quote: str) -> str:
    """Escape a literal chunk for embedding in an f-string."""
    out: list[str] = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ch == quote:
            out.append("\\" + quote)
        elif ch == "{":
            out.append("{{")
        elif ch == "}":
            out.append("}}")
        else:
            out.append(ch)
    return "".join(out)


def _percent_pieces(fmt: str) -> "list[str | None] | None":
    """Split a %-format string into literal chunks and %s placeholders.

    ``None`` entries mark ``%s`` placeholders. Returns ``None`` for anything
    beyond ``%s`` / ``%%`` (widths, ``%d``, ``%r``, mapping keys, ...),
    which this rule deliberately does not attempt.
    """
    pieces: "list[str | None]" = []
    literal: list[str] = []
    i = 0
    while i < len(fmt):
        ch = fmt[i]
        if ch == "%":
            if i + 1 >= len(fmt):
                return None
            nxt = fmt[i + 1]
            if nxt == "s":
                pieces.append("".join(literal))
                literal = []
                pieces.append(None)
                i += 2
            elif nxt == "%":
                literal.append("%")
                i += 2
            else:
                return None
        else:
            literal.append(ch)
            i += 1
    pieces.append("".join(literal))
    return pieces


def _format_pieces(fmt: str) -> "list[str | None] | None":
    """Split a str.format template into literals and automatic ``{}`` fields.

    Any manual field (``{0}``, ``{name}``, ``{:spec}``) or escaped brace
    disqualifies the string (returns ``None``).
    """
    pieces: "list[str | None]" = []
    literal: list[str] = []
    i = 0
    while i < len(fmt):
        ch = fmt[i]
        if ch == "{":
            if i + 1 < len(fmt) and fmt[i + 1] == "}":
                pieces.append("".join(literal))
                literal = []
                pieces.append(None)
                i += 2
            else:
                return None
        elif ch == "}":
            return None
        else:
            literal.append(ch)
            i += 1
    pieces.append("".join(literal))
    return pieces


def _alias_text(alias: ast.alias) -> str:
    if alias.asname is None:
        return alias.name
    return f"{alias.name} as {alias.asname}"


def _is_numeric_aug_pattern(stmt: ast.stmt) -> bool:
    """``x = x + <int>`` -- the only duplicate pattern we can collapse."""
    return (
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
        and isinstance(stmt.value, ast.BinOp)
        and isinstance(stmt.value.op, ast.Add)
        and isinstance(stmt.value.left, ast.Name)
        and stmt.value.left.id == stmt.targets[0].id
        and isinstance(stmt.value.right, ast.Constant)
        and isinstance(stmt.value.right.value, int)
        and not isinstance(stmt.value.right.value, bool)
    )


# --- structural guard -----------------------------------------------------------


def _module_signature(module) -> tuple:
    functions = tuple(
        (
            fn.qualified_name,
            tuple((p.name, p.kind, p.default is not None) for p in fn.params),
        )
        for fn in module.functions
    )
    classes = tuple(
        (c.qualified_name, c.bases, c.methods) for c in module.classes
    )
    variables = tuple(sorted(module.module_variables))
    return (functions, classes, variables)


def structural_guard(
    original_source: str, migrated_source: str, filename: str = "<string>"
) -> "tuple[bool, str | None]":
    """Verify that the structure every Phase-4 rule promises to preserve did
    survive: the ordered function list with full parameter shapes, classes
    with bases and methods, and module-level variable names. Imports, call
    expressions, and statement bodies are allowed to change -- that is what
    migration means.

    Returns ``(ok, reason)``; ``reason`` is ``None`` on success.
    """
    before = analyze_source(original_source, filename=filename).module
    after = analyze_source(migrated_source, filename=filename).module
    sig_before = _module_signature(before)
    sig_after = _module_signature(after)
    if sig_before == sig_after:
        return True, None
    if sig_before[0] != sig_after[0]:
        detail = "function definitions or signatures changed"
    elif sig_before[1] != sig_after[1]:
        detail = "class definitions changed"
    else:
        detail = "module-level variables changed"
    return False, f"structural guard failed: {detail}"


# --- the engine -------------------------------------------------------------------


class TransformationEngine:
    """Applies all deterministic rules to one source file."""

    _AUG_OPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*"}

    def __init__(self, config: MigrationConfig | None = None) -> None:
        self.config = config if config is not None else MigrationConfig()

    # -- public entry point --------------------------------------------------

    def transform_source(
        self, source: str, filename: str = "<string>"
    ) -> MigrationResult:
        """Run all rules; validate; never return broken code.

        Raises:
            SourceParseError: if ``source`` is not valid Python.
        """
        tree = parse_source(source, filename=filename)
        source_map = _SourceMap(source)

        candidates: list[_Candidate] = []
        candidates.extend(self._has_key_to_in(tree, source_map))
        candidates.extend(self._percent_to_fstring(tree, source_map))
        candidates.extend(self._format_to_fstring(tree, source_map))
        candidates.extend(self._bare_except_typing(tree, source_map))
        candidates.extend(self._aug_assign(tree, source_map))
        candidates.extend(self._duplicate_collapse(tree, source_map))
        candidates.extend(self._unused_imports(tree, source_map))

        accepted = self._resolve_overlaps(candidates)
        transformations = [
            Transformation(
                file=filename,
                line=c.line,
                kind=c.kind,
                risk=c.risk,
                original=source_map.extract(
                    c.edit.start[0], c.edit.start[1], c.edit.end[0], c.edit.end[1]
                ),
                replacement=c.edit.replacement,
                reason=c.reason,
            )
            for c in accepted
        ]
        if not accepted:
            return MigrationResult(
                filename=filename,
                original_source=source,
                migrated_source=source,
                transformations=[],
                applied=False,
                syntax_valid=True,
                structural_guard_passed=True,
                rejected_reason=None,
            )

        migrated = self._apply_edits(source, [c.edit for c in accepted])
        try:
            ast.parse(migrated)
        except SyntaxError:
            return MigrationResult(
                filename=filename,
                original_source=source,
                migrated_source=source,  # never hand back broken code
                transformations=transformations,
                applied=False,
                syntax_valid=False,
                structural_guard_passed=False,
                rejected_reason=(
                    "migrated source failed to parse; original returned unchanged"
                ),
            )
        ok, reason = structural_guard(source, migrated, filename=filename)
        if not ok:
            return MigrationResult(
                filename=filename,
                original_source=source,
                migrated_source=source,
                transformations=transformations,
                applied=False,
                syntax_valid=True,
                structural_guard_passed=False,
                rejected_reason=f"{reason}; original returned unchanged",
            )
        return MigrationResult(
            filename=filename,
            original_source=source,
            migrated_source=migrated,
            transformations=transformations,
            applied=True,
            syntax_valid=True,
            structural_guard_passed=True,
            rejected_reason=None,
        )

    # -- overlap resolution -----------------------------------------------------

    @staticmethod
    def _resolve_overlaps(candidates: list[_Candidate]) -> list[_Candidate]:
        """Deterministic greedy acceptance: sort by start position; when two
        candidates share a start, the *larger* span wins (a holistic rewrite
        beats several partial ones); anything overlapping an accepted edit
        is dropped."""
        ordered = sorted(
            candidates,
            key=lambda c: (
                c.edit.start[0], c.edit.start[1],
                -c.edit.end[0], -c.edit.end[1],
            ),
        )
        accepted: list[_Candidate] = []
        last_end: tuple[int, int] = (0, 0)
        for candidate in ordered:
            if candidate.edit.start >= last_end:
                accepted.append(candidate)
                last_end = candidate.edit.end
        return accepted

    @staticmethod
    def _apply_edits(source: str, edits: list[_Edit]) -> str:
        source_map = _SourceMap(source)
        parts: list[str] = []
        cursor = (1, 0)
        for edit in edits:
            parts.append(
                source_map.extract(
                    cursor[0], cursor[1], edit.start[0], edit.start[1]
                )
            )
            parts.append(edit.replacement)
            cursor = edit.end
        parts.append(
            source_map.extract(
                cursor[0], cursor[1], len(source_map.lines) + 1, 0
            )
        )
        return "".join(parts)

    # -- rule: HAS_KEY_TO_IN ------------------------------------------------------

    def _has_key_to_in(self, tree: ast.Module, source_map: _SourceMap) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "has_key"):
                continue
            if len(node.args) != 1 or node.keywords:
                continue
            parent = parents.get(node)
            negated = isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not)
            target_node = parent if negated else node
            key_text = source_map.text(node.args[0])
            dict_text = source_map.text(func.value)
            if not key_text or not dict_text:
                continue
            joiner = "not in" if negated else "in"
            # Always parenthesized: a comparison has lower precedence than
            # the call it replaces, so bare insertion could re-parse wrongly
            # (e.g. ``a + d.has_key(k)`` -> ``a + k in d``).
            replacement = f"({key_text} {joiner} {dict_text})"
            candidates.append(
                _Candidate(
                    edit=_Edit(
                        start=(target_node.lineno, target_node.col_offset),
                        end=(target_node.end_lineno, target_node.end_col_offset),
                        replacement=replacement,
                    ),
                    kind=TransformKind.HAS_KEY_TO_IN,
                    risk=Risk.SAFE,
                    line=target_node.lineno,
                    reason=(
                        "Deprecated Python 2 dict API. For dictionaries, "
                        "'k in d' is exactly equivalent to 'd.has_key(k)' "
                        "and works on every Python 3 container."
                    ),
                )
            )
        return candidates

    # -- rule: PERCENT_TO_FSTRING ---------------------------------------------------

    def _percent_to_fstring(self, tree: ast.Module, source_map: _SourceMap) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Mod)
                and isinstance(node.left, ast.Constant)
                and isinstance(node.left.value, str)
                and isinstance(node.right, ast.Tuple)
            ):
                continue
            elts = node.right.elts
            if any(isinstance(e, ast.Starred) for e in elts):
                continue
            if not all(_is_simple_embed(e) for e in elts):
                continue
            fmt = node.left.value
            quote = _choose_quote(fmt)
            if quote is None:
                continue
            pieces = _percent_pieces(fmt)
            if pieces is None or pieces.count(None) != len(elts):
                continue
            texts = [source_map.text(e) for e in elts]
            body = ""
            arg_index = 0
            for piece in pieces:
                if piece is None:
                    body += "{" + texts[arg_index] + "}"
                    arg_index += 1
                else:
                    body += _escape_fstring_literal(piece, quote)
            replacement = f"f{quote}{body}{quote}"
            candidates.append(
                _Candidate(
                    edit=_Edit(
                        start=(node.lineno, node.col_offset),
                        end=(node.end_lineno, node.end_col_offset),
                        replacement=replacement,
                    ),
                    kind=TransformKind.PERCENT_TO_FSTRING,
                    risk=Risk.REVIEW,
                    line=node.lineno,
                    reason=(
                        "'%' formatting with %s applies str(); an f-string "
                        "applies format(x, ''). These agree for all builtins "
                        "and almost every class, but a class overriding "
                        "__format__ inconsistently with __str__ would "
                        "observe a difference."
                    ),
                )
            )
        return candidates

    # -- rule: FORMAT_TO_FSTRING -----------------------------------------------------

    def _format_to_fstring(self, tree: ast.Module, source_map: _SourceMap) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "format"):
                continue
            receiver = func.value
            if not (
                isinstance(receiver, ast.Constant)
                and isinstance(receiver.value, str)
            ):
                continue
            if node.keywords or not node.args:
                continue
            if any(isinstance(a, ast.Starred) for a in node.args):
                continue
            if not all(_is_simple_embed(a) for a in node.args):
                continue
            fmt = receiver.value
            quote = _choose_quote(fmt)
            if quote is None:
                continue
            pieces = _format_pieces(fmt)
            if pieces is None or pieces.count(None) != len(node.args):
                continue
            texts = [source_map.text(a) for a in node.args]
            body = ""
            arg_index = 0
            for piece in pieces:
                if piece is None:
                    body += "{" + texts[arg_index] + "}"
                    arg_index += 1
                else:
                    body += _escape_fstring_literal(piece, quote)
            replacement = f"f{quote}{body}{quote}"
            candidates.append(
                _Candidate(
                    edit=_Edit(
                        start=(node.lineno, node.col_offset),
                        end=(node.end_lineno, node.end_col_offset),
                        replacement=replacement,
                    ),
                    kind=TransformKind.FORMAT_TO_FSTRING,
                    risk=Risk.SAFE,
                    line=node.lineno,
                    reason=(
                        "'str.format' with automatic placeholders and "
                        "f-strings both render each argument with "
                        "format(x, ''), so the rewrite is exactly equivalent."
                    ),
                )
            )
        return candidates

    # -- rule: BARE_EXCEPT_TYPING --------------------------------------------------------

    def _bare_except_typing(self, tree: ast.Module, source_map: _SourceMap) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ExceptHandler) and node.type is None):
                continue
            line_index = node.lineno - 1
            if line_index >= len(source_map.lines):
                continue
            line = source_map.lines[line_index]
            if line[node.col_offset:node.col_offset + 6] != "except":
                continue
            colon = line.find(":", node.col_offset + 6)
            if colon == -1:
                continue  # pathological layout; leave untouched
            candidates.append(
                _Candidate(
                    edit=_Edit(
                        start=(node.lineno, node.col_offset),
                        end=(node.lineno, colon + 1),
                        replacement="except Exception:",
                    ),
                    kind=TransformKind.BARE_EXCEPT,
                    risk=Risk.REVIEW,
                    line=node.lineno,
                    reason=(
                        "A bare 'except:' also catches SystemExit, "
                        "KeyboardInterrupt, and GeneratorExit; catching "
                        "Exception is the recommended modern form."
                    ),
                )
            )
        return candidates

    # -- rule: AUG_ASSIGN_MODERNIZE ---------------------------------------------------------

    def _aug_assign(self, tree: ast.Module, source_map: _SourceMap) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            value = node.value
            if not (
                isinstance(value, ast.BinOp) and type(value.op) in self._AUG_OPS
            ):
                continue
            target_name = node.targets[0].id
            op_symbol = self._AUG_OPS[type(value.op)]
            constant = None
            if (
                isinstance(value.left, ast.Name)
                and value.left.id == target_name
                and _is_numeric_constant(value.right)
            ):
                constant = value.right
            elif (
                op_symbol in ("+", "*")
                and isinstance(value.right, ast.Name)
                and value.right.id == target_name
                and _is_numeric_constant(value.left)
            ):
                constant = value.left  # commutative form: x = 2 + x
            if constant is None:
                continue
            name_text = source_map.text(node.targets[0])
            const_text = source_map.text(constant)
            candidates.append(
                _Candidate(
                    edit=_Edit(
                        start=(node.lineno, node.col_offset),
                        end=(node.end_lineno, node.end_col_offset),
                        replacement=f"{name_text} {op_symbol}= {const_text}",
                    ),
                    kind=TransformKind.AUG_ASSIGN,
                    risk=Risk.REVIEW,
                    line=node.lineno,
                    reason=(
                        "Augmented assignment is the modern form of "
                        "'x = x + n'. Identical for numbers; a class that "
                        "defines __iadd__ (and friends) to differ from "
                        "__add__ would observe a change."
                    ),
                )
            )
        return candidates

    # -- rule: DUPLICATE_RUN_COLLAPSE ----------------------------------------------------------

    def _duplicate_collapse(self, tree: ast.Module, source_map: _SourceMap) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        for body in _iter_statement_lists(tree):
            index = 0
            while index < len(body):
                signature = ast.dump(body[index])
                end = index + 1
                while (
                    end < len(body)
                    and ast.dump(body[end]) == signature
                ):
                    end += 1
                run = end - index
                if run >= self.config.min_duplicate_run and _is_numeric_aug_pattern(
                    body[index]
                ):
                    first = body[index]
                    last = body[end - 1]
                    name = first.targets[0].id
                    step = first.value.right.value
                    total = run * step
                    candidates.append(
                        _Candidate(
                            edit=_Edit(
                                start=(first.lineno, first.col_offset),
                                end=(last.end_lineno, last.end_col_offset),
                                replacement=f"{name} += {total}",
                            ),
                            kind=TransformKind.DUPLICATE_COLLAPSE,
                            risk=Risk.REVIEW,
                            line=first.lineno,
                            reason=(
                                f"Collapses {run} identical "
                                f"'{name} = {name} + {step}' statements into "
                                f"one augmented assignment. Exact for "
                                f"integers; float rounding and __add__ call "
                                f"counts are the documented caveats."
                            ),
                        )
                    )
                index = end
        return candidates

    # -- rule: UNUSED_IMPORT_REMOVAL -------------------------------------------------------------

    def _unused_imports(self, tree: ast.Module, source_map: _SourceMap) -> list[_Candidate]:
        usage = _collect_usage(tree)
        reads = usage.module_scope.reads
        candidates: list[_Candidate] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "__future__":
                    continue
                aliases = [a for a in node.names if a.name != "*"]
                stars = [a for a in node.names if a.name == "*"]
                if not aliases:
                    continue

                def bound_name(a: ast.alias) -> str:
                    return a.asname or a.name

            elif isinstance(node, ast.Import):
                aliases = [a for a in node.names if a.name != "*"]
                stars = []
                if not aliases:
                    continue

                def bound_name(a: ast.alias) -> str:
                    return a.asname or a.name.split(".")[0]

            else:
                continue

            keep = [a for a in aliases if bound_name(a) in reads]
            if len(keep) == len(aliases):
                continue  # nothing unused
            unused_names = [bound_name(a) for a in aliases if bound_name(a) not in reads]
            reason = self._unused_import_reason(unused_names)

            if keep or stars:
                kept_text = ", ".join(_alias_text(a) for a in keep + stars)
                if isinstance(node, ast.ImportFrom):
                    prefix = "." * (node.level or 0) + (node.module or "")
                    replacement = f"from {prefix} import {kept_text}"
                else:
                    replacement = f"import {kept_text}"
                edit = _Edit(
                    start=(node.lineno, node.col_offset),
                    end=(node.end_lineno, node.end_col_offset),
                    replacement=replacement,
                )
            else:
                edit = self._whole_line_removal(node, source_map)
                if edit is None:
                    continue
            candidates.append(
                _Candidate(
                    edit=edit,
                    kind=TransformKind.UNUSED_IMPORT,
                    risk=Risk.REVIEW,
                    line=node.lineno,
                    reason=reason,
                )
            )
        return candidates

    @staticmethod
    def _unused_import_reason(names: list[str]) -> str:
        listed = ", ".join(f"'{n}'" for n in names)
        return (
            f"Phase-2 unused-import analysis: {listed} never read in this "
            f"module. Import side effects are the only possible behavior "
            f"difference."
        )

    @staticmethod
    def _whole_line_removal(node: ast.stmt, source_map: _SourceMap) -> "_Edit | None":
        """Remove an entire statement line -- only when nothing else shares it."""
        line_index = node.lineno - 1
        if line_index >= len(source_map.lines):
            return None
        line = source_map.lines[line_index]
        prefix = line[: node.col_offset]
        suffix = line[node.end_col_offset:]
        if prefix.strip() or suffix.strip():
            return None  # statement shares its line; leave untouched
        if line.endswith("\n"):
            return _Edit(
                start=(node.lineno, node.col_offset),
                end=(node.lineno + 1, 0),
                replacement="",
            )
        return _Edit(
            start=(node.lineno, node.col_offset),
            end=(node.lineno, len(line)),
            replacement="",
        )