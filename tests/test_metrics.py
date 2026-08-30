"""Tests for backend.analyzer.metrics."""
from __future__ import annotations

import pytest

from backend.analyzer import analyze_source

LOC_SOURCE = (
    "#!/usr/bin/env python\n"           # comment-only line
    "import os  # trailing comment\n"   # code line
    "\n"                                 # blank
    "def main():\n"
    '    """Docstring."""\n'            # docstrings count as code
    "    x = 1  # trailing\n"
    "    return x\n"
    "\n"
    "# full comment\n"
    "# another\n"
)


def test_line_categories():
    m = analyze_source(LOC_SOURCE, filename="loc.py").metrics
    assert m.total_lines == 10
    assert m.blank_lines == 2
    assert m.comment_lines == 3
    assert m.code_lines == 5


def test_counts_on_small_module():
    m = analyze_source(LOC_SOURCE, filename="loc.py").metrics
    assert m.num_functions == 1
    assert m.num_methods == 0
    assert m.num_classes == 0
    assert m.num_imports == 1
    assert m.max_function_length == 4
    assert m.average_function_length == 4.0
    assert m.longest_function == "main"
    assert m.max_nesting_depth == 0


def test_metrics_on_calculator_fixture(calculator_analysis):
    m = calculator_analysis.metrics
    assert m.total_lines == 75
    assert m.blank_lines == 19
    assert m.comment_lines == 0
    assert m.code_lines == 56
    assert m.num_functions == 8
    assert m.num_methods == 3
    assert m.num_classes == 1
    assert m.num_imports == 2
    assert m.max_nesting_depth == 2
    assert m.max_function_length == 15
    assert m.longest_function == "calculate_total"
    assert m.average_function_length == pytest.approx(5.875, abs=0.01)


def test_empty_source_metrics():
    m = analyze_source("", filename="empty.py").metrics
    assert m.total_lines == 0
    assert m.code_lines == 0
    assert m.num_functions == 0
    assert m.max_function_length == 0
    assert m.average_function_length == 0.0
    assert m.longest_function is None