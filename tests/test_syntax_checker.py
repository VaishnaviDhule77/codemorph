"""Tests for backend.verification.syntax_checker."""
from __future__ import annotations

import json

from backend.verification import check_syntax


def test_valid_source():
    result = check_syntax("def f(x):\n    return x + 1\n", filename="ok.py")
    assert result.valid is True
    assert result.error_line is None
    assert result.error_message is None


def test_syntax_error_reports_location():
    result = check_syntax("def broken(:\n    pass\n", filename="bad.py")
    assert result.valid is False
    assert result.filename == "bad.py"
    assert result.error_line == 1
    assert result.error_offset is not None
    assert result.error_message


def test_compile_catches_what_parse_misses():
    # ast.parse accepts a misplaced __future__ import; compile() rejects it.
    # The syntax gate must use compile(): generated code that parses but
    # fails at import time is still caught.
    source = "import os\nfrom __future__ import annotations\n"
    result = check_syntax(source, filename="future.py")
    assert result.valid is False
    assert "future" in (result.error_message or "").lower()


def test_to_dict_serialization():
    result = check_syntax("x = = 1\n", filename="s.py")
    data = json.loads(json.dumps(result.to_dict()))
    assert data["valid"] is False
    assert data["filename"] == "s.py"
    assert data["error_line"] == 1
    assert data["error_message"]