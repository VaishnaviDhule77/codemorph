"""Tests for the composition layer and the CLI entry point."""
from __future__ import annotations

import json

from backend.analyzer.__main__ import main


def test_analyze_source_end_to_end(calculator_analysis):
    assert calculator_analysis.filename == "calculator.py"
    assert isinstance(calculator_analysis.structure, str)
    assert "Module: calculator.py" in calculator_analysis.structure


def test_to_dict_is_json_serializable(calculator_analysis):
    data = json.loads(json.dumps(calculator_analysis.to_dict()))
    assert data["metrics"]["num_functions"] == 8
    assert data["module"]["dependencies"]["main"] == [
        "Calculator.add", "calculate_total",
    ]


def test_structure_rendering(calculator_analysis):
    structure = calculator_analysis.structure
    assert "Imports (2)" in structure
    assert "from typing import List, Optional  [line 9]" in structure
    assert "Functions (8)" in structure
    assert "Function: Calculator.add [method]" in structure
    assert "Function: Calculator._run_nested.clamp [nested]" in structure
    assert "main → Calculator.add, calculate_total" in structure
    assert "Internal dependencies (3)" in structure


def test_cli_text_report(tmp_path, capsys):
    sample = tmp_path / "sample.py"
    sample.write_text("def double(x):\n    return x * 2\n", encoding="utf-8")
    assert main([str(sample)]) == 0
    out = capsys.readouterr().out
    assert "Module: sample.py" in out
    assert "double" in out
    assert "Metrics" in out


def test_cli_json_report(tmp_path, capsys):
    sample = tmp_path / "sample.py"
    sample.write_text("x = 1\n", encoding="utf-8")
    assert main([str(sample), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["metrics"]["total_lines"] == 1


def test_cli_reports_syntax_errors(tmp_path, capsys):
    broken = tmp_path / "broken.py"
    broken.write_text("def broken(:\n", encoding="utf-8")
    assert main([str(broken)]) == 1
    assert "error:" in capsys.readouterr().err


def test_cli_missing_file(tmp_path, capsys):
    assert main([str(tmp_path / "nope.py")]) == 2
    assert "error:" in capsys.readouterr().err