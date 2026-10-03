import sqlite3
con = sqlite3.connect("../../llm_gatewayV9/gateway_v9.db")
con.row_factory = sqlite3.Row
row = con.execute("SELECT MIN(ts) as mn, MAX(ts) as mx, COUNT(*) as n FROM calls").fetchone()
print(f"calls: n={row['n']} min_ts={row['mn']} max_ts={row['mx']}")
import datetime
if row["mx"]:
    print("latest:", datetime.datetime.fromtimestamp(row["mx"]))
# errors overall
errs = con.execute(
    "SELECT provider, status, error, ts FROM calls WHERE status='error' ORDER BY ts DESC LIMIT 15"
).fetchall()
print(f"\nrecent errors: {len(errs)}")
for r in errs:
    print(f"  {r['provider']:14} | {str(r['error'] or '')[:100]}")
