"""Tests for backend.verification.test_generator."""
from __future__ import annotations

from backend.analyzer import analyze_source
from backend.verification import GeneratedTest, generate_tests


def cases_of(source: str):
    return generate_tests(analyze_source(source, filename="inline.py"))


def brief(cases):
    return [(c.function, c.description, c.args, dict(c.kwargs)) for c in cases]


def test_calculator_function_selection(calculator_analysis):
    cases = generate_tests(calculator_analysis)
    functions = list(dict.fromkeys(c.function for c in cases))
    assert functions == ["validate_amount", "calculate_tax", "calculate_total"]
    assert len(cases) == 12


def test_validate_amount_cases(calculator_analysis):
    cases = [
        c for c in generate_tests(calculator_analysis)
        if c.function == "validate_amount"
    ]
    # "empty" (0.0) deduplicates against "boundary" (0.0) for floats.
    assert brief(cases) == [
        ("validate_amount", "normal", (2.5,), {}),
        ("validate_amount", "boundary", (0.0,), {}),
        ("validate_amount", "invalid", ("x",), {}),
    ]


def test_calculate_tax_cases(calculator_analysis):
    cases = [
        c for c in generate_tests(calculator_analysis)
        if c.function == "calculate_tax"
    ]
    assert brief(cases) == [
        ("calculate_tax", "normal", (2.5, 2.5), {}),
        ("calculate_tax", "boundary", (0.0, 0.0), {}),
        ("calculate_tax", "invalid", ("x", 2.5), {}),
        ("calculate_tax", "default", (2.5,), {}),
    ]


def test_calculate_total_cases(calculator_analysis):
    cases = [
        c for c in generate_tests(calculator_analysis)
        if c.function == "calculate_total"
    ]
    # "items" has no plain-builtin annotation -> LIST name heuristic.
    assert brief(cases) == [
        ("calculate_total", "normal", (2.5, ["a", "bb", "cccc"]), {}),
        ("calculate_total", "boundary", (0.0, ["x"]), {}),
        ("calculate_total", "empty", (0.0, []), {}),
        ("calculate_total", "invalid", ("x", ["a", "bb", "cccc"]), {}),
        ("calculate_total", "default", (2.5,), {}),
    ]


def test_annotations_drive_types():
    source = (
        "def f(n: int, s: str, r: float, flag: bool, items: list):\n"
        "    return n\n"
    )
    (case,) = [c for c in cases_of(source) if c.description == "normal"]
    assert case.args == (5, "hello", 2.5, True, ["a", "bb", "cccc"])
    # kwargs is a TUPLE of (name, value) pairs by model definition; all
    # parameters here are positional, so it is the empty tuple.
    assert case.kwargs == ()


def test_name_heuristics_when_unannotated():
    source = "def f(count, name, rate, flag, items):\n    return count\n"
    (case,) = [c for c in cases_of(source) if c.description == "normal"]
    assert case.args == (5, "hello", 2.5, True, ["a", "bb", "cccc"])


def test_kwonly_and_defaults():
    source = "def f(a, *, flag=False, count):\n    return a\n"
    cases = {c.description: c for c in cases_of(source)}
    assert cases["normal"].args == (5,)
    assert dict(cases["normal"].kwargs) == {"flag": True, "count": 5}
    # "default" omits the defaulted kwonly parameter but keeps the required one
    assert cases["default"].args == (5,)
    assert dict(cases["default"].kwargs) == {"count": 5}


def test_zero_parameter_function():
    (case,) = cases_of("def zero():\n    return 1\n")
    assert case.function == "zero"
    assert case.description == "no-args"
    assert case.args == () and case.kwargs == ()


def test_skipped_functions():
    source = (
        "class C:\n"
        "    def method(self):\n"
        "        return 1\n"
        "\n"
        "def _private():\n"
        "    return 1\n"
        "\n"
        "def main():\n"
        "    return 1\n"
        "\n"
        "async def afunc(x):\n"
        "    return x\n"
        "\n"
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "    return inner\n"
        "\n"
        "def public(x):\n"
        "    return x\n"
    )
    cases = cases_of(source)
    # 'outer' is module-level, so it IS testable per the documented rules
    # (its NESTED 'inner' is what gets skipped): one no-args case. The
    # method, _private, main, and async function are all skipped.
    assert list(dict.fromkeys(c.function for c in cases)) == ["outer", "public"]
    no_args = [c for c in cases if c.description == "no-args"]
    assert len(no_args) == 1 and no_args[0].function == "outer"
    assert all(c.function != "outer.inner" for c in cases)


def test_generated_test_serialization():
    case = GeneratedTest(
        function="f", description="normal", args=(1, "a"), kwargs=(("k", 2),)
    )
    data = case.to_dict()
    assert data == {
        "function": "f", "description": "normal",
        "args": [1, "a"], "kwargs": {"k": 2},
    }