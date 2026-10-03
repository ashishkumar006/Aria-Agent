import sqlite3
con = sqlite3.connect("../../llm_gatewayV9/gateway_v8.db")
con.row_factory = sqlite3.Row
# Find the 503s specifically in the whole recent window (last 6h)
cutoff = 1787601152 - 21600
rows = con.execute(
    "SELECT ts, provider, status, error, agent FROM calls "
    "WHERE ts >= ? AND status='error' ORDER BY ts ASC",
    (cutoff,),
).fetchall()
print(f"errors in last 6h: {len(rows)}")
for r in rows:
    err = str(r["error"] or "")[:130].replace("\n", " ")
    t = datetime.datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S")
    print(f"  {t} {r['provider']:14} agent={str(r['agent'] or '-'):11} {err}")
