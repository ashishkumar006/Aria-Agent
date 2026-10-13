"""Blocking-call and scope regressions in the async chat paths.

Both classes of bug here shipped green once:

* A synchronous gateway call inside an `async def` blocks the event loop, so
  every other request in the process waits. `/api/health` measured 15ms at
  rest and ~3s during a chat turn.
* Using an alias (`_aio`) before the `import ... as _aio` that binds it is a
  NameError at runtime, not an import error - so the module imported fine and
  the route only failed when a turn actually ran.

The second one bit twice in a single session: a fixed warmup join reintroduced
it in `chat_simple_stream`, and again in three handlers when the blocking
calls were wrapped.
"""
import ast
import inspect
import pathlib

import pytest

import agent_server

SRC = pathlib.Path(agent_server.__file__).read_text(encoding="utf-8")


def _aio_is_bound_before_use(fn) -> list[int]:
    """Lines using `_aio.to_thread` before `_aio` is bound in that scope.

    A nested function legitimately closes over its parent's binding, so only
    module-level functions are checked.
    """
    tree = ast.parse(inspect.getsource(fn))
    uses = [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == "to_thread"
            and isinstance(n.value, ast.Name) and n.value.id == "_aio"]
    if not uses:
        return []
    binds = [n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.Import) for a in n.names
             if (a.asname or a.name.split(".")[0]) == "_aio"]
    first_use = min(uses)
    earlier = [b for b in binds if b < first_use]
    if earlier:
        return []
    return [u for u in uses if min(binds or [10**9]) > u] if binds else uses


@pytest.mark.parametrize("fn", [
    agent_server.chat_simple,
    agent_server.chat_simple_stream,
    agent_server.agui_endpoint,
    agent_server._with_chat_memory,
    agent_server._doc_context,
], ids=lambda f: f.__name__)
def test_the_alias_is_bound_before_it_is_used(fn):
    """`await _aio...` with no binding in scope is a NameError.

    A module-level name lookup passes at import, so this only fires when a
    turn actually runs - and it takes the whole route down, not just the
    branch that used it.
    """
    early = _aio_is_bound_before_use(fn)
    assert not early, (
        f"{fn.__name__} awaits _aio.to_thread on line(s) {early} before the "
        f"`import asyncio as _aio` that binds it")


def test_every_module_level_async_handler_binds_its_alias_first():
    """Sweep all module-level async defs, not just the ones named above.

    The three that broke were found one failure at a time. This finds them all
    at once, and fails when a NEW handler is added with the same mistake.
    """
    tree = ast.parse(SRC)
    module_bound = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                module_bound.add(a.asname or a.name.split(".")[0])

    broken = []
    for fn in tree.body:
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        uses = [n.lineno for n in ast.walk(fn)
                if isinstance(n, ast.Attribute) and n.attr == "to_thread"
                and isinstance(n.value, ast.Name) and n.value.id == "_aio"]
        if not uses:
            continue
        if "aio" in module_bound:
            continue
        binds = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Import)
                 for a in n.names
                 if (a.asname or a.name.split(".")[0]) == "_aio"]
        if not binds:
            broken.append(f"{fn.name} (never bound)")
        elif min(binds) > min(uses):
            broken.append(f"{fn.name} (bound at {min(binds)}, used at {min(uses)})")
    assert not broken, (
        f"async handlers use _aio before binding it: {broken}")


def test_no_sync_gateway_call_survives_in_the_hot_async_paths():
    """The blocking calls that made a chat turn freeze the console.

    `_session_cost_breakdown`/`_session_cost_delta` are a synchronous
    httpx.Client to localhost with a 5s timeout; `_with_chat_memory` makes
    three sequential gateway round-trips with 30-60s timeouts. Both were
    called directly from `async def` handlers.
    """
    for fn in (agent_server.chat_simple, agent_server.chat_simple_stream,
               agent_server.agui_endpoint):
        src = inspect.getsource(fn)
        for name in ("_session_cost_breakdown(", "_session_cost_delta("):
            for line in src.splitlines():
                stripped = line.strip()
                if not stripped.startswith(name):
                    continue
                # Allowed only when off-thread.
                assert "to_thread" in stripped or "await" in stripped, (
                    f"{fn.__name__} calls {name} synchronously on the event "
                    f"loop: {stripped}")


def test_notify_on_run_completion_is_off_the_loop():
    """`notify_task_done` posts to the gateway with a 15s timeout, inline at
    the exact moment a run finishes."""
    src = inspect.getsource(agent_server._stream_run)
    assert "threading.Thread" in src and "notify_task_done" in src, (
        "the run-completion notification is back on the event loop")