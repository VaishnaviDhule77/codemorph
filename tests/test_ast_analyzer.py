"""Tests for backend.analyzer.ast_analyzer (structural AST model)."""
from __future__ import annotations

import pytest

from backend.analyzer import FileAnalysis, SourceParseError, analyze_source


def by_name(analysis: FileAnalysis) -> dict:
    return {fn.qualified_name: fn for fn in analysis.module.functions}


# -- fixture-based tests ------------------------------------------------------


def test_all_functions_discovered_in_order(calculator_analysis):
    assert [fn.qualified_name for fn in calculator_analysis.module.functions] == [
        "validate_amount",
        "calculate_tax",
        "calculate_total",
        "Calculator.__init__",
        "Calculator.add",
        "Calculator._run_nested",
        "Calculator._run_nested.clamp",
        "main",
    ]


def test_function_flags(calculator_analysis):
    functions = by_name(calculator_analysis)
    assert functions["Calculator.add"].is_method is True
    assert functions["Calculator.add"].is_nested is False
    assert functions["Calculator._run_nested.clamp"].is_nested is True
    assert functions["Calculator._run_nested.clamp"].is_method is False
    assert functions["validate_amount"].is_async is False


def test_line_spans_and_length(calculator_analysis):
    fn = by_name(calculator_analysis)["calculate_total"]
    assert fn.lineno == 28
    assert fn.end_lineno == 42
    assert fn.length == 15


def test_parameters(calculator_analysis):
    functions = by_name(calculator_analysis)
    total = functions["calculate_total"]
    assert [(p.name, p.annotation, p.default) for p in total.params] == [
        ("amount", "float", None),
        ("items", "Optional[List[str]]", "None"),
    ]
    tax = functions["calculate_tax"]
    assert [(p.name, p.default) for p in tax.params] == [
        ("amount", None), ("rate", "TAX_RATE"),
    ]
    init = functions["Calculator.__init__"]
    assert [(p.name, p.default) for p in init.params] == [
        ("self", None), ("precision", "2"),
    ]
    assert functions["main"].params == ()


def test_imports(calculator_analysis):
    math_import, typing_import = calculator_analysis.module.imports
    assert math_import.is_from is False
    assert math_import.module == "math"
    assert math_import.bound_names == ("math",)
    assert math_import.statement == "import math"
    assert typing_import.is_from is True
    assert typing_import.module == "typing"
    assert typing_import.bound_names == ("List", "Optional")
    assert typing_import.statement == "from typing import List, Optional"


def test_module_variables(calculator_analysis):
    assert calculator_analysis.module.module_variables == [
        "DISCOUNT_THRESHOLD", "TAX_RATE",
    ]


def test_calculate_total_details(calculator_analysis):
    fn = by_name(calculator_analysis)["calculate_total"]
    assert fn.docstring == "Add tax and apply a volume discount."
    assert fn.variables == ("validated", "tax", "total", "items", "item", "receipt")
    assert [call.name for call in fn.calls] == [
        "validate_amount", "calculate_tax", "len", "math.fsum",
    ]
    assert fn.num_conditions == 2
    assert [(loop.kind, loop.lineno) for loop in fn.loops] == [("for", 35)]
    assert len(fn.exception_handlers) == 1
    assert fn.exception_handlers[0].exception_types == ("TypeError", "ValueError")
    assert fn.raises == ()
    assert len(fn.returns) == 1
    assert fn.returns[0].value == "receipt"
    assert fn.max_nesting == 2


def test_validate_amount_details(calculator_analysis):
    fn = by_name(calculator_analysis)["validate_amount"]
    assert fn.calls == ()          # raise ValueError(...) is not a call site
    assert fn.raises == ("ValueError",)
    assert fn.returns[0].value == "amount"
    assert fn.num_conditions == 1
    assert fn.max_nesting == 1


def test_main_details(calculator_analysis):
    fn = by_name(calculator_analysis)["main"]
    assert fn.variables == ("calc", "total", "i")
    assert [call.name for call in fn.calls] == [
        "Calculator", "calculate_total", "print", "calc.add", "range", "print",
    ]
    assert fn.returns == ()        # no explicit return statements
    assert [(loop.kind, loop.lineno) for loop in fn.loops] == [("for", 70)]


def test_classes(calculator_analysis):
    calculator = calculator_analysis.module.classes[0]
    assert calculator.name == "Calculator"
    assert calculator.bases == ()
    assert calculator.methods == ("__init__", "add", "_run_nested")
    assert calculator.class_variables == ()
    assert calculator.docstring == "A tiny calculator with a nested helper."


def test_internal_dependencies(calculator_analysis):
    # Callees are recorded by QUALIFIED name: the nested ``clamp`` helper
    # lives at ``Calculator._run_nested.clamp``. Qualified names keep the
    # graph unambiguous when different scopes define same-named functions.
    assert calculator_analysis.module.dependencies == {
        "calculate_total": ["calculate_tax", "validate_amount"],
        "Calculator._run_nested": ["Calculator._run_nested.clamp"],
        "main": ["Calculator.add", "calculate_total"],
    }


def test_same_simple_name_in_two_scopes_stays_distinct():
    """Name-based call resolution over-approximates but never conflates identities.

    A call to ``helper()`` matches BOTH definitions (documented
    over-approximation in the README), yet each callee is recorded under
    its own qualified name -- a simple-name graph could not tell them apart.
    """
    source = (
        "def outer_a():\n"
        "    def helper():\n"
        "        return 1\n"
        "    return helper()\n"
        "\n"
        "def outer_b():\n"
        "    def helper():\n"
        "        return 2\n"
        "    return helper()\n"
    )
    deps = analyze_source(source).module.dependencies
    assert deps == {
        "outer_a": ["outer_a.helper", "outer_b.helper"],
        "outer_b": ["outer_a.helper", "outer_b.helper"],
    }


def test_module_docstring(calculator_analysis):
    assert calculator_analysis.module.docstring.startswith("Sample legacy calculator")


def test_max_nesting_depth_module_level(calculator_analysis):
    assert calculator_analysis.module.max_nesting_depth == 2


# -- focused inline-source tests ------------------------------------------------


def test_syntax_error_carries_location():
    with pytest.raises(SourceParseError) as excinfo:
        analyze_source("def broken(:\n    pass\n", filename="broken.py")
    error = excinfo.value
    assert error.filename == "broken.py"
    assert error.lineno == 1
    assert "broken.py:1" in str(error)


def test_empty_source():
    result = analyze_source("", filename="empty.py")
    assert result.module.functions == []
    assert result.module.classes == []
    assert result.module.imports == []
    assert result.module.dependencies == {}
    assert result.module.max_nesting_depth == 0


def test_parameter_kinds():
    source = "def f(a, b=1, *args, c, d=2, **kw):\n    return a\n"
    params = analyze_source(source).module.functions[0].params
    assert [(p.name, p.kind, p.default) for p in params] == [
        ("a", "positional", None),
        ("b", "positional", "1"),
        ("args", "vararg", None),
        ("c", "kwonly", None),
        ("d", "kwonly", "2"),
        ("kw", "kwarg", None),
    ]


def test_relative_and_aliased_imports():
    source = (
        "from . import utils\n"
        "import numpy as np\n"
        "from ..pkg.helpers import tool as t\n"
    )
    first, second, third = analyze_source(source).module.imports
    assert first.module is None and first.level == 1
    assert first.bound_names == ("utils",)
    assert first.statement == "from . import utils"
    assert second.module == "numpy"
    assert second.bound_names == ("np",)
    assert third.module == "pkg.helpers" and third.level == 2
    assert third.statement == "from ..pkg.helpers import tool as t"


def test_async_functions():
    source = (
        "import asyncio\n"
        "\n"
        "async def fetch_all(urls):\n"
        "    results = []\n"
        "    async for chunk in urls:\n"
        "        results.append(chunk)\n"
        "    return results\n"
        "\n"
        "async def main():\n"
        "    await fetch_all([])\n"
    )
    functions = {
        fn.qualified_name: fn for fn in analyze_source(source).module.functions
    }
    fetch_all = functions["fetch_all"]
    assert fetch_all.is_async is True
    assert [(loop.kind, loop.lineno) for loop in fetch_all.loops] == [("async-for", 5)]
    assert fetch_all.variables == ("results", "chunk")
    assert [call.name for call in fetch_all.calls] == ["results.append"]
    assert functions["main"].is_async is True
    assert [call.name for call in functions["main"].calls] == ["fetch_all"]


def test_classes_inline():
    source = (
        "class Base:\n"
        "    pass\n"
        "\n"
        "class Service(Base):\n"
        '    """Service with class-level config."""\n'
        "\n"
        "    retries: int = 3\n"
        '    label = "svc"\n'
        "\n"
        "    def run(self):\n"
        "        return self.label\n"
        "\n"
        "    @staticmethod\n"
        "    def helper():\n"
        "        return Service.label\n"
    )
    result = analyze_source(source)
    base, service = result.module.classes
    assert base.bases == ()
    assert base.docstring is None
    assert service.bases == ("Base",)
    assert service.methods == ("run", "helper")
    assert service.class_variables == ("retries", "label")
    functions = {fn.qualified_name: fn for fn in result.module.functions}
    assert functions["Service.run"].is_method is True
    assert functions["Service.helper"].decorators == ("staticmethod",)
    assert functions["Service.helper"].returns[0].value == "Service.label"


def test_bare_except_and_reraise():
    source = (
        "def f(x):\n"
        "    try:\n"
        "        return g(x)\n"
        "    except:\n"
        "        raise\n"
    )
    fn = analyze_source(source).module.functions[0]
    assert fn.exception_handlers[0].exception_types == ()
    assert fn.raises == ("<re-raise>",)


def test_recursion_is_recorded():
    source = (
        "def fact(n):\n"
        "    if n <= 1:\n"
        "        return 1\n"
        "    return n * fact(n - 1)\n"
    )
    assert analyze_source(source).module.dependencies == {"fact": ["fact"]}


def test_lambda_is_not_a_function_and_binds_variable():
    source = "def f(x):\n    g = lambda y: y + 1\n    return g(x)\n"
    result = analyze_source(source)
    assert [fn.name for fn in result.module.functions] == ["f"]
    assert result.module.functions[0].variables == ("g",)


def test_with_statement_binds_names():
    source = (
        "def read(path):\n"
        "    with open(path) as handle, open(path) as other:\n"
        "        return handle.read()\n"
    )
    fn = analyze_source(source).module.functions[0]
    assert fn.variables == ("handle", "other")
    assert [call.name for call in fn.calls] == ["open", "open", "handle.read"]