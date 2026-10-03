"""Capability-aware router. Same RPM/RPD bookkeeping as V1, but now it can
skip providers that lack a requested capability (tools/reasoning/structured/caching)."""
from __future__ import annotations
import time
from collections import deque, defaultdict


LIMITS = {
    "ollama":     {"rpm": 9999, "rpd": 9999999, "tpm": 99999999, "cooldown": 0,   "max_ctx": 32000},
    "cerebras":   {"rpm": 30,   "rpd": 9999,    "tpm": 60000,    "cooldown": 2,   "max_ctx": 8000,    "tokens_per_day": 1_000_000},
    "groq":       {"rpm": 30,   "rpd": 1000,    "tpm": 6000,     "cooldown": 2,   "max_ctx": 100000},
    "nvidia":     {"rpm": 40,   "rpd": 9999,    "tpm": 100000,   "cooldown": 2,   "max_ctx": 100000},
    "gemini":     {"rpm": 15,   "rpd": 1000,    "tpm": 250000,   "cooldown": 4,   "max_ctx": 1000000},
    "gemini35lite": {"rpm": 15, "rpd": 1000,    "tpm": 250000,   "cooldown": 4,   "max_ctx": 1000000},
    "openrouter": {"rpm": 20,   "rpd": 50,      "tpm": 99999999, "cooldown": 3,   "max_ctx": 100000},
    "github":     {"rpm": 10,   "rpd": 50,      "tpm": 99999999, "cooldown": 6,   "max_ctx": 8000},
    "kilo":       {"rpm": 60,   "rpd": 9999,    "tpm": 99999999, "cooldown": 1,   "max_ctx": 262144},
}

SHORTCUTS = {
    "g": "gemini", "gem": "gemini", "gemini": "gemini",
    "g35l": "gemini35lite", "gem35l": "gemini35lite", "gemini35lite": "gemini35lite",
    "n": "nvidia", "nv": "nvidia", "nvidia": "nvidia",
    "o": "ollama", "oll": "ollama", "ollama": "ollama",
    "gr": "groq", "groq": "groq",
    "c": "cerebras", "cer": "cerebras", "cerebras": "cerebras",
    "or": "openrouter", "opr": "openrouter", "openrouter": "openrouter",
    "gh": "github", "ghb": "github", "github": "github",
    "k": "kilo", "kilo": "kilo",
}


def resolve(name):
    if not name:
        return None
    return SHORTCUTS.get(name.lower())


def limits_for(name):
    """Rate limits for a pool member. Key-pool siblings (`gemini-2`, …)
    share their canonical provider's limits; unknown names raise KeyError
    just like a direct LIMITS lookup would."""
    if name in LIMITS:
        return LIMITS[name]
    import re as _re
    m = _re.fullmatch(r"(.+)-(\d+)", name or "")
    if m and m.group(1) in LIMITS:
        return LIMITS[m.group(1)]
    raise KeyError(name)


def expand_order(order, providers):
    """Insert key-pool siblings right after their canonical member:
    order [gemini, groq] with members {gemini, gemini-2, groq} →
    [gemini, gemini-2, groq]. Unknown names pass through (Router filters
    them against the live pool), order otherwise preserved."""
    expanded = []
    for name in order:
        expanded.append(name)
        i = 2
        while f"{name}-{i}" in providers:
            expanded.append(f"{name}-{i}")
            i += 1
    return expanded


class RateState:
    def __init__(self):
        self.calls_minute = deque()
        self.tokens_minute = deque()
        self.calls_today = 0
        self.tokens_today = 0
        self.day_start = self._day_start()
        self.last_call = 0.0
        self.unavailable_until = 0.0
        self.unavailable_reason = ""

    @staticmethod
    def _day_start():
        now = time.time()
        return now - (now % 86400)

    def gc(self):
        now = time.time()
        if now - self.day_start >= 86400:
            self.calls_today = 0
            self.tokens_today = 0
            self.day_start = self._day_start()
        cutoff = now - 60
        while self.calls_minute and self.calls_minute[0] < cutoff:
            self.calls_minute.popleft()
        while self.tokens_minute and self.tokens_minute[0][0] < cutoff:
            self.tokens_minute.popleft()

    def can_use(self, limits, est_tokens=0):
        self.gc()
        now = time.time()
        if now < self.unavailable_until:
            return False, f"backoff: {self.unavailable_reason} ({self.unavailable_until - now:.0f}s left)"
        wait = limits["cooldown"] - (now - self.last_call)
        if wait > 0:
            return False, f"cooldown ({wait:.1f}s)"
        if len(self.calls_minute) >= limits["rpm"]:
            return False, "RPM limit"
        if self.calls_today >= limits["rpd"]:
            return False, "RPD limit"
        tpm = sum(t for _, t in self.tokens_minute)
        if tpm + est_tokens > limits["tpm"]:
            return False, "TPM limit"
        if "tokens_per_day" in limits and self.tokens_today + est_tokens > limits["tokens_per_day"]:
            return False, "daily token cap"
        return True, None

    def record(self, tokens):
        now = time.time()
        self.calls_minute.append(now)
        self.tokens_minute.append((now, tokens))
        self.calls_today += 1
        self.tokens_today += tokens
        self.last_call = now

    def mark_unavailable(self, seconds: float, reason: str):
        self.unavailable_until = time.time() + seconds
        self.unavailable_reason = reason

    def snapshot(self, limits):
        self.gc()
        now = time.time()
        tpm = sum(t for _, t in self.tokens_minute)
        return {
            "rpm_used": len(self.calls_minute),
            "rpm_limit": limits["rpm"],
            "rpd_used": self.calls_today,
            "rpd_limit": limits["rpd"],
            "tpm_used": tpm,
            "tpm_limit": limits["tpm"],
            "tokens_today": self.tokens_today,
            "tokens_per_day": limits.get("tokens_per_day"),
            "cooldown_remaining": max(0, limits["cooldown"] - (now - self.last_call)) if self.last_call else 0,
            "last_call": self.last_call,
            "backoff_remaining": max(0, self.unavailable_until - now),
            "backoff_reason": self.unavailable_reason if now < self.unavailable_until else "",
        }


class Router:
    def __init__(self, providers: dict, order: list[str]):
        self.providers = providers
        self.order = [p for p in order if p in providers]
        self.state = defaultdict(RateState)

    def candidates(self, override=None):
        if override:
            r = resolve(override)
            if r and r in self.providers:
                return [r]
            # Direct pool-member pin, incl. key-pool siblings (gemini-2, …).
            if override in self.providers:
                return [override]
            return []
        return list(self.order)

    def pick(self, est_tokens, candidates, required_caps: list[str] | None = None):
        """Choose the least-recently-loaded usable candidate (fewest calls
        in the last minute, then longest idle) instead of strict priority
        order — concurrent calls spread across providers in parallel
        rather than queueing on provider #1 until it cools down.
        Capability, max_ctx and RPM/RPD/TPM guards are unchanged; error
        failover in main.py stays sequential (racing would pay N× cost)."""
        attempts = []
        usable = []
        for name in candidates:
            limits = limits_for(name)
            prov = self.providers[name]
            caps = getattr(prov, "capabilities", {})
            if required_caps:
                missing = [c for c in required_caps if not caps.get(c)]
                if missing:
                    attempts.append({"provider": name, "reason": f"skipped:no_{missing[0]}"})
                    continue
            if est_tokens > limits["max_ctx"]:
                attempts.append({"provider": name, "reason": f"prompt {est_tokens} > max_ctx {limits['max_ctx']}"})
                continue
            ok, why = self.state[name].can_use(limits, est_tokens)
            if ok:
                usable.append(name)
            else:
                attempts.append({"provider": name, "reason": why})
        if not usable:
            return None, attempts
        best = min(usable, key=lambda n: (len(self.state[n].calls_minute),
                                          self.state[n].last_call))
        return best, attempts

    def all_status(self):
        out = {}
        for name in self.providers:
            out[name] = self.state[name].snapshot(limits_for(name))
            out[name]["model"] = self.providers[name].model
            out[name]["capabilities"] = getattr(self.providers[name], "capabilities", {})
        return out


# -----------------------------------------------------------------------------
# V3 Router pool — separate failover ring for routing-decision LLM calls.
# Same rate-state machinery, separate state dict so router quotas never compete
# with worker quotas (provider keys are shared but providers meter per-model).
# -----------------------------------------------------------------------------

# NOTE: "kilo" intentionally absent — build_router_providers() wires only
# cerebras/groq/nvidia/github (per-model metered keys), so any name here
# without a builder is silently filtered by the Router constructor.
DEFAULT_ROUTER_ORDER = ["cerebras", "groq", "nvidia", "github"]


class RouterPool:
    """Failover ring for router-LLM calls. Mirrors `Router` but for the
    Perception/Memory/Decision routing classifiers. Each call is logged with
    a call_role marker (router_perception | router_memory | router_decision)
    so the dashboard can show router activity separately from worker activity.
    """
    def __init__(self, providers: dict, order: list[str]):
        self.providers = providers
        self.order = [p for p in order if p in providers]
        self.state = defaultdict(RateState)

    def candidates(self):
        return list(self.order)

    def all_status(self):
        out = {}
        for name in self.providers:
            out[name] = self.state[name].snapshot(limits_for(name))
            out[name]["model"] = self.providers[name].model
        return out
