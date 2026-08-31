"""Fixture exercising every Phase-2 analyzer rule.

Every rule fires at least once; tests assert the exact findings.
"""

import json
import os
import sys
from math import pi, sqrt

DEBUG = True


def circle_area(radius):
    """Use ``pi`` so the ``math`` import stays partially used."""
    return pi * radius * radius


def load_config(path):
    """Read JSON from disk without any error handling."""
    handle = open(path)
    data = json.loads(handle.read())
    handle.close()
    return data


def parse_count(raw):
    """Convert input to an int, guarded by a try block."""
    try:
        return int(raw)
    except ValueError:
        return 0


def bump(total):
    """Three identical consecutive statements."""
    total = total + 1
    total = total + 1
    total = total + 1
    return total


def run_snippet(source):
    """Dangerous constructs live here."""
    code = eval(source)
    exec(code)
    return code


def nested_scan(matrix):
    """Deeply nested loops and conditionals."""
    found = 0
    for row in matrix:
        for cell in row:
            if cell:
                if cell > 0:
                    found += 1
    return found


def classify(value):
    """Monolithic legacy dispatcher for size and complexity rules."""
    label = "unknown"
    if value == 0:
        label = "zero"
    elif value == 1:
        label = "one"
    elif value == 2:
        label = "two"
    elif value == 3:
        label = "three"
    elif value == 4:
        label = "four"
    elif value == 5:
        label = "five"
    elif value == 6:
        label = "six"
    elif value == 7:
        label = "seven"
    elif value == 8:
        label = "eight"
    elif value == 9:
        label = "nine"
    elif value == 10:
        label = "ten"
    elif value == 11:
        label = "eleven"
    elif value == 12:
        label = "twelve"
    elif value == 13:
        label = "thirteen"
    elif value == 14:
        label = "fourteen"
    elif value == 15:
        label = "fifteen"
    elif value < 0:
        label = "negative"
    else:
        label = "large"
    parts = label.split("-")
    upper = [part.upper() for part in parts]
    joined = "-".join(upper)
    trimmed = joined[:20]
    safe = trimmed.replace(" ", "_")
    padded = safe.ljust(24, ".")
    suffix = ""
    if len(padded) > 20:
        suffix = "!"
    elif len(padded) > 10:
        suffix = "?"
    display = padded + suffix
    if display.isupper():
        display = display.title()
    return display


def risky_cleanup(path):
    """Unhandled file operation plus unused locals."""
    target = open(path)
    target.close()
    cache = {}
    leftovers = []
    for leftover in leftovers:
        pass
    try:
        pass
    except Exception as err:
        pass


def swallow_errors(block):
    """A bare except swallows everything."""
    try:
        return block()
    except:
        return None