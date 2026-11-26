"""The attachment budget: what a turn actually carries.

Attachments ride along with every message in the conversation, which makes them
the one input the user controls that grows without the agent asking. A per-file
cap is not a budget — three 120k files is ~90k tokens on turn one and again on
turn twenty. These pin the shared ceiling, and the part that matters more than
the arithmetic: what does NOT fit is named, because a silent drop reads as "it
saw my file".
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_server as A  # noqa: E402


def test_a_small_set_is_carried_whole():
    files = [{"path": "a.py", "content": "print(1)\n"},
             {"path": "b.py", "content": "print(2)\n"}]
    out = A._attachment_lines(files)
    joined = "\n".join(out)
    assert "ATTACHED FILE: a.py" in joined
    assert "ATTACHED FILE: b.py" in joined
    assert "trimmed" not in joined
    assert "not included" not in joined


def test_the_shared_budget_stops_the_set_not_just_one_file():
    big = "x" * 100_000
    out = A._attachment_lines([{"path": f"f{i}.py", "content": big}
                               for i in range(5)])
    joined = "\n".join(out)
    # The ceiling is shared: five files that fit individually cannot all fit.
    carried = joined.count("x" * 1000)  # rough presence check only
    assert carried > 0
    assert "not included" in joined, "the file left out must be named"
    # Whatever ran out is reported per file, with the size, not swallowed.
    assert "attachment budget for this turn is spent" in joined


def test_a_trimmed_file_says_how_much_was_cut():
    files = [{"path": "big.py", "content": "y" * 300_000}]
    out = "\n".join(A._attachment_lines(files, budget=200_000))
    assert "trimmed from 300000 chars" in out
    assert "read_file" in out


def test_a_missing_content_key_is_skipped_without_a_block():
    out = A._attachment_lines([{"path": "a.py"}, {"path": "b.py", "content": "z"}])
    joined = "\n".join(out)
    assert "ATTACHED FILE: a.py" not in joined
    assert "ATTACHED FILE: b.py" in joined


def test_the_budget_is_a_real_ceiling():
    files = [{"path": f"f{i}.py", "content": "q" * 90_000} for i in range(4)]
    out = A._attachment_lines(files, budget=200_000)
    carried = sum(block.count("q") for block in out)
    assert carried <= 200_000


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))