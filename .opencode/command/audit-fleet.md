---
description: Run the full audit fleet in parallel and merge the findings into one deduplicated, severity-ranked report.
agent: build
---

Audit this codebase with the whole review fleet, in parallel, then merge the
results.

Scope: $ARGUMENTS

Dispatch these subagents concurrently with the task tool, each told to focus on
its own discipline and to report findings with `file:line` evidence and a
concrete trigger:

1. `architect-auditor` — the overall pass: deep bug hunt plus modernization
   proposals. Do not skip it even though the others overlap; it is the
   integrator's baseline.
2. `bug-hunter` — logic flaws, state invariants, edge cases.
3. `concurrency-auditor` — races, async correctness, resource lifetime.
4. `security-auditor` — authn/authz, injection, SSRF, traversal, secrets,
   cost amplification.
5. `reliability-auditor` — timeouts, degradation, retry correctness, blast
   radius, state after failure.
6. `persistence-auditor` — atomicity, schema integrity, growth, coherence.
7. `performance-auditor` — latency, N+1s, caching, LLM cost.
8. `agent-systems-auditor` — prompts, tool contracts, DAG design, evals.
9. `frontend-auditor` — React async races, streaming rendering, a11y, bundle.
10. `api-contract-auditor` — endpoint semantics, SSE contracts, server/client
    field mismatches.
11. `test-auditor` — untested invariants, weak assertions, flaky tests.
12. `dependency-auditor` — lockfiles, pinning, build reproducibility, secrets.
13. `docs-truth-auditor` — claims the code contradicts.

Tell every agent: read `AGENTS.md` first, never read `.env` files, and do not
report formatting or naming.

When all reports are back, merge them yourself and produce ONE report:

- **Deduplicate** by root cause, not by wording. Twelve agents reporting the
  same missing lock is one finding with twelve witnesses — cite the
  independent confirmations, because agreement across specialists is itself
  evidence of severity.
- **Rank by severity then by blast radius.** Critical first.
- For each merged finding: title, severity, every `file:line` site, the
  trigger, the impact, the fix, and which agents confirmed it.
- Separate **must fix before shipping** from **should fix** from
  **modernization backlog**.
- Close with: the top three changes that would most improve correctness, the
  top three that would most improve reliability, and the single highest-value
  missing test.
- Include a short **disagreements** section where two auditors concluded
  differently, and say which one you believe and why.

Be dense. No filler, no praise, no restating the code. If the fleet finds
nothing critical, say so plainly — that is a real and useful result.
