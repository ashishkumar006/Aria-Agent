"""Reasoning that arrives as the answer is a parser problem, not a prompt one.

Some providers stream their thinking in `content`. The run then ends with a
paragraph of deliberation where the answer should be, and no amount of prompt
wording fixes a channel the provider chose. These pin the detection.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mcp_runner as mr  # noqa: E402


@pytest.mark.parametrize("text", [
    "We need to build project. Need inspect environment. We have empty app only? "
    "User says empty folder but app exists. Need create ledger package etc. Need "
    "understand expected hidden tests likely. Need implement robustly. We need to "
    "be careful about the money type and about SQLite transactions, and about what "
    "the CLI prints, so let me look at the folder before writing anything.",
    "Let me look at the file first and then decide what to change. The module has "
    "three functions and a class, and the caller in cli.py uses two of them, so a "
    "rename would touch both. I should check the tests before I edit anything, and "
    "then run the checks to see whether the change holds.",
    "The user wants me to explore the project in read-only mode and explain how the "
    "integration authenticates. I should gather the files first: the server module, "
    "the auth helper, and the bridge. Then I need to trace the path from an MCP tool "
    "call to the HTTP request, and say which layer owns each step.",
    # The live shape: no opener, 32k characters, one code fence.
    "So ruff is a separate executable, not a module. run_checks probably runs "
    "ruff. Let me check what run_checks does. Let me think about the design of "
    "the scanner: tokens with line and column, then the parser, then the "
    "evaluator. I need to be careful about positions in error messages, and "
    "maybe about the pipe semantics. We need to handle select() and //, and "
    "probably also the strict mode. Let me plan the modules first.",
])
def test_deliberation_is_detected(text):
    assert mr._looks_like_reasoning(text) is True


def test_a_long_coding_answer_is_treated_as_reasoning():
    """A coding agent's reply is a summary; 30k characters is deliberation.

    Live: 32,783 characters, one code fence, returned as the answer.
    """
    wall = ("So ruff is a separate executable, not a module. Let me think about "
            "the design. " + "I need to consider the scanner and the parser. " * 200)
    assert mr._looks_like_reasoning(wall, short_answer_guard=True) is True


def test_a_long_document_is_still_a_document_without_the_guard():
    """The authoring path legitimately returns long text; only the studio opts in."""
    doc = "# Design\n\n" + ("This section describes the storage layer in detail. " * 200)
    assert mr._looks_like_reasoning(doc, short_answer_guard=False) is False


@pytest.mark.parametrize("text", [
    "Created `ledger/store.py` with SQLite persistence and versioned migrations.",
    "I fixed the bug in src/app.ts(41,2) — the null check was inverted.",
    "short answer",
    "Here is the change:\n```python\ndef f():\n    return 1\n```",
])
def test_real_answers_are_left_alone(text):
    assert mr._looks_like_reasoning(text) is False