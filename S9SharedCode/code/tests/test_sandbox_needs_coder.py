"""A plan that schedules `sandbox_executor` with no coder above it cannot work.

Live failure (conversation qa-conc-alpha, session s8-fda749d6), asked
"What is 25 * 4? Answer with just the number.":

    planner -> sandbox_executor -> formatter        (planner again -> executor
                                                     again -> planner again)

`sandbox_executor` takes the `code` string out of an upstream coder result, so
given only USER_QUERY there is nothing to run and it fails instantly with
"no code in upstream coder output" — recorded with `retries: 0`, `cost: 0.0` and
an empty result, so nothing downstream could tell why. The run re-planned twice
into the SAME impossible shape, spent 59.4 s and three planner calls, and
answered:

    "I am unable to provide an answer to your..."

The same question had answered `108` in 21 s on an earlier run, so this is
planner variance, not a capability gap. The graph never had to make that shape
legal, so it now corrects it: drop the orphan executor, let the formatter read
the query, answer.
"""
import pytest

flow = pytest.importorskip("flow")


def _orphan_plan():
    g = flow.Graph()
    ex = g.add_node("sandbox_executor", inputs=["USER_QUERY"])
    f = g.add_node("formatter", inputs=[ex])
    return g, ex, f


def test_an_executor_with_no_coder_is_removed():
    g, ex, f = _orphan_plan()
    removed = flow.ensure_sandbox_has_code(g)
    assert removed == [ex], removed
    assert ex not in g.g.nodes


def test_the_formatter_survives_and_reads_the_query():
    """The point of the guard: the run must still produce an answer."""
    g, ex, f = _orphan_plan()
    flow.ensure_sandbox_has_code(g)
    assert g.g.nodes[f]["inputs"] == ["USER_QUERY"]
    assert f in g.g.nodes


def test_a_coder_fed_executor_is_left_alone():
    g = flow.Graph()
    c = g.add_node("coder", inputs=["USER_QUERY"])
    ex = g.add_node("sandbox_executor", inputs=[c])
    f = g.add_node("formatter", inputs=[ex])
    assert flow.ensure_sandbox_has_code(g) == []
    assert ex in g.g.nodes
    assert g.g.nodes[ex]["inputs"] == [c]


def test_a_coder_further_upstream_still_counts():
    """Ancestors, not just direct predecessors."""
    g = flow.Graph()
    c = g.add_node("coder", inputs=["USER_QUERY"])
    d = g.add_node("distiller", inputs=[c])
    ex = g.add_node("sandbox_executor", inputs=[d])
    assert flow.ensure_sandbox_has_code(g) == []
    assert ex in g.g.nodes


def test_every_consumer_of_the_removed_executor_is_rewired():
    g = flow.Graph()
    q = g.add_node("planner", inputs=["USER_QUERY"])
    ex = g.add_node("sandbox_executor", inputs=[q])
    f1 = g.add_node("formatter", inputs=[ex])
    f2 = g.add_node("formatter", inputs=[ex, q])
    flow.ensure_sandbox_has_code(g)
    # The executor's own predecessor is inherited, so the chain stays alive.
    assert g.g.nodes[f1]["inputs"] == [q]
    assert g.g.nodes[f2]["inputs"] == [q]  # q not duplicated
    assert ex not in g.g.nodes


def test_a_consumer_left_with_nothing_reads_the_query():
    """`USER_QUERY` is not a graph node, so a lone orphan leaves no predecessor
    to inherit. Falling back to the question is what makes the corrected plan
    able to answer at all."""
    g = flow.Graph()
    ex = g.add_node("sandbox_executor", inputs=["USER_QUERY"])
    f = g.add_node("formatter", inputs=[ex])
    flow.ensure_sandbox_has_code(g)
    assert g.g.nodes[f]["inputs"] == ["USER_QUERY"]


def test_the_guard_is_idempotent():
    g, ex, f = _orphan_plan()
    assert flow.ensure_sandbox_has_code(g) == [ex]
    assert flow.ensure_sandbox_has_code(g) == []


def test_correction_is_applied_when_the_planner_node_completes():
    """Wire-up check: the guard must run on the real executor path.

    A structural rule nobody calls is a function and a bug at the same time —
    the live run re-planned twice, so the hook belongs where a COMPLETED planner
    node's successors land in the graph.
    """
    import inspect

    src = inspect.getsource(flow.Executor.run)
    assert "ensure_sandbox_has_code(" in src, (
        "the orphan-executor guard is defined but never called")
