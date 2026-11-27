# Policy (`policy/`)

The declarative policy engine that gates every tool call and channel action. Pure
logic, no network, fully unit-tested. First matching rule wins; ties resolve to
**deny**.

## Files
| File | Responsibility |
|---|---|
| `engine.py` | The evaluator. Request shape: `{trust, tool, channel, agent, session, spend_usd, over_spend_cap, destructive}` → verdict. |
| `policy.yaml` | The ruleset (version, defaults, spend caps, rules). Reloads automatically when edited. |
| `test_policy.py` | Unit tests for the evaluator. |

## Evaluation
- **First match wins**; no match → `default_action` (deny).
- **Dry-run** (global `defaults.dry_run: true` or per-rule `enforce: false`): the
  verdict is reported but the caller is told `allowed=true, dry_run=true`, so
  operators observe before arming.
- **Ties → deny.**

## Defaults (`policy.yaml`)
- `dry_run: true` (report-only) and `default_action: deny`.
- Spend caps: `per_call: $0.50`, `per_agent_per_day: $5.00`,
  `per_session_per_day: $10.00`.
- Rules: `owner-allow` (owner does anything, still logged + capped);
  `paired-messaging` (messaging/reads for paired); channel ops for paired/owner;
  a final catch-all that denies all tools for `untrusted`.

## Notes
- Trust comes from `../adaptors/trust.py` (owner / paired / untrusted).
- Channel tools dispatch as `<service>_<op>` (e.g. `gmail_send`,
  `calendar_list`); keep the rule tool-ids in sync with the real op names or they
  fall through to default-deny.