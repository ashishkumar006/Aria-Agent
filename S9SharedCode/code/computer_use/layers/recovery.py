"""Layer D — error recovery (charter §8, §10).

Decides what to do when a turn fails. Carries minimal state across failures
so we don't loop forever. Maps the four "traps that look the same" to
distinct directives.
"""
from __future__ import annotations


class RecoveryPolicy:
    """Decides what to do when a turn fails. Carries minimal state across
    failures so we don't loop forever."""

    def __init__(self, max_retries: int = 3, reflow_rescans: int = 2):
        self.max_retries = max_retries
        self.reflow_rescans = reflow_rescans
        self.failures: list[str] = []

    def handle(self, failure: str) -> str:
        """Return a recovery directive: 'rescan' | 'escalate' | 'abort'."""
        self.failures.append(failure)
        f = failure.lower()
        # Classic reflow cache-miss → re-scan before retrying (trap #3).
        if "element_index" in f and ("not found" in f or "out of range" in f):
            if self.failures.count(failure) <= self.reflow_rescans:
                return "rescan"
        # Permissions / denial → abort (trap #1, trap #4 elevated).
        if "permission" in f or "denied" in f or "tcc" in f or "uac" in f:
            return "abort"
        if len(self.failures) >= self.max_retries:
            return "abort"
        return "rescan"
