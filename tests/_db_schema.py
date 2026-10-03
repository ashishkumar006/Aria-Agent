import sqlite3
con = sqlite3.connect("../../llm_gatewayV9/gateway_v9.db")
tables = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print("tables:", [t[0] for t in tables])
for (t,) in tables:
    cols = con.execute(f"PRAGMA table_info({t})").fetchall()
    print(f"  {t}: {[c[1] for c in cols]}")
