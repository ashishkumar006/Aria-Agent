r"""A node's work must not be thrown away by a window the gateway could have read.

Two live failures, both avoidable:

1. `kilo HTTP 400: This endpoint's maximum context length is 256000 tokens.
   However, you requested about 259371 tokens (194119 of text input, 1252 of
   tool input, 64000 in the output)`. `max_ctx` has been in router.LIMITS since
   it was written and nothing read it — router.py's own docstring still claims
   "the context-window checks all still apply". So a researcher that had spent
   minutes gathering evidence sent a prompt + reservation over the window, got
   a 400, and lost the work.
2. `503 all providers unavailable. attempts: [... 'backoff: RPM quota burned
   (39s left)' ...]` — every worker blocked, the soonest slot 39s away, and the
   ladder gave up immediately. A wide fan-out burns the whole pool's RPM in one
   wave; failing there discards the node and hands the recovery planner the
   same hop to redo, which burns the quota again.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
import router as R  # noqa: E402


def _msgs(text_len):
    return [{"role": "user", "content": "x" * text_len}]


# ── the estimate ─────────────────────────────────────────────────────────────

def test_tool_definitions_count_as_input():
    """Upstream's own breakdown lists "1252 of tool input" separately."""
    tools = [{"type": "function", "function": {"name": "run_command",
                                               "description": "d" * 4000}}]
    without = main._est_input_tokens(_msgs(4000), None)
    with_tools = main._est_input_tokens(_msgs(4000), None, tools)
    assert with_tools > without + 900, (without, with_tools)


def test_est_tokens_is_input_plus_the_reservation():
    est = main._est_tokens(_msgs(4000), None, 65536)
    assert est == main._est_input_tokens(_msgs(4000), None) + 8192


# ── the clamp ────────────────────────────────────────────────────────────────

def test_the_real_failing_call_now_fits():
    """194119 text + 1252 tool + 65536 output = 259371 against a 256000 window."""
    est_in = (194119 + 1252)
    allowed, why = main._clamp_max_tokens("kilo", est_in, 65536)
    assert allowed is not None and allowed < 65536, (allowed, why)
    assert est_in + allowed <= 256000, (est_in, allowed)


def test_a_prompt_that_cannot_fit_anywhere_is_refused_not_sent():
    """Better a clean skip than a guaranteed 400 that costs the node."""
    allowed, why = main._clamp_max_tokens("kilo", 255900, 65536)
    assert allowed is None
    assert "window" in why


def test_a_small_prompt_keeps_its_full_output_budget():
    assert main._clamp_max_tokens("kilo", 2000, 65536) == (65536, "")


def test_a_wide_window_is_untouched():
    """Gemini's 1M window must not shrink a researcher's answer budget."""
    assert main._clamp_max_tokens("gemini", 195371, 65536) == (65536, "")


def test_unknown_provider_does_not_blow_up_the_ladder():
    allowed, _why = main._clamp_max_tokens("not-a-provider", 500_000, 65536)
    assert allowed == 65536


def test_kilo_window_matches_what_the_endpoint_answers():
    """262144 is the binary round number; the endpoint says 256000. An
    over-stated window is exactly what lets a clamped call 400 anyway."""
    assert R.limits_for("kilo")["max_ctx"] == 256000


def test_every_dispatch_in_the_ladder_uses_the_clamped_value():
    """Structural guard: a new call site that forwards `req.max_tokens` straight
    through would silently re-open the overflow this file exists to close."""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    start = src.index("provider = router.providers[name]")
    end = src.index("all providers unavailable")
    assert "max_tokens=req.max_tokens" not in src[start:end], (
        "a dispatch inside the candidate loop bypasses the clamp")
    assert "max_tokens=_mt" in src[start:end]


# ── the wait instead of the 503 ──────────────────────────────────────────────

class _State:
    def __init__(self, backoff=0.0, cooldown=0.0):
        self._b, self._c = backoff, cooldown

    def snapshot(self, limits):
        return {"backoff_remaining": self._b, "cooldown_remaining": self._c}


class _Router:
    def __init__(self, states):
        self.state = states


def test_soonest_wait_is_the_nearest_slot_not_the_worst():
    r = _Router({"kilo": _State(backoff=39.0), "gemini": _State(backoff=12.0),
                 "gemini-2": _State(backoff=25.0)})
    assert main._soonest_wait(r, ["kilo", "gemini", "gemini-2"]) == 12.0


def test_soonest_wait_is_zero_when_anything_is_free():
    r = _Router({"kilo": _State(backoff=39.0), "gemini": _State()})
    assert main._soonest_wait(r, ["kilo", "gemini"]) == 0.0


def test_a_far_off_slot_is_not_worth_blocking_the_call_for():
    """The ladder waits up to SLOT_WAIT_MAX_S; beyond that it must fail over
    rather than hold the caller's request open for minutes."""
    r = _Router({"kilo": _State(backoff=600.0)})
    assert main._soonest_wait(r, ["kilo"]) > main.SLOT_WAIT_MAX_S


def test_the_ladder_waits_before_declaring_everything_dead():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "waiting {wait:.1f}s for the next slot" in src
    assert "_waits_left = SLOT_WAITS" in src
    # the loop must have room for the waits, or a wait consumes an attempt
    assert "range(len(candidates) + 1 + SLOT_WAITS)" in src
