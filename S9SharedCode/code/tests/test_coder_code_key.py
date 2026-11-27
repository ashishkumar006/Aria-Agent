"""The coder's code must survive a renamed key.

Live failure (session s8-d7013102, "write a Python script that reads a CSV…"):
the coder returned its script under `analyze_csv_py` (and later `script`), never
`code`. The node was marked complete (valid JSON), but `sandbox_executor` reads
only `output["code"]`, so it failed with "no code in upstream coder output" three
times and the run re-planned around a failure that was really a key mismatch.

`_coder_code_from_output` maps any code-ish key back to `code` so a recoverable
shape never becomes a hard failure.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import skills  # noqa: E402


def test_known_aliases_are_accepted():
    assert skills._coder_code_from_output({"code": "print(1)"}) == "print(1)"
    assert skills._coder_code_from_output({"script": "print(2)"}) == "print(2)"
    assert skills._coder_code_from_output(
        {"script_content": "print(3)"}) == "print(3)"


def test_task_named_py_key_is_accepted():
    # The exact live shape: {"description", "data_csv", "analyze_csv_py", "output"}
    out = {"description": "analyze a csv", "data_csv": "a,b\n1,2",
           "analyze_csv_py": "import csv\nprint('hi')", "output": ""}
    assert skills._coder_code_from_output(out) == "import csv\nprint('hi')"


def test_longest_string_fallback():
    out = {"description": "short", "weird_key": "x = 1\n" * 50}
    assert skills._coder_code_from_output(out).startswith("x = 1")


def test_empty_or_non_dict_is_safe():
    assert skills._coder_code_from_output({}) == ""
    assert skills._coder_code_from_output(None) == ""
    assert skills._coder_code_from_output({"code": "   "}) == ""


def test_code_key_wins_over_a_longer_description():
    out = {"code": "print(1)", "description": "a much longer description " * 10}
    assert skills._coder_code_from_output(out) == "print(1)"