"""Shared fixtures for the CodeMorph test suite."""
from __future__ import annotations

import pathlib

import pytest

from backend.analyzer import analyze_source

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture()
def calculator_source() -> str:
    return (FIXTURES_DIR / "calculator.py").read_text(encoding="utf-8")


@pytest.fixture()
def calculator_analysis(calculator_source: str):
    return analyze_source(calculator_source, filename="calculator.py")