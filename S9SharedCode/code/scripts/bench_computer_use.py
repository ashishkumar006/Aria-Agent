"""Regenerate the BENCHMARKS.md tables from captured session traces.

Walks ``state/sessions/s8-*/graph.json`` (and any ``s8-*`` graph under the
configured root), aggregates the same tables BENCHMARKS.md reports:

  * per-skill latency (n / ok / fail / mean / p50 / p90 / max)
  * computer-skill layer distribution (where attempts ended)
  * computer-session outcomes (complete / failed / none / running / skipped)

Run:
  python scripts/bench_computer_use.py            # print markdown
  python scripts/bench_computer_use.py --root state/sessions --json

This is a measurement tool only — it does not edit source files. The numbers
are derived directly from the captured ``graph.json`` / ``nodes/*.json`` files
so the benchmarks stop rotting when the engine changes.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _iter_session_graphs(root: Path):
    """Yield (session_id, graph_dict) for every s8-* graph under root."""
    if not root.exists():
        return
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not d.name.startswith("s8-"):
            continue
        g = d / "graph.json"
        if not g.exists():
            continue
        try:
            yield d.name, json.loads(g.read_text(encoding="utf-8"))
        except Exception:
            continue


def _collect_nodes(graph: dict) -> list[dict]:
    """Flatten the graph's node list (handles both list and nested shapes)."""
    nodes = graph.get("nodes") or []
    out: list[dict] = []
    for n in nodes:
        if isinstance(n, dict):
            out.append(n)
        # Some graphs nest sub-graphs under a "graph" key.
        sub = n.get("graph") if isinstance(n, dict) else None
        if isinstance(sub, dict):
            out.extend(_collect_nodes(sub))
    return out


def _node_skill(n: dict) -> str | None:
    return (n.get("skill") or n.get("agent_name")
            or (n.get("result") or {}).get("agent_name"))


def _node_status(n: dict) -> str:
    return (n.get("status") or "").lower()


def _node_elapsed(n: dict) -> float | None:
    md = n.get("metadata") or {}
    for key in ("elapsed_s", "elapsed", "duration_s"):
        v = md.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    res = n.get("result") or {}
    v = res.get("elapsed_s")
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _computer_layer(n: dict) -> str | None:
    """Best-effort extraction of the computer layer a node ended on."""
    res = n.get("result") or {}
    # ComputerResult exposes .layer; traces may store it under result.layer.
    layer = res.get("layer")
    if isinstance(layer, str):
        return layer
    # Fall back to status-like markers in metadata.
    md = n.get("metadata") or {}
    for cand in ("layer", "computer_layer", "end_layer"):
        if isinstance(md.get(cand), str):
            return md[cand]
    return None


def aggregate(root: Path) -> dict:
    skill_stats: dict[str, list[float]] = defaultdict(list)
    skill_ok = Counter()
    skill_fail = Counter()
    computer_layers = Counter()
    outcomes = Counter()
    total_skill_nodes = 0
    computer_invocations = 0
    sessions_touching_computer = 0

    for sid, graph in _iter_session_graphs(root):
        nodes = _collect_nodes(graph)
        saw_computer = False
        for n in nodes:
            skill = _node_skill(n)
            if not skill:
                continue
            total_skill_nodes += 1
            elapsed = _node_elapsed(n)
            if elapsed is not None:
                skill_stats[skill].append(elapsed)
            st = _node_status(n)
            if st in ("complete", "ok", "success"):
                skill_ok[skill] += 1
            elif st in ("fail", "failed", "error"):
                skill_fail[skill] += 1
            if skill == "computer":
                saw_computer = True
                computer_invocations += 1
                layer = _computer_layer(n)
                if layer:
                    computer_layers[layer] += 1
                else:
                    computer_layers["(unknown)"] += 1
        if saw_computer:
            sessions_touching_computer += 1
        # Session-level outcome: prefer an explicit "outcome" field, else
        # derive from whether any computer node completed.
        outcome = (graph.get("outcome") or graph.get("status") or "").lower()
        if not outcome:
            comp = [n for n in nodes if _node_skill(n) == "computer"]
            if not comp:
                outcome = "none"
            elif any(_node_status(n) in ("complete", "ok", "success") for n in comp):
                outcome = "complete"
            elif any(_node_status(n) in ("running", "interrupted") for n in comp):
                outcome = "running"
            elif any(_node_status(n) in ("skip", "skipped") for n in comp):
                outcome = "skipped"
            else:
                outcome = "failed"
        outcomes[outcome or "none"] += 1

    return {
        "sessions": len(list(_iter_session_graphs(root))),
        "total_skill_nodes": total_skill_nodes,
        "computer_invocations": computer_invocations,
        "sessions_touching_computer": sessions_touching_computer,
        "skill_stats": {k: v for k, v in skill_stats.items()},
        "skill_ok": dict(skill_ok),
        "skill_fail": dict(skill_fail),
        "computer_layers": dict(computer_layers),
        "outcomes": dict(outcomes),
    }


def _fmt_latency_row(skill: str, vals: list[float], ok: int, fail: int) -> str:
    n = len(vals)
    if n == 0:
        return f"| {skill} | 0 | {ok} | {fail} | - | - | - | - | $0.000 |"
    mean = statistics.mean(vals)
    p50 = _percentile(vals, 50)
    p90 = _percentile(vals, 90)
    mx = max(vals)
    return (f"| {skill} | {n} | {ok} | {fail} | {mean:.2f}s | {p50:.2f}s | "
            f"{p90:.2f}s | {mx:.2f}s | $0.000 |")


def render_markdown(agg: dict) -> str:
    lines = ["# Computer-Use Engine — Benchmarks (regenerated)", ""]
    lines.append(f"- Sessions analyzed: **{agg['sessions']}**")
    lines.append(f"- Total skill nodes: **{agg['total_skill_nodes']}**")
    lines.append(f"- Computer-skill invocations: **{agg['computer_invocations']}**")
    lines.append(f"- Sessions touching computer skill: "
                 f"**{agg['sessions_touching_computer']}**")
    lines.append("")

    lines.append("## Per-skill latency (from node elapsed_s)")
    lines.append("| Skill | n | ok | fail | mean | p50 | p90 | max | cost |")
    lines.append("|-------|---|----|------|------|-----|-----|-----|------|")
    for skill in sorted(agg["skill_stats"], key=lambda s: -len(agg["skill_stats"][s])):
        lines.append(_fmt_latency_row(
            skill, agg["skill_stats"][skill],
            agg["skill_ok"].get(skill, 0), agg["skill_fail"].get(skill, 0)))
    lines.append("")

    lines.append("## Computer-skill layer distribution")
    lines.append("| Layer reached | Count |")
    lines.append("|---------------|-------|")
    for layer, cnt in sorted(agg["computer_layers"].items(), key=lambda x: -x[1]):
        lines.append(f"| {layer} | {cnt} |")
    lines.append("")

    lines.append("## Computer-session outcomes")
    lines.append("| Outcome | Sessions |")
    lines.append("|---------|----------|")
    for outcome, cnt in sorted(agg["outcomes"].items(), key=lambda x: -x[1]):
        lines.append(f"| {outcome} | {cnt} |")
    lines.append("")

    comp = agg["computer_invocations"]
    done = agg["outcomes"].get("complete", 0)
    if comp:
        lines.append(f"Baseline computer-session completion rate: "
                     f"**{done / agg['sessions'] * 100:.1f}%** "
                     f"({done}/{agg['sessions']}).")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="state/sessions",
                    help="directory containing s8-* session folders")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_absolute():
        # Resolve relative to the repo root (parent of scripts/).
        root = Path(__file__).resolve().parent.parent / args.root
    agg = aggregate(root)
    if args.json:
        print(json.dumps(agg, indent=2))
    else:
        print(render_markdown(agg))


if __name__ == "__main__":
    main()
