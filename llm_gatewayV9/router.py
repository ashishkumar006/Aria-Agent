"""Capability-aware router. Same RPM/RPD bookkeeping as V1, but now it can
skip providers that lack a requested capability (tools/reasoning/structured/caching)."""
from __future__ import annotations
import time
from collections import deque, defaultdict, OrderedDict


LIMITS = {
    "ollama":     {"rpm": 9999, "rpd": 9999999, "tpm": 99999999, "cooldown": 0,   "max_ctx": 32000},
    "gemini":     {"rpm": 15,   "rpd": 1000,    "tpm": 250000,   "cooldown": 4,   "max_ctx": 1000000},
    "gemini35lite": {"rpm": 15, "rpd": 1000,    "tpm": 250000,   "cooldown": 4,   "max_ctx": 1000000},
    "kilo":       {"rpm": 60,   "rpd": 9999,    "tpm": 99999999, "cooldown": 1,   "max_ctx": 256000},
}
# cerebras / groq / nvidia / openrouter / github were removed at the operator's
# request (providers.DISABLED_PROVIDERS). Their entries are gone from LIMITS
# too, not merely filtered downstream: this table is served verbatim in
# /v1/status and rendered in Settings, so a surviving row would advertise a
# budget for a provider that can no longer be constructed.

# ── prompt-cache affinity ───────────────────────────────────────────────────
# A DAG node issues several LLM calls in a row. Provider caches are per
# provider (Gemini mints a cachedContent under one API key; the implicit prefix
# cache is per provider too), so letting each hop be re-picked by load throws
# away the cache every time. An affinity id pins the node to one provider.
#
# TTL is comfortably longer than a node's own iterations but far shorter than a
# run, so a node that finishes and a node that starts fresh are not confused,
# and a provider that goes stale is re-picked by load rather than pinned forever.
AFFINITY_TTL_S = 900.0
AFFINITY_MAX = 4096

SHORTCUTS = {
    "g": "gemini", "gem": "gemini", "gemini": "gemini",
    "g35l": "gemini35lite", "gem35l": "gemini35lite", "gemini35lite": "gemini35lite",
    "o": "ollama", "oll": "ollama", "ollama": "ollama",
    "k": "kilo", "kilo": "kilo",
}
# Aliases for the removed providers are gone WITH them. Leaving "n" or "gr"
# resolving to a provider that can no longer be constructed turns a typo into a
# 400 that looks like a gateway fault rather than an unknown shortcut.
for _dead_alias, _dead in (("n", "nvidia"), ("nv", "nvidia"), ("nvidia", "nvidia"),
                           ("gr", "groq"), ("groq", "groq"),
                           ("c", "cerebras"), ("cer", "cerebras"), ("cerebras", "cerebras"),
                           ("or", "openrouter"), ("opr", "openrouter"),
                           ("openrouter", "openrouter"),
                           ("gh", "github"), ("ghb", "github"), ("github", "github")):
    SHORTCUTS.pop(_dead_alias, None)


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
        self._tpm = 0  # running sum of tokens_minute, avoids O(n) re-sum per pick
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
            self._tpm -= self.tokens_minute.popleft()[1]

    def can_use(self, limits, est_tokens=0, ignore_pacing=False):
        """Is this provider usable right now?

        `ignore_pacing` skips ONE check: the `cooldown` spacing that separates
        consecutive calls to the same provider. That spacing exists to spread
        independent callers, and it is what RPM/TPM cannot see. It is the wrong
        tool for a DAG node's own next hop, which is not an independent caller
        but the same conversation, whose prompt cache lives on that provider —
        measured live, a four-hop node landed on four DIFFERENT siblings because
        each hop found its previous provider still 1-4s into pacing.

        It is not a general bypass: the failure backoff, RPM, RPD, TPM, the
        daily token cap, the capability and the context-window checks all still
        apply. Skipping those would turn cache affinity into an outage.
        """
        self.gc()
        now = time.time()
        if now < self.unavailable_until:
            return False, f"backoff: {self.unavailable_reason} ({self.unavailable_until - now:.0f}s left)"
        if not ignore_pacing:
            wait = limits["cooldown"] - (now - self.last_call)
            if wait > 0:
                return False, f"cooldown ({wait:.1f}s)"
        if len(self.calls_minute) >= limits["rpm"]:
            return False, "RPM limit"
        if self.calls_today >= limits["rpd"]:
            return False, "RPD limit"
        if self._tpm + est_tokens > limits["tpm"]:
            return False, "TPM limit"
        if "tokens_per_day" in limits and self.tokens_today + est_tokens > limits["tokens_per_day"]:
            return False, "daily token cap"
        return True, None

    def record(self, tokens):
        now = time.time()
        self.calls_minute.append(now)
        self.tokens_minute.append((now, tokens))
        self._tpm += tokens
        self.calls_today += 1
        self.tokens_today += tokens
        self.last_call = now

    def mark_unavailable(self, seconds: float, reason: str):
        self.unavailable_until = time.time() + seconds
        self.unavailable_reason = reason

    def snapshot(self, limits):
        self.gc()
        now = time.time()
        return {
            "rpm_used": len(self.calls_minute),
            "rpm_limit": limits["rpm"],
            "rpd_used": self.calls_today,
            "rpd_limit": limits["rpd"],
            "tpm_used": self._tpm,
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
        # affinity id -> (provider, last_used_monotonic). Per-Router, so a
        # per-user pool never inherits another user's provider history.
        # OrderedDict = O(1) LRU eviction instead of sorting the whole map.
        self.affinity: "OrderedDict[str, tuple[str, float]]" = OrderedDict()

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

    def pick(self, est_tokens, candidates, required_caps: list[str] | None = None,
             preferred: str | None = None, model: str | None = None):
        """Choose the least-recently-loaded usable candidate (fewest calls
        in the last minute, then longest idle) instead of strict priority
        order — concurrent calls spread across providers in parallel
        rather than queueing on provider #1 until it cools down.
        Capability, max_ctx and RPM/RPD/TPM guards are unchanged; error
        failover in main.py stays sequential (racing would pay N× cost).

        `preferred` short-circuits that for one case: a caller whose node is
        mid-conversation on a provider (see `note_affinity`) must stay there, or
        its prompt cache is wasted. It is honoured only when still usable, so a
        provider that has cooled into cooldown or blown its TPM is skipped and
        the load-based pick happens as before — affinity must never become an
        outage.
        """
        attempts = []
        usable = []
        for name in candidates:
            limits = limits_for(name)
            prov = self.providers[name]
            caps = getattr(prov, "capabilities", {})
            # V9: capability is per REQUESTED MODEL, not per provider default.
            # A provider such as Kilo serves hundreds of models behind one
            # configured default ("openrouter/free"); gating on that default
            # made every vision-capable model under it unreachable (the router
            # skipped Kilo with "no_vision" even when the caller pinned
            # nvidia/nemotron-3.5-super-vl). Recompute the caps for the model
            # the caller actually asked for, so the gate matches the request.
            if model:
                try:
                    import providers as _P
                    caps = _P.model_capabilities(name, model, caps)
                except Exception:
                    pass
            if required_caps:
                missing = [c for c in required_caps if not caps.get(c)]
                if missing:
                    attempts.append({"provider": name, "reason": f"skipped:no_{missing[0]}"})
                    continue
            if est_tokens > limits["max_ctx"]:
                attempts.append({"provider": name, "reason": f"prompt {est_tokens} > max_ctx {limits['max_ctx']}"})
                continue
            ok, why = self.state[name].can_use(
                limits, est_tokens,
                # Only the affinity candidate may cross the pacing gap; every
                # other provider is still spaced as before, so a wave of
                # distinct nodes keeps spreading instead of queueing on one key.
                ignore_pacing=(bool(preferred) and name == preferred))
            if ok:
                usable.append(name)
            else:
                attempts.append({"provider": name, "reason": why})
        if not usable:
            return None, attempts
        if preferred and preferred in usable:
            return preferred, attempts
        best = min(usable, key=lambda n: (len(self.state[n].calls_minute),
                                          self.state[n].last_call))
        return best, attempts

    # ── prompt-cache affinity ───────────────────────────────────────────────
    def provider_for(self, affinity: str | None) -> str | None:
        """The provider this affinity last succeeded on, if still fresh."""
        if not affinity:
            return None
        entry = self.affinity.get(affinity)
        if not entry:
            return None
        name, when = entry
        if time.time() - when > AFFINITY_TTL_S:
            self.affinity.pop(affinity, None)
            return None
        self.affinity.move_to_end(affinity)
        return name if name in self.providers else None

    def note_affinity(self, affinity: str | None, provider: str) -> None:
        """Remember where an affinity lives, so its next hop lands there.

        Bounded and TTL-expired rather than growing forever: the agent's
        affinity id is `session:node`, and a long-lived gateway would otherwise
        accumulate one entry per DAG node ever run. Eviction is by oldest
        timestamp, so the active set — the nodes in flight — is what survives.
        """
        if not affinity or provider not in self.providers:
            return
        self.affinity[affinity] = (provider, time.time())
        self.affinity.move_to_end(affinity)
        while len(self.affinity) > AFFINITY_MAX:
            self.affinity.popitem(last=False)

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

# The router pool now has exactly one member. cerebras/groq/nvidia/github were
# the per-model-metered routers and all four are in DISABLED_PROVIDERS, so
# leaving them here would advertise a ring that can never be constructed.
# Kilo joins the pool, so a tier classification falls back to the token-count
# rule only if Kilo itself is unavailable.
DEFAULT_ROUTER_ORDER = ["kilo"]


class RouterPool:
    """Failover ring for router-LLM calls. Mirrors `Router` but for the
    Perception/Memory/Decision routing classifiers. Each call is logged with
    a call_role marker (router_perception | router_memory | router_decision)
    so the dashboard can show router activity separately from worker activity.
    """
    def __init__(self, providers: dict, order: list[str]):
        self.providers = providers
        self.order = [o for o in order if o in providers]
        self.state = defaultdict(RateState)
        # Consecutive unparseable replies per router. A router that cannot
        # follow the tier instruction is benched instead of being asked
        # again on every request (see main._bench_router).
        self.parse_strikes: dict[str, int] = defaultdict(int)

    def candidates(self):
        return list(self.order)

    def all_status(self):
        out = {}
        for name in self.providers:
            out[name] = self.state[name].snapshot(limits_for(name))
            out[name]["model"] = self.providers[name].model
        return out
