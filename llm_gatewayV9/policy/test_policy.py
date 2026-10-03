"""Fundamental suite for the declarative policy engine (offline, no servers).

Pure logic: first matching rule wins, default-deny, dry-run reporting,
spend caps. Also pins the shipped policy.yaml loads and hot-reloads.

Run: uv run pytest policy -q
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from policy.engine import (  # noqa: E402
    PolicyEngine,
    _match,
    get_engine,
    reload_engine,
    spend_over_cap,
)


def _eng(rules, dry_run=True, default="deny", caps=None):
    return PolicyEngine({"defaults": {"dry_run": dry_run,
                                      "default_action": default},
                         "rules": rules,
                         "spend_caps_usd": caps or {}})


# ── matcher ──────────────────────────────────────────────────────────────

class TestMatch:
    def test_scalar_and_list(self):
        assert _match({"trust": "owner"}, {"trust": "owner", "x": 1})
        assert not _match({"trust": "owner"}, {"trust": "paired"})
        assert _match({"tool": ["a", "b"]}, {"tool": "b"})
        assert not _match({"tool": ["a"]}, {"tool": "z"})

    def test_nested_and_empty(self):
        assert _match({}, {"anything": 1})
        assert _match({"a": {"b": 1}}, {"a": {"b": 1, "c": 2}})
        assert not _match({"a": {"b": 1}}, {"a": "nope"})


# ── evaluation ───────────────────────────────────────────────────────────

class TestEvaluate:
    def test_first_match_wins(self):
        eng = _eng([{"id": "r1", "when": {"trust": "owner"},
                     "action": "allow", "enforce": True},
                    {"id": "r2", "when": {"trust": "owner"},
                     "action": "deny", "enforce": True}])
        v = eng.evaluate({"trust": "owner"})
        assert (v.allowed, v.action, v.rule_id) == (True, "allow", "r1")

    def test_default_deny_when_armed(self):
        eng = _eng([], dry_run=False)
        v = eng.evaluate({"trust": "untrusted", "tool": "x"})
        assert (v.allowed, v.action, v.dry_run) == (False, "deny", False)

    def test_dry_run_reports_but_allows(self):
        eng = _eng([{"id": "r1", "when": {"trust": "untrusted"},
                     "action": "deny"}], dry_run=True)
        v = eng.evaluate({"trust": "untrusted"})
        assert v.allowed is True and v.dry_run is True
        assert v.action == "deny" and v.rule_id == "r1"

    def test_enforce_flag_arms_single_rule(self):
        eng = _eng([{"id": "r1", "when": {"trust": "untrusted"},
                     "action": "deny", "enforce": True}], dry_run=True)
        v = eng.evaluate({"trust": "untrusted"})
        assert (v.allowed, v.dry_run) == (False, False)

    def test_bad_action_clamps_to_deny(self):
        eng = _eng([{"id": "r1", "when": {}, "action": "explode",
                     "enforce": True}])
        assert eng.evaluate({}).action == "deny"

    def test_approve_action(self):
        eng = _eng([{"id": "r1", "when": {"tool": "shell"},
                     "action": "approve", "enforce": True}])
        v = eng.evaluate({"tool": "shell"})
        assert (v.allowed, v.action) == (False, "approve")


# ── spend caps ───────────────────────────────────────────────────────────

class TestSpend:
    def test_over_cap(self, monkeypatch):
        import policy.engine as E

        monkeypatch.setattr(
            E, "get_engine",
            lambda: PolicyEngine(
                {"spend_caps_usd": {"per_call": 0.05,
                                    "per_agent_per_day": 1.0,
                                    "per_session_per_day": 0.5}}))
        assert spend_over_cap(per_call_usd=0.06) is True
        assert spend_over_cap(per_call_usd=0.01) is False
        assert spend_over_cap(agent_day_usd=2.0) is True
        assert spend_over_cap(session_day_usd=0.6) is True


# ── shipped policy file ──────────────────────────────────────────────────

class TestShippedPolicy:
    def test_loads_and_evaluates(self):
        eng = reload_engine()
        assert isinstance(eng.rules, list) and eng.rules
        v = eng.evaluate({"trust": "owner", "tool": "anything"})
        assert isinstance(v.allowed, bool) and v.dry_run is True

    def test_untrusted_never_allowed_when_armed(self):
        eng = get_engine()
        # Flip to armed on a COPY (never mutates the shipped singleton).
        armed = PolicyEngine({"defaults": {"dry_run": False,
                                            "default_action": "deny"},
                              "rules": eng.rules})
        v = armed.evaluate({"trust": "untrusted", "tool": "nope-tool-xyz"})
        assert v.allowed is False
