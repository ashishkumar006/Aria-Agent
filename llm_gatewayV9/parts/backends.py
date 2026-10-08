"""Optional-dependency probe.

`trimesh` and `manifold3d` are both optional and neither is in
pyproject.toml. Every import of them in this package goes through here, and
every call site checks the result, so a missing wheel degrades the feature
instead of breaking the gateway's import graph.

The probe caches, because `importlib.import_module` on a missing module is a
filesystem walk on every call, and the report serialises the probe result on
every request. `reset()` exists for tests that simulate an installed or
missing dependency.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from types import ModuleType

from .errors import MissingBackendError

# name -> distribution on PyPI. Kept beside the probe so the error a user
# sees names the thing they have to install, not the import that failed.
DISTRIBUTIONS = {
    "trimesh": "trimesh",
    "manifold3d": "manifold3d",
}


@dataclass(frozen=True)
class Backends:
    """Which optional libraries resolved, and why the others did not."""

    trimesh: ModuleType | None = None
    manifold3d: ModuleType | None = None
    errors: dict[str, str] = field(default_factory=dict)

    def has(self, name: str) -> bool:
        return getattr(self, name, None) is not None

    def module(self, name: str) -> ModuleType | None:
        return getattr(self, name, None)

    def version(self, name: str) -> str | None:
        mod = self.module(name)
        return getattr(mod, "__version__", None) if mod is not None else None

    def require(self, name: str) -> ModuleType:
        mod = self.module(name)
        if mod is None:
            raise MissingBackendError(
                f"{name} is not installed",
                module=name,
                install=DISTRIBUTIONS.get(name, name),
                reason=self.errors.get(name),
            )
        return mod

    def describe(self) -> dict:
        """JSON-safe capability block for the report.

        The report must say which libraries produced it. A watertightness
        answer computed without manifold3d is a weaker claim than one
        computed with it, and the consumer cannot otherwise tell.
        """
        return {
            name: {
                "available": self.has(name),
                "version": self.version(name),
                "error": self.errors.get(name),
            }
            for name in DISTRIBUTIONS
        }


_CACHE: Backends | None = None


def probe(force: bool = False) -> Backends:
    """Resolve optional backends, caching the outcome."""
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    found: dict[str, ModuleType] = {}
    errors: dict[str, str] = {}
    for name in DISTRIBUTIONS:
        try:
            found[name] = importlib.import_module(name)
        except Exception as exc:  # a broken wheel raises more than ImportError
            errors[name] = f"{type(exc).__name__}: {exc}"
    _CACHE = Backends(
        trimesh=found.get("trimesh"),
        manifold3d=found.get("manifold3d"),
        errors=errors,
    )
    return _CACHE


def reset() -> None:
    """Drop the cache so the next `probe()` re-imports."""
    global _CACHE
    _CACHE = None


__all__ = ["Backends", "DISTRIBUTIONS", "probe", "reset"]