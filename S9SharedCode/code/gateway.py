"""Bridge to llm_gatewayV9.

V9 is V8 plus two things: (1) `/v1/vision` — typed shim for single-image
vision calls that the Browser skill's Layer-3 driver hits; (2) per-agent
USD pricing on `/v1/cost/by_agent` so the ledger surfaces dollars in
addition to tokens. V8's `agent` tagging, `/v1/chat/batch`, and retry-
on-5xx carry forward unchanged.

The session-version mapping (V9 for Session 9) lets V8 stay frozen for
Session 8.

Auto-starts the gateway on port 8109 if it is not already up, then
re-exports the V9 `LLM` client and a module-level `embed()` helper.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import subprocess
import time
import weakref
from typing import Optional
from pathlib import Path

import httpx

GATEWAY_V9_DIR = Path(__file__).resolve().parents[2] / "llm_gatewayV9"
# 127.0.0.1, NOT "localhost". On this box `localhost` resolves to ::1 first
# and the gateway binds IPv4 only, so every call paid a refused IPv6 connect
# before falling back — measured 2591ms via httpx against 1034ms for the
# literal address. On a memory-panel request that is paid twice (liveness
# probe + the real call).
GATEWAY_URL = "http://127.0.0.1:8109"

# One pooled, keep-alive client for the whole process. `httpx.get(...)` builds
# a Client per call, which constructs an SSL context and re-reads the CA
# bundle each time — ~1s of pure setup on this machine, on top of a fresh TCP
# handshake. Measured: 1034ms for a pooled-client request against ~10ms from
# a client that already had a connection.
_CLIENT: "httpx.Client | None" = None
# Its own pool, for the same reason a watchdog gets its own power supply.
_PROBE_CLIENT: "httpx.Client | None" = None


def _gateway_token() -> str:
    """The shared gateway token, or "" when none is reachable yet.

    The gateway now requires `X-Gateway-Token` on every `/v1/*` route - its own
    surface had been reachable with no credential at all, which let an
    unauthenticated caller wipe memory, reload policy and spend real money. The
    two processes start independently, so the gateway publishes its token to
    `state/gateway.token` at launch; an operator can instead set
    `GATEWAY_V9_TOKEN` for both sides. Never raise here: a missing token must
    produce a clear 401 from the gateway, not an import error at agent start-up.
    """
    import os as _os
    from pathlib import Path as _P
    env = (_os.environ.get("GATEWAY_V9_TOKEN") or "").strip()
    if env:
        return env
    # Same base GATEWAY_V9_DIR is built from (parents[2] = repo root); an
    # off-by-one here silently yields "" and every gateway call 401s.
    p = _P(__file__).resolve().parents[2] / "llm_gatewayV9" / "state" \
        / "gateway.token"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _client() -> "httpx.Client":
    global _CLIENT
    token = _gateway_token()
    headers = {"X-Gateway-Token": token} if token else {}
    if _CLIENT is None:
        _CLIENT = httpx.Client(
            # `pool` belongs on Timeout, not on Limits: `Limits(pool_timeout=...)`
            # raises TypeError, and because `get_json` swallows every exception by
            # design that turned into `{"documents": []}` — the whole document
            # library reading as empty. It was caught the same minute it happened
            # only because these helpers now name what they swallow.
            timeout=httpx.Timeout(30.0, pool=10.0), follow_redirects=True,
            # 64, not 16. This one client serves every plain read AND the
            # vision/upload calls, which hold their slot for the full
            # `self._raw.timeout` — 600 s. Eight flow nodes plus a render was
            # enough to exhaust 16 slots, and an exhausted pool blocks the next
            # caller for its whole timeout. The pool wait is capped at 10 s so
            # saturation surfaces as a named, logged failure instead of a slow
            # mysterious None.
            limits=httpx.Limits(max_keepalive_connections=16,
                                max_connections=64),
            headers=headers)
    elif token and "X-Gateway-Token" not in _CLIENT.headers:
        # Token file appeared after the client was built (gateway started late).
        _CLIENT.headers["X-Gateway-Token"] = token
    return _CLIENT


_swallow_notes = {"last": 0.0, "counts": {}}


def _note_swallow(where: str, what: str) -> None:
    """Name a swallowed gateway failure, at most one line per 30 s.

    `get_json`/`post_multipart`/`delete_json` return None by design — a missing
    index is not a failed run — but "by design" was indistinguishable from
    "silently broken". The author's figure listing is one `get_json` whose
    fallback is `{}`, so a gateway hiccup cost it EVERY picture in the library
    and left no trace: it read exactly like a library with no images in it. The
    None contract stays; the line is what was missing.
    """
    import time as _t
    now = _t.time()
    counts = _swallow_notes["counts"]
    counts[where] = counts.get(where, 0) + 1
    if now - _swallow_notes["last"] >= 30.0:
        _swallow_notes["last"] = now
        total = sum(counts.values())
        print(f"[gateway] swallowed failure: {where} -> {what} "
              f"({total} such call(s) in the window; returning None as designed, "
              f"but this is how a capability can look absent while broken)")
        _swallow_notes["counts"] = {}


def get_json(path: str, timeout: float = 10.0) -> dict | None:
    """One GET against a gateway JSON route, or None when it cannot be had.

    Auxiliary reads (a document's figure listing, a catalogue) used to need a
    client and a token at every call site. They also must never raise into a
    run: a listing the agent cannot read is a missing hint, not a failed
    document, so the failure mode here is a quiet None — now a LOUD quiet."""
    try:
        r = _client().get(f"{GATEWAY_URL}{path}", timeout=timeout)
        if r.status_code != 200:
            _note_swallow(path, f"HTTP {r.status_code}")
            return None
        out = r.json()
    except Exception as e:
        _note_swallow(path, f"{type(e).__name__}: {str(e)[:80]}")
        return None
    return out if isinstance(out, dict) else None


def post_multipart(path: str, *, files: dict, data: dict | None = None,
                   timeout: float = 120.0) -> dict | None:
    """One multipart POST to a gateway route, or None when it cannot be had.

    The document plane's only ingest is a file upload, so every caller that
    wants bytes indexed (an artifact, a fetched web page) needs this. Status is
    deliberately not raised into the caller's face: a document that failed to
    register is a missing index, not a failed run, and the caller keeps its
    artifact either way."""
    try:
        r = _client().post(f"{GATEWAY_URL}{path}", files=files,
                           data=data or {}, timeout=timeout)
        out = r.json()
    except Exception as e:
        _note_swallow(path, f"{type(e).__name__}: {str(e)[:80]}")
        return None
    if r.status_code >= 400:
        _note_swallow(path, f"HTTP {r.status_code}")
    return out if isinstance(out, dict) else None


def document_exists(doc_id: str, timeout: float = 10.0):
    """True if the document plane holds this id, False if it says 404, None if
    the gateway could not be asked.

    `get_json` folds every failure into None, which is the right contract for a
    read and the wrong one for a staleness check: a link to a document the user
    deleted must be repaired, while a link during an outage must be left alone.
    Only an explicit 404 is evidence the row is gone."""
    try:
        r = _client().get(f"{GATEWAY_URL}/v1/documents/{doc_id}",
                          timeout=timeout)
    except Exception:
        return None
    if r.status_code == 404:
        return False
    if r.status_code != 200:
        return None
    try:
        return bool((r.json() or {}).get("document"))
    except Exception:
        return None


def delete_json(path: str, timeout: float = 30.0) -> dict | None:
    """One DELETE against a gateway JSON route, or None when it cannot be had.

    The companion of `get_json`/`post_multipart`. Call sites used to hand-roll
    an httpx client for the one verb that was missing — which is how the persona
    publisher kept uploading to the live document plane while every other upload
    path was already behind a seam a test could hold shut."""
    try:
        r = _client().delete(f"{GATEWAY_URL}{path}", timeout=timeout)
        if r.status_code >= 400:
            return None
        try:
            out = r.json()
        except Exception:
            return {}
    except Exception:
        return None
    return out if isinstance(out, dict) else {}


# Sync pool keyed by timeout, for the call sites that cannot use `_client()`
# unchanged because they deliberately ask for a different budget (the notify
# resolver uses 8s so a wedged gateway cannot hold a reminder hostage).
_SYNC_POOL: dict[float, "httpx.Client"] = {}


@contextlib.contextmanager
def sync_pool(timeout: float = 30.0):
    """`with sync_pool(8.0) as c:` over a shared client; exit does not close.

    `httpx.Client` construction is 230-590 ms here, and `notify.resolve_target`
    built one per channel per request — which is why `/api/notify/channels`
    answered 16 channels in 9.6 s and blocked the agent's event loop for all of
    it. Same reason `_client()` exists.

    These pools carry NO headers. Authorization stays on the request, computed
    fresh by the caller (`_gw_auth_headers()` / `_gw_headers()`): the shared
    client is reused by every caller in the process, so a header set on it would
    outlive the call that set it — and a per-user token placed there would ride
    along on someone else's request.
    """
    key = round(float(timeout), 3)
    c = _SYNC_POOL.get(key)
    if c is None or getattr(c, "is_closed", False):
        c = httpx.Client(timeout=key, follow_redirects=True,
                         limits=httpx.Limits(max_keepalive_connections=4,
                                             max_connections=16))
        _SYNC_POOL[key] = c
    yield c


# ── the same lesson, for the async half ─────────────────────────────────────
# `_client()` pooled the sync path years ago, but every `httpx.AsyncClient` in
# this process was still built per call. Building one costs 230-590 ms here
# (fresh SSL context + pool), and it happened on EVERY agent->gateway proxy
# read, every memory embed, every voice chunk and every streaming chat call.
# Measured consequence: `/api/documents/folders` answered 14 bytes in 520 ms and
# `/api/notify/channels` took 9.6 s, almost none of it the gateway's fault.
#
# Keyed by the running loop because an httpx client is bound to the loop that
# created it; reusing one across loops raises "Event loop is closed", and the
# test suite runs several lifespans in one process.
#
# The key is the loop OBJECT through a WeakKeyDictionary, never `id(loop)`. An
# id is a memory address, and CPython hands a finished loop's address straight
# to the next one — so an `(id(loop), timeout)` key collided and a client bound
# to a long-dead loop was handed to a live loop born where it used to sit.
# `test_async_pool_is_scoped_to_the_running_loop` caught that as an intermittent
# failure: two successive `asyncio.run()` calls got back the SAME client. Weak
# keys fix both halves — no address is reused while its entry still matters, and
# a loop that dies takes its clients out of the pool instead of leaking them.
_ASYNC_POOL: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def async_client(timeout: float = 30.0) -> "httpx.AsyncClient":
    """A shared AsyncClient for gateway calls on the current event loop.

    The caller must NOT close it. Per-request headers (a user token, say) still
    belong on the request, not here — this client carries NO headers, so one
    caller's credential can never ride along on another's request.
    """
    import asyncio
    loop = asyncio.get_running_loop()
    # `timeout` is sometimes an httpx.Timeout (the streaming path asks for a
    # 600s read with a 10s connect), sometimes a plain float. Key on whatever
    # was passed so the two budgets do not share one client.
    by_timeout = _ASYNC_POOL.setdefault(loop, {})
    tkey = repr(timeout)
    c = by_timeout.get(tkey)
    if c is None or getattr(c, "is_closed", False):
        c = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=32))
        by_timeout[tkey] = c
    return c


@contextlib.contextmanager
def async_session(timeout: float = 30.0):
    """`with async_session() as c:` over the pooled client; exit does not close.

    Deliberately a *sync* context manager: `async with` on an httpx client is
    what people write, and it is exactly the thing that must stop closing the
    shared connection pool.
    """
    yield async_client(timeout)


def reset_client_pools() -> None:
    """Forget every pooled client. For tests, never for production traffic.

    Several suites patch `httpx.Client` / `httpx.AsyncClient` with a fake to
    assert that a proxy call carries the gateway token, or to simulate a dead
    gateway. Those patches only take effect if the call site actually
    constructs a client, which pooling deliberately stopped doing — so a cached
    client would leak from one test into the next and the assertion would
    silently describe a connection made before the test started.

    Dropping the entries (not closing them) is enough and avoids needing a live
    event loop: the abandoned clients are garbage-collected.
    """
    _SYNC_POOL.clear()
    _ASYNC_POOL.clear()


def _probe_client() -> "httpx.Client":
    """A connection pool of one, exclusively for liveness.

    The probe used to borrow the shared `_CLIENT` pool — 16 connections that
    document uploads hold for 120 s and vision calls hold for 600 s. Once all
    16 were busy, the probe could not even get a socket: it hit PoolTimeout,
    which is not a ConnectError, so it fell into the soft branch and the
    gateway was declared dead while answering real requests in 269 ms. A
    liveness check must not depend on the resource it is checking.
    """
    global _PROBE_CLIENT
    token = _gateway_token()
    headers = {"X-Gateway-Token": token} if token else {}
    if _PROBE_CLIENT is None:
        _PROBE_CLIENT = httpx.Client(
            # Split on purpose. A slow CONNECT is what "nothing is listening"
            # looks like, so it stays short and `/api/health` does not hang. A
            # slow READ is what a loaded gateway looks like, so it gets room:
            # eight concurrent probes against an idle gateway already finished
            # in 2.26 s, and a flat 2.5 s budget gave that 90 % reading no
            # headroom at all before it was misread as death.
            timeout=httpx.Timeout(connect=2.0, read=9.0, write=5.0, pool=2.0),
            follow_redirects=True,
            # No keep-alive: a pooled socket left open by the gateway's 5 s
            # idle timeout would have to be rediscovered on the next probe.
            # 8 connections, not 2: the console and the desktop shell poll
            # together, and a two-slot probe pool turns a third concurrent
            # poller into a PoolTimeout — which used to count as evidence of
            # death.
            limits=httpx.Limits(max_keepalive_connections=0, max_connections=8),
            headers=headers)
    elif token and "X-Gateway-Token" not in _PROBE_CLIENT.headers:
        _PROBE_CLIENT.headers["X-Gateway-Token"] = token
    return _PROBE_CLIENT


def _probe() -> str:
    """Liveness, three-valued on purpose: "up", "down", or "busy".

    "busy" is the case that was missing, and it is the case the whole system
    confuses for death. The only evidence that NOTHING IS LISTENING is a
    connection-level refusal. A read timeout, a pool timeout, or a 5xx is
    evidence that something is listening and is slow or unhappy — which must
    never trigger a second gateway launch.

    Measured on this box, gateway idle: eight concurrent probes finished in
    2.23-2.26 s against a 2.5 s budget. That is 90 % of the timeout with no
    load on it, because `/health` is served from the gateway's single event
    loop and that loop also does the ledger's sqlite writes. So under real load
    the probe DOES cross the deadline — and every crossing used to mean
    `return False`, `ensure_gateway()` launching a duplicate `main.py` on the
    already-bound port with its output to DEVNULL, 45 s of polling, then
    "Gateway V9 failed to start within 45s" returned to a caller that drops the
    turn. That is the "runs get skipped when the gateway is under load" report,
    and the gateway was up the entire time.

    The three paths still short-circuit on "down": they share one host, so when
    nothing is listening the other two cannot answer either, and on Windows a
    closed loopback port silently drops the SYN rather than refusing it — so
    trying all three multiplied the wait by three.
    """
    for path in ("/health", "/healthz", "/v1/routers"):
        try:
            r = _probe_client().get(f"{GATEWAY_URL}{path}")
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return "down"
        except (httpx.ReadTimeout, httpx.PoolTimeout, httpx.WriteTimeout) as e:
            # Slow, not dead. Keep trying the other paths on the chance one is
            # cheap enough to answer, but remember we never saw a refusal.
            _note_busy(f"{path}: {type(e).__name__}")
            continue
        except Exception as e:
            _note_busy(f"{path}: {type(e).__name__}")
            continue
        if r.status_code == 404:
            # This gateway version does not implement the path; try the next.
            continue
        if r.status_code < 500:
            return "up"
        return "busy"  # 5xx: it answered. Answering is proof of life.
    return "busy"


_busy_notes = {"n": 0, "last": 0.0}


def _note_busy(what: str) -> None:
    """Say it out loud, at most once every 30 s.

    A busy probe used to be indistinguishable from a dead one in the logs, which
    is why this took so long to find: the run failed with "gateway unavailable"
    while the gateway's own ledger was recording it answering requests.
    """
    import time as _t
    now = _t.time()
    _busy_notes["n"] += 1
    if now - _busy_notes["last"] >= 30.0:
        _busy_notes["last"] = now
        print(f"[gateway] probe slow, NOT treating as down: {what} "
              f"({_busy_notes['n']} such probes since last note)")
        _busy_notes["n"] = 0


def _is_up() -> bool:
    """True unless nothing is listening.

    Deliberately true for "busy": a loaded gateway must not be reported dead.
    Callers that would launch a process need `_probe() == "down"` and nothing
    less, which is what `ensure_gateway()` now asks for.
    """
    return _probe() != "down"


def ensure_gateway() -> None:
    """Start V9 if it is not already running. Idempotent.

    Only a genuine "nothing is listening" may launch a process. A busy answer is
    waited on instead, because launching a second `main.py` against an
    already-bound port fails at bind time, its error went to DEVNULL so nothing
    showed, and the 45 s poll then raised "failed to start within 45s" — killing
    a turn against a gateway that was alive and merely loaded.
    """
    state = _probe()
    if state != "down":
        if state == "busy":
            # Give the loop a moment to drain and confirm, without launching.
            for _ in range(6):
                time.sleep(0.5)
                if _probe() == "up":
                    return
            # Still slow. Proceed anyway: the request itself will time out or
            # succeed, and it is far better than concluding the gateway is gone.
            print("[gateway] still slow after waiting; sending the request "
                  "anyway rather than launching a duplicate")
        return
    if not GATEWAY_V9_DIR.exists():
        raise RuntimeError(
            f"Gateway V9 directory not found at {GATEWAY_V9_DIR}. "
            "Build llm_gatewayV9 (Session 9 prerequisite) before running S9 code."
        )
    print(f"[gateway] nothing listening on {GATEWAY_URL}; "
          f"launching llm_gatewayV9 from {GATEWAY_V9_DIR}")
    # A real log, not DEVNULL: a child that dies on "address already in use"
    # was previously undiagnosable because its only evidence was discarded.
    try:
        _out = open(Path(__file__).resolve().parent / "state" / "gateway_launch.log",
                    "ab")
    except Exception:
        _out = subprocess.DEVNULL
    try:
        subprocess.Popen(
            ["uv", "run", "main.py"],
            cwd=str(GATEWAY_V9_DIR),
            stdout=_out,
            stderr=subprocess.STDOUT,
        )
    finally:
        if _out not in (subprocess.DEVNULL, None):
            try:
                _out.close()
            except Exception:
                pass
    for _ in range(45):
        time.sleep(1)
        if _probe() != "down":
            print(f"[gateway] up on {GATEWAY_URL}")
            return
    raise RuntimeError(f"Gateway V9 failed to start within 45s. Check {GATEWAY_V9_DIR}")


# Load V9's client.py without polluting sys.path. The gateway dir has its
# own `schemas.py`, which would shadow ours if we put it on the path.
import importlib.util as _importlib_util

_client_path = GATEWAY_V9_DIR / "client.py"
if _client_path.exists():
    _spec = _importlib_util.spec_from_file_location("llm_gatewayV9_client", _client_path)
    _mod = _importlib_util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _RawLLM = _mod.LLM
else:
    _RawLLM = None  # populated once V9 is built; importers should ensure_gateway() first


# ── Routing wrapper ─────────────────────────────────────────────────────────
# S9 no longer forces a single provider/model. Every call passes through to
# the gateway's NATURAL routing system (router.order / agent_routing.yaml /
# MODEL_ROUTES / auto_route). The gateway's failover ladder stays intact and
# picks the best available backend per request. Callers may still pin
# `provider`/`model`/`agent`/`auto_route` explicitly when they want to.
#
# Optional env knobs (only applied when set) let an operator bias the default
# without hard-coding it: S9_LLM_PROVIDER / S9_LLM_MODEL. When unset, the
# gateway decides.
_DEFAULT_PROVIDER = os.getenv("S9_LLM_PROVIDER") or None
_DEFAULT_MODEL = os.getenv("S9_LLM_MODEL") or None

# ── per-node prompt-cache affinity ──────────────────────────────────────────
# A DAG node is not one LLM call. The tool loop iterates (sometimes six
# times), an author retries on an empty reply, a researcher writes its
# findings up in several calls. The gateway picks the LEAST-LOADED provider per
# request, so every one of those hops could land on a different provider — and
# caches are per provider (Gemini mints a cachedContent under one API key and
# cannot read another's; the implicit prefix cache is per provider too). A
# bouncing node therefore re-pays its whole system prompt on every hop and gets
# nothing back.
#
# The node sets this once, and every call it makes inherits the same id, so the
# gateway keeps that node on one provider while different nodes still spread.
#
# A ContextVar, not a parameter, because `run_skill` reaches the gateway from a
# dozen places (`_sectioned_document_blocks`, `_repair_json_reply`,
# `run_with_tools`, the vision path, …) and threading one more argument through
# all of them invites exactly the bug this file already documents twice: a value
# read where it was never bound, surfacing as a NameError inside the run. The
# context travels into `asyncio.to_thread` and into tasks created under the
# node, which is precisely where those calls happen.
_NODE_AFFINITY: contextvars.ContextVar = contextvars.ContextVar(
    "aria_node_route_affinity", default=None)


def node_affinity(affinity: Optional[str]):
    """Bind a route-affinity id for every gateway call made inside the block."""
    return _NODE_AFFINITY.set(affinity) if affinity else None


@contextlib.contextmanager
def node_scope(affinity: Optional[str]):
    """`with node_scope(f"{session}:{node}"):` — set, then always reset.

    Resetting matters: the executor runs nodes in tasks on one loop, and a
    token left unset would let a later node inherit an earlier node's provider
    history and get pinned somewhere it never chose.
    """
    if not affinity:
        yield
        return
    token = _NODE_AFFINITY.set(affinity)
    try:
        yield
    finally:
        _NODE_AFFINITY.reset(token)


def current_node_affinity() -> Optional[str]:
    return _NODE_AFFINITY.get()


# ── operator Stop, seen from inside a node ──────────────────────────────────
# The same shape and the same reason as the affinity above. `should_cancel` is
# decided by the request handler and honoured deep inside the work: the tool
# loop's hop boundary, the per-section authoring calls, the figure pre-pass.
# Threading a predicate through `run_skill` -> `_run_loop` -> `run_with_tools`
# -> `run_tool_loop` and through every section call would touch six signatures
# to pass one boolean, and this file already documents twice what a value read
# where it was never bound does to a run.
#
# It holds a CALLABLE, not a bool: the event is set by another thread minutes
# after the node starts, so a snapshot taken at entry would always say "keep
# going".
_NODE_CANCEL: contextvars.ContextVar = contextvars.ContextVar(
    "aria_node_cancel", default=None)


@contextlib.contextmanager
def cancel_scope(should_cancel):
    """Publish the run's cancel predicate for everything called inside the block.

    `None` (every non-DAG caller, and a DAG run with the flag off) binds
    nothing, so `cancel_requested()` stays False and the code paths below it
    behave exactly as they did before.
    """
    if should_cancel is None:
        yield
        return
    token = _NODE_CANCEL.set(should_cancel)
    try:
        yield
    finally:
        _NODE_CANCEL.reset(token)


def cancel_requested() -> bool:
    """True once the operator has stopped the run this call belongs to.

    False outside a DAG node. A predicate that raises returns False: a
    cancellation check must never be the thing that fails a node.
    """
    fn = _NODE_CANCEL.get()
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        return False


class _RoutedLLM:
    """Thin proxy over the V9 LLM client. Does NOT force routing — it lets
    the gateway's natural routing decide unless the caller explicitly pins
    provider/model/agent/auto_route."""

    def __init__(self, base_url: str = GATEWAY_URL, timeout: float = 600):
        if _RawLLM is None:
            raise RuntimeError(
                "Gateway V9 client unavailable. Confirm llm_gatewayV9/client.py exists."
            )
        self._raw = _RawLLM(base_url=base_url, timeout=timeout)

    def chat(self, prompt: str = None, *, messages=None, system=None,
             provider: str = None, model: str = None, max_tokens=2048,
             temperature=0.7, tools=None, tool_choice=None, cache_system=None,
             reasoning=None, response_format=None, auto_route=None,
             agent=None, session=None, route_affinity=None) -> dict:
        # Only apply the optional env bias when the caller did not pin anything.
        provider = provider or _DEFAULT_PROVIDER
        model = model or _DEFAULT_MODEL
        # Inside a DAG node the affinity is inherited from the context, so every
        # one of the node's hops shares one provider without any call site
        # having to know about it.
        route_affinity = route_affinity or _NODE_AFFINITY.get()
        return self._raw.chat(
            prompt=prompt, messages=messages, system=system,
            provider=provider, model=model, max_tokens=max_tokens,
            temperature=temperature, tools=tools, tool_choice=tool_choice,
            cache_system=cache_system, reasoning=reasoning,
            response_format=response_format, auto_route=auto_route,
            agent=agent, session=session, route_affinity=route_affinity,
        )

    async def stream_chat(self, *, messages, tools=None, tool_choice="auto",
                          provider: str = None, model: str = None,
                          max_tokens=2048, temperature=0.7,
                          agent=None, session=None, route_affinity=None):

        """Stream /v1/chat and yield events as they arrive.

        Async on purpose: it runs on the caller's event loop, so a callback that
        touches an asyncio.Queue (the streaming endpoint does) is safe. An
        earlier sync-generator design iterated inside a worker thread and put
        onto that queue from the wrong thread.

        Yields plain dicts:
          {"type": "text", "text": <delta>}            - one per token group
          {"type": "tool_calls", "tool_calls": [...]}  - once, before "done"
          {"type": "done", "provider": <name>}

        Tool calls arrive from the gateway as OpenAI streaming fragments
        (partial JSON keyed by index), so they are accumulated here and only
        handed out whole; the loop never sees a half-built call.
        """
        body = {
            "messages": messages, "stream": True,
            "max_tokens": max_tokens, "temperature": temperature,
            "provider": provider or _DEFAULT_PROVIDER,
            "model": model or _DEFAULT_MODEL,
            "agent": agent, "session": session,
            "route_affinity": route_affinity or _NODE_AFFINITY.get(),
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"
        headers = {"Content-Type": "application/json"}
        tok = _gateway_token()
        if tok:
            headers["X-Gateway-Token"] = tok
        acc: dict[int, dict] = {}
        with async_session(httpx.Timeout(600.0, connect=10.0)) as c:
            async with c.stream("POST", f"{GATEWAY_URL}/v1/chat", json=body,
                                headers=headers) as r:
                if r.status_code != 200:
                    raw = (await r.aread()).decode("utf-8", "ignore")[:300]
                    raise RuntimeError(f"gateway HTTP {r.status_code}: {raw}")
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    try:
                        d = json.loads(line[6:])
                    except Exception:
                        continue
                    if d.get("error"):
                        raise RuntimeError(str(d["error"])[:300])
                    if d.get("delta"):
                        yield {"type": "text", "text": str(d["delta"])}
                    if d.get("reasoning_delta"):
                        # The model's reasoning, on its own channel. The panel
                        # renders it collapsed; it is never part of the answer.
                        yield {"type": "thinking", "text": str(d["reasoning_delta"])}
                    frag = d.get("tool_call_delta")
                    if frag:
                        try:
                            pieces = json.loads(frag)
                        except Exception:
                            pieces = []
                        for pc in (pieces if isinstance(pieces, list) else []):
                            idx = int(pc.get("index") or 0)
                            slot = acc.setdefault(
                                idx, {"id": "", "name": "", "arguments": ""})
                            if pc.get("id"):
                                slot["id"] = pc["id"]
                            fn = pc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
                            # Gemini's thought signature. It has no place in
                            # OpenAI's fragment shape, so the gateway sends it
                            # as a sibling key, and it is not optional: Gemini 3
                            # 400s on a replayed functionCall that lacks one,
                            # which the router reports as a 502 and the node
                            # dies. `provider_meta` is where the batched path
                            # already puts it and where `_translate_messages`
                            # already looks.
                            sig = (pc.get("thought_signature")
                                   or pc.get("thoughtSignature"))
                            if sig:
                                slot["provider_meta"] = {"thoughtSignature": sig}
                    if d.get("done"):
                        yield {"type": "done",
                               "provider": d.get("provider") or ""}
        if acc:
            calls = []
            for idx in sorted(acc):
                slot = acc[idx]
                raw_args = slot["arguments"].strip()
                try:
                    parsed = json.loads(raw_args) if raw_args else {}
                except Exception:
                    parsed = {}
                calls.append({"id": slot["id"] or f"call_{idx}",
                              "name": slot["name"],
                              "arguments": parsed if isinstance(parsed, dict) else {},
                              "provider_meta": slot.get("provider_meta")})
            yield {"type": "tool_calls", "tool_calls": calls}

    def vision(self, image, prompt, *, system=None, provider: str = None,
               model: str = None, max_tokens=800, temperature: float = 0.0,
               agent=None, session=None, response_format=None) -> dict:
        # The V9 client exposes vision via /v1/vision; let the gateway route
        # it naturally unless the caller pinned provider/model.
        # VisionRequest has `schema`/`schema_name` (NOT `response_format`),
        # so translate a json_schema response_format instead of sending a
        # key the server silently drops.
        provider = provider or _DEFAULT_PROVIDER
        model = model or _DEFAULT_MODEL
        body = {
            "image": image, "prompt": prompt, "max_tokens": max_tokens,
            "temperature": temperature, "agent": agent, "session": session,
            "provider": provider, "model": model,
        }
        if system:
            body["system"] = system
        if isinstance(response_format, dict):
            _rf_schema = (response_format.get("schema")
                          or (response_format.get("json_schema") or {}).get("schema"))
            if _rf_schema:
                body["schema"] = _rf_schema
                body["schema_name"] = response_format.get("name", "out")
        # The gateway requires X-Gateway-Token on every /v1/* route, and this
        # call used a bare httpx.post with no headers: the vision endpoint
        # answered 401 and the computer-use engine - which swallows the error
        # and escalates - reported "vision returned unexpected type: NoneType".
        # So the whole L3 layer of the desktop agent was dead while the chat
        # path (which uses the pooled, token-carrying client) worked fine.
        r = _client().post(f"{self._raw.base_url}/v1/vision", json=body,
                            timeout=self._raw.timeout)
        r.raise_for_status()
        return r.json()

    def embed(self, text: str, task_type: str = "retrieval_document",
              provider: str = None, agent: str = None, session: str = None) -> dict:
        return self._raw.embed(text, task_type=task_type, provider=provider,
                               agent=agent, session=session)

    def chat_batch(self, calls: list[dict], max_concurrency: int = 4) -> list[dict]:
        # Do not pin provider/model on batched calls — let the gateway route
        # each one naturally. Only apply the optional env bias if a call has
        # neither set.
        for c in calls:
            c.setdefault("provider", _DEFAULT_PROVIDER)
            c.setdefault("model", _DEFAULT_MODEL)
        return self._raw.chat_batch(calls, max_concurrency=max_concurrency)

    def capabilities(self):
        return self._raw.capabilities()

    def cost_by_agent(self, session=None, agent=None) -> dict:
        return self._raw.cost_by_agent(session=session, agent=agent)


# Public handle used across S9: `from gateway import LLM; LLM().chat(...)`.
LLM = _RoutedLLM


def embed(text: str, task_type: str = "retrieval_document",
          agent: str | None = None, session: str | None = None) -> dict:
    """Compute an embedding for `text` via the gateway's embed endpoint."""
    ensure_gateway()
    return LLM().embed(text, task_type=task_type, agent=agent, session=session)


__all__ = ["ensure_gateway", "LLM", "GATEWAY_URL", "GATEWAY_V9_DIR", "embed"]
