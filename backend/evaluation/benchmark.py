"""Benchmark tasks for the evaluation framework (Phase 10).

Eight small, self-contained Python modernization tasks. Every task is
valid, runnable Python 3 with at least one testable module-level
function, so differential testing (Phase 5) is always meaningful: the
tasks contain *legacy style*, not legacy runtime behavior.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BenchmarkTask:
    """One benchmark task: a name, what it exercises, and the source."""

    name: str
    description: str
    source: str


BENCHMARK_TASKS: tuple = (
    BenchmarkTask(
        name="percent_format",
        description="%-formatting should become f-strings",
        source='''def greet(name):
    return 'Hello %s!' % (name,)


def banner(text, count):
    return '%s: %d items' % (text, count)
''',
    ),
    BenchmarkTask(
        name="format_call",
        description="str.format() calls should become f-strings",
        source='''def label(item, count):
    return '{} ({})'.format(item, count)


def status(code):
    return 'status={}'.format(code)
''',
    ),
    BenchmarkTask(
        name="bare_except",
        description="bare except clauses should catch Exception",
        source='''def parse_int(text):
    try:
        return int(text)
    except:
        return 0
''',
    ),
    BenchmarkTask(
        name="unused_imports",
        description="unused imports should be removed",
        source='''import os
import sys
import json


def load(data):
    return json.loads(data)
''',
    ),
    BenchmarkTask(
        name="string_concat",
        description="string accumulation in a loop should use join",
        source='''def sentence(words):
    result = ''
    for word in words:
        result = result + word + ' '
    return result.strip()
''',
    ),
    BenchmarkTask(
        name="index_loop",
        description="range(len(...)) indexing should iterate directly",
        source='''def total(values):
    sum_value = 0
    for i in range(len(values)):
        sum_value = sum_value + values[i]
    return sum_value
''',
    ),
    BenchmarkTask(
        name="type_checks",
        description="type(x) == T comparisons should use isinstance",
        source='''def classify(value):
    if type(value) == int:
        return 'int'
    if type(value) == str:
        return 'str'
    return 'other'
''',
    ),
    BenchmarkTask(
        name="manual_increment",
        description="x = x + n assignments should use augmented assignment",
        source='''def bump(counter):
    counter = counter + 1
    counter = counter + 1
    counter = counter + 1
    return counter
''',
    ),
)


def get_task(name: str) -> BenchmarkTask:
    """Look up a benchmark task by name."""
    for task in BENCHMARK_TASKS:
        if task.name == name:
            return task
    known = ", ".join(t.name for t in BENCHMARK_TASKS)
    raise KeyError(f"unknown benchmark task '{name}'; known tasks: {known}")