"""Lazily-built, per-user provider pools.

A `Router` owns per-provider rate state, cooldowns and failover order. If two
users shared one Router, one user's 15 RPM would throttle the other. So each
user gets their own pool of provider instances and their own Router, built
from that user's decrypted keys and cached until those keys change.

The operator pool in `app.state` is untouched: a request with no user token
continues to use it.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from cache import GeminiCache
from router import Router, RouterPool

import providers as P


@dataclass
class UserPools:
    provider_cache: GeminiCache
    providers: dict
    router: Router
    router_pool: RouterPool
    signature: str


class UserPoolManager:
    def __init__(self, worker_order: list[str], router_order: list[str],
                 gemini_only: bool = False):
        self.worker_order = list(worker_order)
        self.router_order = list(router_order)
        self.gemini_only = bool(gemini_only)
        self._pools: dict[str, UserPools] = {}
        self._lock = threading.Lock()

    def invalidate(self, user_id: str) -> None:
        with self._lock:
            self._pools.pop(user_id, None)

    def get(self, store, user_id: str) -> UserPools | None:
        """The calling user's pools, or None when they have no keys.

        None deliberately does NOT fall back to the operator pool: a user
        token exists so calls are billed to that user's own credentials.
        """
        keys = store.provider_keys(user_id)
        if not keys:
            return None
        signature = store.signature(user_id)
        with self._lock:
            hit = self._pools.get(user_id)
            if hit is not None and hit.signature == signature:
                return hit

        cache = GeminiCache(ttl_seconds=300)
        providers = P.build_providers(cache, keys=keys)
        if self.gemini_only:
            providers = P.keep_gemini_only(providers)
            router_providers = {}
        else:
            router_providers = P.build_router_providers(keys=keys)
        if not providers:
            return None
        from router import expand_order
        router = Router(providers, expand_order(self.worker_order, providers))
        router_pool = RouterPool(
            router_providers,
            expand_order(self.router_order, router_providers))
        built = UserPools(cache, providers, router, router_pool, signature)
        with self._lock:
            self._pools[user_id] = built
        return built
