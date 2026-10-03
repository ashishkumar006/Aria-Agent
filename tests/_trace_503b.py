import sqlite3, time
con = sqlite3.connect("../../llm_gatewayV9/gateway_v9.db")
con.row_factory = sqlite3.Row
# AI-summary turn started at ts≈1787601152; window covers it plus retries
window_start = 1787601152 - 120
rows = con.execute(
    "SELECT ts, provider, model, status, error, agent, call_role "
    "FROM calls WHERE ts >= ? ORDER BY ts ASC LIMIT 60",
    (window_start,),
).fetchall()
print(f"calls in window: {len(rows)}")
for r in rows:
    err = str(r["error"] or "")[:100]
    line = f"  {r['provider']:14} agent={str(r['agent'] or '-'):12} role={str(r['call_role'] or '-'):10} status={r['status']}"
    if err:
        line += f"\n      ERR: {err}"
    print(line)
