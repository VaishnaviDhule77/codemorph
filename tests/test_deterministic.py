"""Tests for backend.migration.deterministic (Phase 4)."""
from __future__ import annotations

import json
from collections import Counter

import pytest

from backend.analyzer import SourceParseError, analyze_source, run_findings
from backend.analyzer.__main__ import main
from backend.migration import (
    MigrationConfig,
    Risk,
    TransformationEngine,
    TransformKind,
    structural_guard,
)


def migrate(source: str, filename: str = "inline.py",
            config: "MigrationConfig | None" = None):
    return TransformationEngine(config).transform_source(source, filename=filename)


def records(result) -> list[tuple[str, int, str]]:
    return [(t.kind, t.line, t.risk.value) for t in result.transformations]


# -- rule: has_key -> in ---------------------------------------------------------


def test_has_key_transforms():
    source = "def check(d, k):\n    return d.has_key(k)\n"
    result = migrate(source)
    assert result.migrated_source == "def check(d, k):\n    return (k in d)\n"
    assert result.applied
    assert records(result) == [(TransformKind.HAS_KEY_TO_IN, 2, "SAFE")]
    t = result.transformations[0]
    assert t.original == "d.has_key(k)"
    assert t.replacement == "(k in d)"
    assert t.file == "inline.py"
    assert t.reason


def test_has_key_negation():
    source = "def missing(d, k):\n    return not d.has_key(k)\n"
    result = migrate(source)
    assert result.migrated_source == "def missing(d, k):\n    return (k not in d)\n"
    t = result.transformations[0]
    assert t.original == "not d.has_key(k)"
    assert t.replacement == "(k not in d)"


def test_has_key_precedence_parens():
    # A comparison has lower precedence than the call it replaces, so the
    # rewrite must be parenthesized to stay correct inside larger expressions.
    source = "def f(a, d, k):\n    return a + d.has_key(k)\n"
    result = migrate(source)
    assert result.migrated_source == "def f(a, d, k):\n    return a + (k in d)\n"
    assert result.structural_guard_passed


# -- rule: % -> f-string ------------------------------------------------------------


def test_percent_to_fstring():
    source = "def greet(name):\n    return 'Hello %s!' % (name,)\n"
    result = migrate(source)
    assert result.migrated_source == 'def greet(name):\n    return f"Hello {name}!"\n'
    assert records(result) == [(TransformKind.PERCENT_TO_FSTRING, 2, "REVIEW")]
    t = result.transformations[0]
    assert t.original == "'Hello %s!' % (name,)"
    assert t.replacement == 'f"Hello {name}!"'


def test_percent_multiple_and_literal_percent():
    source = "def wrap(k, v):\n    return '%s=%s%%' % (k, v)\n"
    result = migrate(source)
    assert result.migrated_source == 'def wrap(k, v):\n    return f"{k}={v}%"\n'


def test_percent_braces_escaped():
    source = "def f(x):\n    return '%s {' % (x,)\n"
    result = migrate(source)
    assert result.migrated_source == 'def f(x):\n    return f"{x} {{"\n'


def test_percent_single_quote_in_value():
    # The format string VALUE contains a single quote, but the chosen
    # f-string delimiter (double quote) does not appear in the value, so the
    # rewrite is safe: quote choice happens on the value, never on the
    # source-level representation of the string.
    source = 'def f(x):\n    return "it\'s %s" % (x,)\n'
    result = migrate(source)
    assert result.migrated_source == 'def f(x):\n    return f"it\'s {x}"\n'
    assert records(result) == [(TransformKind.PERCENT_TO_FSTRING, 2, "REVIEW")]


@pytest.mark.parametrize("source", [
    "def f(n):\n    return '%d items' % (n,)\n",          # %d: int coercion, skip
    "def f(x):\n    return '%s' % x\n",                # non-tuple RHS, skip
    "def f(a):\n    return '%s %s' % (a, a, a)\n",      # arity mismatch, skip
    "def f(d):\n    return '%s' % (d['k'],)\n",        # non-simple expression
    'def f(x):\n    return "it\'s \\"q\\" %s" % (x,)\n',  # value holds BOTH quotes
])
def test_percent_rule_is_conservative(source):
    result = migrate(source)
    assert result.transformations == []
    assert result.migrated_source == source
    assert result.applied is False


# -- rule: .format -> f-string ---------------------------------------------------------


def test_format_call_to_fstring():
    source = "def greet(name):\n    return 'Hello {}!'.format(name)\n"
    result = migrate(source)
    assert result.migrated_source == 'def greet(name):\n    return f"Hello {name}!"\n'
    assert records(result) == [(TransformKind.FORMAT_TO_FSTRING, 2, "SAFE")]


def test_format_two_args():
    source = "def pair(a, b):\n    return '{} and {}'.format(a, b)\n"
    result = migrate(source)
    assert result.migrated_source == 'def pair(a, b):\n    return f"{a} and {b}"\n'


@pytest.mark.parametrize("source", [
    "def f(x):\n    return '{0}'.format(x)\n",     # positional field
    "def f(x):\n    return '{name}'.format(x)\n",  # named field
    "def f(x):\n    return '{:.2f}'.format(x)\n",  # format spec
    "def f(x):\n    return '{} {}'.format(x)\n",   # arity mismatch
])
def test_format_rule_is_conservative(source):
    result = migrate(source)
    assert result.transformations == []
    assert result.migrated_source == source


# -- rule: bare except --------------------------------------------------------------------


def test_bare_except_modernization():
    source = (
        "def f(block):\n"
        "    try:\n"
        "        return block()\n"
        "    except:\n"
        "        return None\n"
    )
    result = migrate(source)
    assert result.migrated_source == (
        "def f(block):\n    try:\n        return block()\n"
        "    except Exception:\n        return None\n"
    )
    assert records(result) == [(TransformKind.BARE_EXCEPT, 4, "REVIEW")]
    t = result.transformations[0]
    assert t.original == "except:"
    assert t.replacement == "except Exception:"


def test_bare_except_preserves_comment():
    source = (
        "def f(block):\n"
        "    try:\n"
        "        return block()\n"
        "    except:  # noqa\n"
        "        return None\n"
    )
    result = migrate(source)
    assert "    except Exception:  # noqa\n" in result.migrated_source


# -- rule: augmented assignment --------------------------------------------------------------


def test_aug_assign_modernization():
    source = (
        "def f(x):\n"
        "    x = x + 1\n"
        "    x = x - 2\n"
        "    x = x * 3\n"
        "    x = 2 + x\n"
        "    return x\n"
    )
    result = migrate(source)
    assert result.migrated_source == (
        "def f(x):\n    x += 1\n    x -= 2\n    x *= 3\n    x += 2\n    return x\n"
    )
    assert records(result) == [
        (TransformKind.AUG_ASSIGN, 2, "REVIEW"),
        (TransformKind.AUG_ASSIGN, 3, "REVIEW"),
        (TransformKind.AUG_ASSIGN, 4, "REVIEW"),
        (TransformKind.AUG_ASSIGN, 5, "REVIEW"),
    ]


def test_aug_assign_rejected_cases():
    cases = [
        "def f(x, y):\n    x = x + y\n    return x\n",     # non-constant operand
        "def f(x):\n    y = x + 1\n    return y\n",        # target differs from operand
        "def f(x):\n    x = x ** 2\n    return x\n",       # unsupported operator
        "def f(x):\n    x = x / 2\n    return x\n",        # division unsupported
        "def f(x):\n    x = x + True\n    return x\n",     # bool is not a number here
        "def f(x):\n    x = 2 - x\n    return x\n",        # non-commutative form
    ]
    for source in cases:
        result = migrate(source)
        assert result.transformations == [], source
        assert result.migrated_source == source


# -- rule: duplicate-run collapse ---------------------------------------------------------------


def test_duplicate_run_collapse():
    source = (
        "def bump(total):\n"
        "    total = total + 1\n"
        "    total = total + 1\n"
        "    total = total + 1\n"
        "    return total\n"
    )
    result = migrate(source)
    assert result.migrated_source == "def bump(total):\n    total += 3\n    return total\n"
    # one holistic transformation beats three overlapping AUG_ASSIGN edits
    assert records(result) == [(TransformKind.DUPLICATE_COLLAPSE, 2, "REVIEW")]
    t = result.transformations[0]
    # The span extractor returns the EXACT source text of the run: it starts
    # at column 4 of the first statement and ends at the last one, so the
    # continuation lines keep their 4-space indentation.
    assert t.original == (
        "total = total + 1\n"
        "    total = total + 1\n"
        "    total = total + 1"
    )
    assert t.replacement == "total += 3"


def test_duplicate_min_run_configurable():
    source = "def f(x):\n    x = x + 1\n    x = x + 1\n    return x\n"
    default = migrate(source)
    assert records(default) == [
        (TransformKind.AUG_ASSIGN, 2, "REVIEW"),
        (TransformKind.AUG_ASSIGN, 3, "REVIEW"),
    ]
    assert default.migrated_source == "def f(x):\n    x += 1\n    x += 1\n    return x\n"
    collapsed = migrate(source, config=MigrationConfig(min_duplicate_run=2))
    assert records(collapsed) == [(TransformKind.DUPLICATE_COLLAPSE, 2, "REVIEW")]
    assert collapsed.migrated_source == "def f(x):\n    x += 2\n    return x\n"


def test_duplicate_arbitrary_runs_not_collapsed():
    # Identical statements we have no safe rewrite for stay untouched.
    source = (
        "def f():\n"
        "    print('x')\n    print('x')\n    print('x')\n"
        "    return 1\n"
    )
    result = migrate(source)
    assert result.transformations == []
    assert result.migrated_source == source


# -- rule: unused-import removal -------------------------------------------------------------


def test_unused_import_full_removal():
    source = "import os\n\n\ndef f():\n    return 1\n"
    result = migrate(source)
    assert result.migrated_source == "\n\ndef f():\n    return 1\n"
    assert records(result) == [(TransformKind.UNUSED_IMPORT, 1, "REVIEW")]
    t = result.transformations[0]
    assert t.original == "import os\n"
    assert t.replacement == ""


def test_unused_import_partial_from_import():
    source = "from math import pi, sqrt\n\n\ndef area(r):\n    return pi * r * r\n"
    result = migrate(source)
    assert result.migrated_source == (
        "from math import pi\n\n\ndef area(r):\n    return pi * r * r\n"
    )
    t = result.transformations[0]
    assert t.original == "from math import pi, sqrt"
    assert t.replacement == "from math import pi"


def test_unused_import_plain_mixed():
    source = "import os, sys\n\n\ndef f():\n    return sys.path\n"
    result = migrate(source)
    assert result.migrated_source == "import sys\n\n\ndef f():\n    return sys.path\n"


def test_clean_calculator_is_untouched(calculator_source):
    result = migrate(calculator_source, filename="calculator.py")
    assert result.transformations == []
    assert result.migrated_source == calculator_source
    assert result.applied is False
    assert result.syntax_valid and result.structural_guard_passed


# -- integration: the smelly fixture ------------------------------------------------------------


def test_smelly_migration_integration(smelly_source):
    result = migrate(smelly_source, filename="smelly.py")
    assert result.applied
    assert result.syntax_valid and result.structural_guard_passed
    assert records(result) == [
        (TransformKind.UNUSED_IMPORT, 7, "REVIEW"),
        (TransformKind.UNUSED_IMPORT, 8, "REVIEW"),
        (TransformKind.UNUSED_IMPORT, 9, "REVIEW"),
        (TransformKind.DUPLICATE_COLLAPSE, 37, "REVIEW"),
        (TransformKind.BARE_EXCEPT, 135, "REVIEW"),
    ]
    assert "import os" not in result.migrated_source
    assert "import sys" not in result.migrated_source
    assert "from math import pi, sqrt" not in result.migrated_source
    assert "from math import pi\n" in result.migrated_source
    assert "total += 3" in result.migrated_source
    assert "except Exception:" in result.migrated_source
    assert "    except:\n" not in result.migrated_source


def test_smelly_migration_reduces_findings(smelly_source):
    """Migration driven by static analysis measurably improves the code:
    18 findings -> 13 after one deterministic pass."""
    before = analyze_source(smelly_source, filename="smelly.py")
    result = migrate(smelly_source, filename="smelly.py")
    after = analyze_source(result.migrated_source, filename="smelly_migrated.py")
    counts_before = Counter(f.category for f in run_findings(before))
    counts_after = Counter(f.category for f in run_findings(after))
    assert sum(counts_before.values()) == 18
    assert counts_before["UNUSED_IMPORT"] == 3
    assert counts_after["UNUSED_IMPORT"] == 0
    assert counts_before["DUPLICATED_PATTERN"] == 1
    assert counts_after["DUPLICATED_PATTERN"] == 0
    assert counts_before["BARE_EXCEPT"] == 1
    assert counts_after["BARE_EXCEPT"] == 0
    assert counts_after["UNUSED_VARIABLE"] == 4      # unchanged: not migrated
    assert counts_after["DANGEROUS_EVAL"] == 1       # needs the LLM phase
    assert sum(counts_after.values()) == 13


def test_transformations_are_traceable(smelly_source):
    result = migrate(smelly_source, filename="smelly.py")
    assert result.transformations  # guard against a vacuous pass
    for t in result.transformations:
        assert t.original in smelly_source
        assert t.replacement in result.migrated_source
        assert t.file == "smelly.py"
        assert t.line >= 1
        assert t.reason


def test_idempotency():
    source = (
        "import os\n"
        "\n"
        "def f(d, k, name):\n"
        "    if not d.has_key(k):\n"
        "        return 'missing %s' % (name,)\n"
        "    x = 1\n"
        "    x = x + 1\n"
        "    return x\n"
    )
    first = migrate(source)
    assert records(first) == [
        (TransformKind.UNUSED_IMPORT, 1, "REVIEW"),
        (TransformKind.HAS_KEY_TO_IN, 4, "SAFE"),
        (TransformKind.PERCENT_TO_FSTRING, 5, "REVIEW"),
        (TransformKind.AUG_ASSIGN, 7, "REVIEW"),
    ]
    assert first.migrated_source == (
        "\n"
        "def f(d, k, name):\n"
        "    if (k not in d):\n"
        '        return f"missing {name}"\n'
        "    x = 1\n"
        "    x += 1\n"
        "    return x\n"
    )
    second = migrate(first.migrated_source)
    assert second.transformations == []
    assert second.migrated_source == first.migrated_source


# -- structural guard ------------------------------------------------------------------------


def test_structural_guard_detects_changes():
    source = "def f(a, b):\n    return a + b\n"
    ok, reason = structural_guard(source, source)
    assert ok and reason is None
    ok, reason = structural_guard(source, source + "\ndef extra():\n    pass\n")
    assert not ok and "function" in reason
    ok, reason = structural_guard(source, "def f(a, c):\n    return a + c\n")
    assert not ok and "signature" in reason
    ok, reason = structural_guard(source, "X = 1\n" + source)
    assert not ok and "variable" in reason


# -- serialization -----------------------------------------------------------------------------


def test_transformation_to_dict(smelly_source):
    result = migrate(smelly_source, filename="smelly.py")
    data = json.loads(json.dumps(result.transformations[0].to_dict()))
    assert set(data) == {
        "file", "line", "kind", "risk", "original", "replacement", "reason",
    }
    assert data["file"] == "smelly.py"
    assert data["line"] == 7
    assert data["kind"] == "UNUSED_IMPORT_REMOVAL"
    assert data["risk"] == "REVIEW"
    assert data["original"] == "import os\n"
    assert data["replacement"] == ""
    assert data["reason"]


def test_result_to_dict(smelly_source):
    result = migrate(smelly_source, filename="smelly.py")
    data = json.loads(json.dumps(result.to_dict()))
    assert set(data) == {
        "filename", "applied", "syntax_valid", "structural_guard_passed",
        "rejected_reason", "transformation_count", "transformations",
        "migrated_source",
    }
    assert data["applied"] is True
    assert data["transformation_count"] == 5
    assert data["rejected_reason"] is None
    assert "except Exception:" in data["migrated_source"]


def test_syntax_error_propagates():
    with pytest.raises(SourceParseError):
        migrate("def broken(:\n")


# -- CLI ------------------------------------------------------------------------------------------


def test_cli_migrate_prints_report(tmp_path, capsys):
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n\n\ndef f():\n    return 1\n", encoding="utf-8")
    assert main([str(sample), "--migrate"]) == 0
    out = capsys.readouterr().out
    assert "Deterministic migrations: 1 transformation(s), applied" in out
    assert "UNUSED_IMPORT_REMOVAL" in out
    assert "- import os" in out
    assert "reason:" in out


def test_cli_migrate_out(tmp_path, capsys):
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n\n\ndef f():\n    return 1\n", encoding="utf-8")
    out_path = tmp_path / "migrated.py"
    assert main([str(sample), "--migrate", "--migrate-out", str(out_path)]) == 0
    migrated = out_path.read_text(encoding="utf-8")
    assert "import os" not in migrated
    assert "def f" in migrated
    assert f"wrote migrated source to {out_path}" in capsys.readouterr().out


def test_cli_migrate_json(tmp_path, capsys):
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n\n\ndef f():\n    return 1\n", encoding="utf-8")
    assert main([str(sample), "--migrate", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["applied"] is True
    assert data["transformation_count"] == 1
    assert data["transformations"][0]["kind"] == "UNUSED_IMPORT_REMOVAL"
    assert "import os" not in data["migrated_source"]