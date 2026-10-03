"""Replay report generator for a Session 9 browser run.

This is a VIEWER. It does not modify the orchestrator (flow.py) or the
Browser skill. It reads an existing session from state/sessions/<sid>/ and
the gateway's cost-by-agent ledger, then renders the 8-section report the
assignment requires:

  1. Original user goal
  2. Planner DAG
  3. Browser path chosen (extract / deterministic / a11y / vision / blocked)
  4. Browser actions taken
  5. Screenshots or page-state logs
  6. Extracted data
  7. Final comparison table
  8. Turn count and cost summary

Usage:
    uv run python report.py                 # latest session
    uv run python report.py <session_id>     # specific session
    uv run python report.py <sid> --md       # also write report_<sid>.md

Requires the V9 gateway running on :8109 (for the cost summary).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx
from gateway import GATEWAY_URL
from persistence import SessionStore

ROOT = Path(__file__).parent


def _find_sid(sid: str | None) -> str:
    if sid:
        return sid
    sessions = SessionStore("x").dir.parent
    if not sessions.exists():
        raise SystemExit("no sessions found under state/sessions/")
    latest = sorted(
        (p for p in sessions.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not latest:
        raise SystemExit("no sessions found under state/sessions/")
    return latest[0].name


def _load_graph(sid: str):
    # Validate through SessionStore (raises SessionLoadError with the file
    # path on corrupt data) instead of raw json.loads, then re-serialise
    # to the plain node-link dict form _dag_lines consumes.
    import networkx as _nx
    store = SessionStore(sid)
    try:
        g = store.read_graph()
    except Exception as e:
        print(f"[report] session graph failed validation: {e}")
        return None
    if g is None:
        return None
    data = _nx.node_link_data(g)
    data["links"] = data.pop("edges", [])
    return data


def _dag_lines(graph: dict | None) -> list[str]:
    if not graph:
        return ["(no graph.json for this session)"]
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    out = []
    for nid, n in nodes.items():
        skill = n.get("skill", "?")
        status = n.get("status", "?")
        out.append(f"  {nid} [{skill}] ({status})")
    out.append("")
    out.append("  edges:")
    for e in graph.get("links", []):
        out.append(f"    {e['source']} -> {e['target']}")
    return out


def _cost_summary(sid: str) -> dict:
    try:
        r = httpx.get(f"{GATEWAY_URL}/v1/cost/by_agent",
                      params={"session": sid}, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"_error": str(e)}


def _artifact_files(sid: str) -> list[Path]:
    base = ROOT / "state" / "sessions" / sid / "browser"
    if not base.exists():
        return []
    return sorted(base.rglob("*")) 


def _fmt_cost(cost: dict) -> list[str]:
    if "_error" in cost:
        return [f"  (cost ledger unavailable: {cost['_error']})",
                "  (fallback: turn count from node records below)"]
    lines = []
    tot_calls = 0
    tot_in = tot_out = 0
    for agent, rows in cost.items():
        if not isinstance(rows, list):
            continue
        c = sum(r.get("in_tok", 0) or 0 for r in rows)
        o = sum(r.get("out_tok", 0) or 0 for r in rows)
        tot_calls += len(rows)
        tot_in += c
        tot_out += o
        usd = sum(r.get("dollars", 0) or 0 for r in rows)
        lines.append(f"  {agent:14s} calls={len(rows):3d}  in={c:6d}  out={o:5d}  ${usd:.6f}")
    lines.append("")
    lines.append(f"  TOTAL calls={tot_calls}  tokens(in+out)={tot_in + tot_out} "
                 f"(${sum(sum(r.get('dollars',0) or 0 for r in rows) for rows in cost.values() if isinstance(rows, list)):.6f})")
    return lines


def build_report(sid: str) -> str:
    store = SessionStore(sid)
    goal = store.read_query() or "(no query.txt)"
    nodes = store.read_all_nodes()
    graph = _load_graph(sid)

    # Locate browser + formatter nodes.
    browser_node = None
    formatter_node = None
    for n in nodes:
        if n.skill == "browser" and browser_node is None:
            browser_node = n
        if n.skill == "formatter" and formatter_node is None:
            formatter_node = n

    b_out = (browser_node.result.output if browser_node and browser_node.result else {})
    path = b_out.get("path", "(no browser node)")
    actions = b_out.get("actions", []) or []
    extracted = b_out.get("content")
    final_url = b_out.get("final_url")
    browser_turns = b_out.get("turns", len(actions))

    cost = _cost_summary(sid)
    artifacts = _artifact_files(sid)

    # ── assemble ──
    L: list[str] = []
    bar = "=" * 78

    L.append(bar)
    L.append(f"SESSION REPLAY REPORT — {sid}")
    L.append(bar)

    # 1. goal
    L.append("\n1. ORIGINAL USER GOAL")
    L.append(f"   {goal}")

    # 2. DAG
    L.append("\n2. PLANNER DAG")
    L.extend(_dag_lines(graph))

    # 3. browser path
    L.append("\n3. BROWSER PATH CHOSEN")
    L.append(f"   path = {path}")
    if final_url:
        L.append(f"   final_url = {final_url}")
    L.append(f"   note: 'extract' = static HTML (0 browser turns); "
             f"'a11y'/'vision' = interactive cascade with real actions.")

    # 4. browser actions
    L.append("\n4. BROWSER ACTIONS TAKEN")
    if actions:
        for i, a in enumerate(actions, 1):
            turns = a.get("actions", [])
            L.append(f"   step {i}: turn {a.get('turn', '?')} -> "
                     + "; ".join(
                         f"{t.get('type')}"
                         + (f"({t.get('mark')})" if t.get('mark') is not None else "")
                         + (f"={t.get('value')}" if t.get('value') else "")
                         for t in turns)
                     + f"  [{a.get('outcome', '')}]")
    else:
        L.append("   (no recorded browser actions — path may have been 'extract' "
                 "or the run fell back to researcher before driving the page)")

    # 5. screenshots / page-state
    L.append("\n5. SCREENSHOTS / PAGE-STATE LOGS")
    if artifacts:
        for p in artifacts:
            rel = p.relative_to(ROOT)
            if p.is_file():
                L.append(f"   {rel}")
    else:
        L.append("   (no browser artifact directory for this session — "
                 "path was likely 'extract' or the node failed before driving)")

    # 6. extracted data
    L.append("\n6. EXTRACTED DATA")
    if extracted:
        L.append("   " + extracted.strip().replace("\n", "\n   "))
    else:
        L.append("   (no extracted content block — see formatter output / fallback below)")

    # 7. final comparison table
    L.append("\n7. FINAL COMPARISON TABLE")
    fa = (formatter_node.result.output.get("final_answer") if formatter_node
          and formatter_node.result else None)
    if fa:
        L.append("   " + fa.strip().replace("\n", "\n   "))
    else:
        L.append("   (no formatter final_answer captured)")

    # 8. turn count + cost
    L.append("\n8. TURN COUNT & COST SUMMARY")
    L.append(f"   browser turns (from BrowserOutput): {browser_turns}")
    L.append(f"   total graph nodes: {len(nodes)}")
    L.append("   cost by agent (gateway /v1/cost/by_agent):")
    L.extend(_fmt_cost(cost))

    L.append("\n" + bar)
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sid", nargs="?", default=None)
    ap.add_argument("--md", action="store_true", help="also write report_<sid>.md")
    args = ap.parse_args()

    sid = _find_sid(args.sid)
    report = build_report(sid)
    print(report)

    if args.md:
        out = ROOT / f"report_{sid}.md"
        out.write_text(report, encoding="utf-8")
        print(f"\n[report] wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
