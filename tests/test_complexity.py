"""Tests for backend.analyzer.complexity (McCabe)."""
from __future__ import annotations

import pytest

from backend.analyzer import analyze_source, rank_of

COMPLEXITY_SOURCE = (
    "import operator\n"
    "\n"
    "def baseline(x):\n"
    "    return x\n"
    "\n"
    "def decisions(a, b, c):\n"
    "    if a and b:\n"
    "        return 1\n"
    "    if a or b or c:\n"
    "        return 2\n"
    "    return 3\n"
    "\n"
    "def loops(items):\n"
    "    total = 0\n"
    "    for i in items:\n"
    "        while i:\n"
    "            i -= 1\n"
    "            break\n"
    "    return total\n"
    "\n"
    "def comprehensions(items):\n"
    "    evens = [x for x in items if x % 2 == 0]\n"
    "    squares = {x: x * x for x in evens}\n"
    "    return [x for x in squares]\n"
    "\n"
    "def exceptions(value):\n"
    "    try:\n"
    "        result = int(value)\n"
    "    except ValueError:\n"
    "        result = 0\n"
    "    except (TypeError, KeyError):\n"
    "        result = -1\n"
    "    finally:\n"
    "        assert result is not None\n"
    "    return result\n"
    "\n"
    "def ternary(x):\n"
    '    return "pos" if x > 0 else "neg"\n'
)


def complexities(source: str) -> dict[str, int]:
    report = analyze_source(source, filename="inline.py")
    return {fn.qualified_name: fn.complexity for fn in report.complexity.functions}


def test_known_complexity_values():
    values = complexities(COMPLEXITY_SOURCE)
    assert values["baseline"] == 1
    assert values["decisions"] == 6        # 1 + if + and + if + or + or
    assert values["loops"] == 3            # 1 + for + while
    assert values["comprehensions"] == 5   # 1 + (for+if) + for + for
    assert values["exceptions"] == 4       # 1 + 2 handlers + assert
    assert values["ternary"] == 2          # 1 + ternary


def test_module_level_complexity():
    report = analyze_source(COMPLEXITY_SOURCE, filename="inline.py")
    assert report.complexity.module_level == 1  # only def statements at top level


def test_nested_functions_are_measured_separately():
    source = (
        "def outer(x):\n"
        "    if x:\n"
        "        def inner(y):\n"
        "            if y:\n"
        "                return y\n"
        "            return 0\n"
        "        return inner(x)\n"
        "    return 0\n"
    )
    values = complexities(source)
    assert values["outer"] == 2
    assert values["outer.inner"] == 2


def test_elif_chain():
    source = (
        "def g(x):\n"
        "    if x == 1:\n"
        '        return "one"\n'
        "    elif x == 2:\n"
        '        return "two"\n'
        "    elif x == 3:\n"
        '        return "three"\n'
        "    else:\n"
        '        return "many"\n'
    )
    report = analyze_source(source, filename="inline.py")
    assert complexities(source)["g"] == 4   # 1 + three if/elif decision points
    fn = report.module.functions[0]
    assert fn.num_conditions == 3
    assert fn.max_nesting == 1              # elif does not deepen nesting


def test_deep_nesting():
    source = (
        "def f(x):\n"
        "    if x > 0:\n"
        "        for i in range(x):\n"
        "            if i:\n"
        "                while i:\n"
        "                    i -= 1\n"
        "    return x\n"
    )
    report = analyze_source(source, filename="inline.py")
    assert report.module.functions[0].max_nesting == 4
    assert report.module.max_nesting_depth == 4


def test_fixture_complexity(calculator_analysis):
    c = calculator_analysis.complexity
    values = {fn.qualified_name: fn.complexity for fn in c.functions}
    assert values == {
        "validate_amount": 2,
        "calculate_tax": 1,
        "calculate_total": 5,
        "Calculator.__init__": 1,
        "Calculator.add": 2,
        "Calculator._run_nested": 2,
        "Calculator._run_nested.clamp": 2,
        "main": 2,
    }
    assert c.module_level == 2
    assert c.total == 19
    assert c.average == pytest.approx(2.125, abs=0.01)
    assert c.max_function is not None
    assert c.max_function.qualified_name == "calculate_total"
    assert c.max_function.rank == "A"


def test_ranks():
    assert rank_of(1) == "A"
    assert rank_of(5) == "A"
    assert rank_of(6) == "B"
    assert rank_of(11) == "C"
    assert rank_of(21) == "D"
    assert rank_of(31) == "E"
    assert rank_of(41) == "F"