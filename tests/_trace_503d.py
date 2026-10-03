import sqlite3, datetime
con = sqlite3.connect("../../llm_gatewayV9/gateway_v8.db")
con.row_factory = sqlite3.Row
# AI-summary turn: ts≈1787601152 (2026-08-25 ~01:19 local). Window ±5 min.
window_start = 1787601152 - 300
rows = con.execute(
    "SELECT ts, provider, model, status, error, agent, call_role "
    "FROM calls WHERE ts >= ? ORDER BY ts ASC LIMIT 80",
    (window_start,),
).fetchall()
print(f"calls in window: {len(rows)}")
for r in rows:
    err = str(r["error"] or "")[:110].replace("\n", " ")
    line = f"{r['provider']:14} agent={str(r['agent'] or '-'):11} role={str(r['call_role'] or '-'):10} {r['status']}"
    if err:
        line += f"\n      ERR: {err}"
    print(line)
