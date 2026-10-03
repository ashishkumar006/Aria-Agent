"""Find where the 503s the agent saw actually came from: they appear in the
AGENT's log as gateway responses, but the gateway DB shows all-ok. Check the
gateway's own stdout terminal output vs DB, and search for 503 in agent log.
The classifier calls use auto_route='memory' -> router pool; a 503 from the
ROUTER POOL (not worker) is returned when all router providers are cooling
down. Those may not be logged as 'calls' rows."""
import sqlite3
con = sqlite3.connect("../../llm_gatewayV9/gateway_v8.db")
con.row_factory = sqlite3.Row
# Count by call_role in last 6h
cutoff = 1787601152 - 21600
rows = con.execute(
    "SELECT call_role, status, COUNT(*) as n FROM calls "
    "WHERE ts >= ? GROUP BY call_role, status ORDER BY n DESC",
    (cutoff,),
).fetchall()
print("by role/status (last 6h):")
for r in rows:
    print(f"  {str(r['call_role']):16} {r['status']:8} {r['n']}")

# Router-pool providers: cerebras/groq/nvidia/github/kilo with role=router_*
print("\nrouter_memory calls by provider:")
rows = con.execute(
    "SELECT provider, status, COUNT(*) as n FROM calls "
    "WHERE ts >= ? AND call_role LIKE 'router%' GROUP BY provider, status",
    (cutoff,),
).fetchall()
for r in rows:
    print(f"  {r['provider']:14} {r['status']:8} {r['n']}")
