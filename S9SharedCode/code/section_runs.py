"""Direct execution of one DAG skill for a console section.

Some section actions are deterministic: the Apps "Build app" button has a
builder that already knows how to design and validate a surface. Running the
planner first would add an LLM call without improving the result. This helper
still executes through the DAG's skill/tool layer and persists the run, so the
work is traceable and uses the same skill registry and tool grants as a
planned run - it simply skips the planner for a single known node.

This is deliberately not a second agent loop. The skill's own tools and the
builder call do the work; the helper only supplies the graph context.
"""
from __future__ import annotations

import time
import uuid

from schemas import AgentResult


class SectionRunError(RuntimeError):
    def __init__(self, message: str, session_id: str = ""):
        super().__init__(message)
        self.session_id = session_id


async def run_skill_direct(skill_name: str, query: str, *,
                           run_kind: str,
                           metadata: dict | None = None,
                           session_id: str | None = None
                           ) -> tuple[AgentResult, str]:
    """Run one skill as a one-node DAG run and persist its trace."""
    from skills import SkillRegistry, run_skill
    from persistence import SessionStore

    sid = session_id or f"{run_kind}-{uuid.uuid4().hex[:8]}"
    store = SessionStore(sid)
    store.write_query(query)
    try:
        store.write_run_meta({"kind": run_kind})
    except Exception:
        pass
    registry = SkillRegistry()
    skill = registry.get(skill_name)
    node_id = "n:1"
    meta = dict(metadata or {})
    nodes = {
        node_id: {
            "skill": skill_name,
            "inputs": ["USER_QUERY"],
            "metadata": meta,
            "status": "pending",
        },
    }
    started = time.time()
    result, rendered = await run_skill(
        skill, node_id, nodes, sid, query, None,
    )
    # A minimal, real graph. The Runs page can show this action even though
    # there was no planner: one node, its skill, its outcome and timing.
    try:
        import networkx as nx
        graph = nx.DiGraph()
        graph.add_node(
            node_id,
            skill=skill_name,
            inputs=["USER_QUERY"],
            metadata=meta,
            status="complete" if result.success else "failed",
            result=result,
            started_at=started,
            completed_at=time.time(),
            elapsed_s=result.elapsed_s,
        )
        store.write_graph(graph)
        try:
            from schemas import NodeState
            store.write_node(NodeState(
                node_id=node_id,
                skill=skill_name,
                status="complete" if result.success else "failed",
                inputs=["USER_QUERY"],
                prompt_sent=rendered[:20000],
                result=result,
                started_at=started,
                completed_at=time.time(),
            ))
        except Exception:
            pass
    except Exception:
        # The trace is useful, never required for the action itself.
        pass
    return result, sid


async def require_success(skill_name: str, query: str, *, run_kind: str,
                          metadata: dict | None = None,
                          session_id: str | None = None) -> tuple[dict, str]:
    """Like `run_skill_direct`, but raise the skill's error on failure."""
    result, sid = await run_skill_direct(
        skill_name, query, run_kind=run_kind, metadata=metadata,
        session_id=session_id)
    if not result.success:
        raise SectionRunError(
            result.error or f"{skill_name} failed", session_id=sid)
    out = result.output if isinstance(result.output, dict) else {}
    return out, sid
