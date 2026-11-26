GOAL: {goal}

WINDOW: {app_name} (pid={pid})
CURRENT AX TREE (Markdown):
{tree_markdown}

Decide the next single action. Remember: indices are turn-scoped; only use
indices present in the tree above.

COST OPTIMIZATION (important):
- The AX tree above has ALREADY been pre-filtered to lines relevant to the
  goal (matching keywords). If you don't see an element, it may have been
  filtered out — in that case, escalate to vision rather than guessing.
- Prefer the cheapest path: if the goal is a simple read ("what does this
  say"), the engine may have already extracted it at Layer 1 (zero LLM cost).
  Only act if you're confident the content isn't already available.
- Avoid re-clicking the same element or re-typing the same text — the
  convergence guard will abort the run if you loop.