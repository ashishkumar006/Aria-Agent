"""On-disk persistence for the growing graph (Session 8 origin, live in S9).

The surfaces:

  - SessionStore: per-session directory under state/sessions/<sid>/.
    Owns reading and writing the graph JSON and the per-node JSON
    files. Atomic-write semantics (write to tmp, rename) so a SIGKILL
    mid-write does not corrupt the last successful snapshot.
    (Legacy graph.pkl files are still readable for old sessions.)

The Graph itself (the NetworkX wrapping) lives in flow.py.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

import networkx as nx

from schemas import AgentResult, NodeState

import re as _re

# Overridable so tests can point at a tmp dir instead of the live store
# (S9_STATE_DIR=<tmp>/state makes SESSIONS_ROOT land inside it).
_STATE_DIR = Path(os.environ.get("S9_STATE_DIR") or (Path(__file__).parent / "state"))
SESSIONS_ROOT = _STATE_DIR / "sessions"

# Session/node ids are minted as s8-<hex>, ct-*, c-*, t-*, L0-* etc. Reject
# anything else so a crafted id can never escape SESSIONS_ROOT (path
# traversal) or create junk directories on a typo.
_ID_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,64}$")


def _check_id(kind: str, value: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(f"invalid {kind}: {value!r}")
    return value


class SessionLoadError(RuntimeError):
    """Raised when a persisted session cannot be safely loaded.

    Examples: a NodeState file that no longer matches the schema, a
    `_result_typed` payload that cannot round-trip back into an
    AgentResult. We fail loud here rather than silently degrade — the
    Executor's downstream code does `isinstance(..., AgentResult)`
    checks, and stashing a dict where it expects a Pydantic model is
    exactly the silent-degradation pattern review round-3 #4 flagged."""


def _atomic_write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp name (not a fixed "<name>.tmp"): two concurrent writers to
    # the same session (double-sent turns, recovery replan racing the main
    # loop) must never share a tmp file, and on Windows os.replace fails
    # when the destination tmp is held open by another writer/antivirus.
    # Same discipline as turnlog.py and the gateway memory store.
    tmp = path.with_name(
        f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"
    )
    if isinstance(data, bytes):
        with open(tmp, "wb") as f:
            f.write(data)
    else:
        # Always write text as UTF-8 so non-ASCII queries (e.g. the rupee
        # sign) don't crash on Windows' default cp1252 console encoding.
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
    os.replace(tmp, path)


class SessionStore:
    """One on-disk session. Layout:

        state/sessions/<sid>/
            graph.json             # NetworkX DiGraph (node_link_data)
            query.txt              # the user's verbatim query
            turn_costs.json        # per-turn cost ledger (agent_server)
            browser/               # browser skill artifacts (screenshots)
            nodes/
                n_001.json         # NodeState for the n:1 node, etc.
                n_002.json
                ...
    """

    def __init__(self, session_id: str, create: bool = True):
        # Validate BEFORE touching the filesystem: reads must never mkdir
        # (create=False) and hostile ids must never escape SESSIONS_ROOT.
        self.session_id = _check_id("session_id", session_id)
        self.dir = SESSIONS_ROOT / self.session_id
        self.nodes_dir = self.dir / "nodes"
        if create:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.nodes_dir.mkdir(parents=True, exist_ok=True)

    @property
    def query_path(self) -> Path:
        return self.dir / "query.txt"

    @property
    def graph_path(self) -> Path:
        # P1 #6: graph is persisted as JSON via nx.node_link_data so the file
        # is `cat`-able by students and the format survives a Python upgrade.
        return self.dir / "graph.json"

    @property
    def _legacy_graph_path(self) -> Path:
        # Older sessions wrote pickle; the loader tolerates that for resume
        # on pre-fix sessions but the writer always emits JSON now.
        return self.dir / "graph.pkl"

    def write_query(self, query: str) -> None:
        _atomic_write(self.query_path, query)

    def read_query(self) -> str:
        if not self.query_path.exists():
            return ""
        return self.query_path.read_text(encoding="utf-8-sig")

    @property
    def plan_path(self) -> Path:
        """The planner's structured plan, when it emitted one.

        The topic used to be recoverable only by regexing the researcher's
        system prompt out of query.txt, which is why run titles read "You are
        a research agent...". The planner may now emit a `research_plan`
        block; storing it as its own file makes the topic, facets and source
        hints first-class for the UI and for routing, and keeps working when
        the prompt wording changes.
        """
        return self.dir / "plan.json"

    def write_plan(self, plan: dict) -> None:
        _atomic_write(self.plan_path, json.dumps(plan, indent=2, default=str))

    def read_plan(self) -> dict:
        if not self.plan_path.exists():
            return {}
        try:
            blob = json.loads(self.plan_path.read_text(encoding="utf-8-sig"))
            return blob if isinstance(blob, dict) else {}
        except Exception:
            return {}

    def write_graph(self, graph_obj: nx.DiGraph) -> None:
        """Serialise the DiGraph to JSON via nx.node_link_data. Per-node
        `result` is an AgentResult (Pydantic) — dump it to a dict so the
        JSON encoder is happy. Reviving on read restores the Pydantic shape.

        NetworkX 3.x uses keyword-only `edges="edges"` (the old 2.x
        `attrs={"link": "links"}` form is gone — passing `link=` raises
        TypeError). Files are therefore written with the `"edges"` key;
        read_graph() still tolerates legacy `"links"` files.
        """
        # node_link_data accepts arbitrary node-attr dicts; we just need
        # every value to be JSON-serialisable.
        h = nx.DiGraph()
        for n, d in graph_obj.nodes(data=True):
            attrs = dict(d)
            if isinstance(attrs.get("result"), AgentResult):
                attrs["result"] = attrs["result"].model_dump(mode="json")
                attrs["_result_typed"] = True
            h.add_node(n, **attrs)
        for u, v, d in graph_obj.edges(data=True):
            h.add_edge(u, v, **d)
        payload = nx.node_link_data(h)
        _atomic_write(self.graph_path, json.dumps(payload, indent=2, default=str))

    def read_graph(self) -> nx.DiGraph | None:
        if self.graph_path.exists():
            payload = json.loads(self.graph_path.read_text(encoding="utf-8-sig"))
            # Tolerate legacy files written with the `"links"` edge-list key
            # (NetworkX 2.x era): rewrite to `"edges"` before handing to
            # node_link_graph, whose 3.x default is `edges="edges"`.
            if "links" in payload and "edges" not in payload:
                payload = {**payload, "edges": payload.pop("links")}
            g = nx.node_link_graph(payload, directed=True)
            # NOTES_RUNS round-3 review #4: a write tagged a node's `result`
            # as a typed AgentResult via `_result_typed`. If the dict no
            # longer round-trips through AgentResult.model_validate, that
            # is silent data corruption — the previous "keep the dict, let
            # downstream isinstance checks handle it" was exactly the
            # silent-degradation pattern we just fixed in P0 #2.
            # Raise instead; the SessionLoadError surfaces the bad file path
            # and the validation message so the operator can act on it.
            for nid, d in g.nodes(data=True):
                if d.pop("_result_typed", False) and isinstance(d.get("result"), dict):
                    try:
                        d["result"] = AgentResult.model_validate(d["result"])
                    except (ValueError, TypeError) as e:
                        raise SessionLoadError(
                            f"node {nid} in {self.graph_path}: persisted "
                            f"AgentResult failed model_validate. The graph "
                            f"is unsafe to resume — inspect the file and "
                            f"either repair it or delete the session. "
                            f"validation error: {type(e).__name__}: {e}"
                        ) from e
            return g
        # Backwards-compat: tolerate sessions written by the pre-P1 pickle
        # path. We import pickle lazily so the dependency is only paid when
        # someone resumes a legacy session.
        if self._legacy_graph_path.exists():
            import pickle, sys
            print(f"[persistence] reading legacy pickle graph from "
                  f"{self._legacy_graph_path}", file=sys.stderr)
            return pickle.loads(self._legacy_graph_path.read_bytes())
        return None

    def _node_path(self, node_id: str) -> Path:
        # node_id is like "n:1" — turn that into n_001.json so directory
        # listings sort sensibly.
        try:
            i = int(node_id.split(":", 1)[1])
            return self.nodes_dir / f"n_{i:03d}.json"
        except (IndexError, ValueError):
            safe = node_id.replace(":", "_").replace("/", "_")
            return self.nodes_dir / f"{safe}.json"

    def write_node(self, state: NodeState) -> None:
        _atomic_write(self._node_path(state.node_id), state.model_dump_json(indent=2))

    def read_node(self, node_id: str) -> NodeState | None:
        p = self._node_path(node_id)
        if not p.exists():
            return None
        return NodeState.model_validate_json(p.read_text(encoding="utf-8-sig"))

    def read_all_nodes(self) -> list[NodeState]:
        """Load every persisted NodeState in this session. Corrupt or
        partially-written files (the typical cause is a process kill between
        the temp-file write and the atomic rename) are skipped with a clear
        warning to stderr — never silently dropped. NOTES_RUNS feedback
        P0 #2: a bare `except Exception: continue` here was killing resume
        invisibly when one node file was bad."""
        import sys
        states: list[NodeState] = []
        for p in sorted(self.nodes_dir.glob("n_*.json")):
            try:
                states.append(NodeState.model_validate_json(p.read_text(encoding="utf-8-sig")))
            except (OSError, ValueError) as e:
                # OSError = unreadable; ValueError covers JSON decode +
                # Pydantic ValidationError (which inherits ValueError).
                print(f"[persistence] WARNING: skipped corrupt node file "
                      f"{p}: {type(e).__name__}: {e}", file=sys.stderr)
        return states


def list_sessions() -> list[str]:
    if not SESSIONS_ROOT.exists():
        return []
    return sorted(p.name for p in SESSIONS_ROOT.iterdir() if p.is_dir())
