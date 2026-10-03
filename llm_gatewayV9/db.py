import sqlite3, time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = str(Path(__file__).parent / "gateway_v8.db")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_create_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            latency_ms INTEGER DEFAULT 0,
            status TEXT,
            error TEXT,
            prompt_chars INTEGER DEFAULT 0,
            response_chars INTEGER DEFAULT 0,
            override TEXT,
            attempted TEXT,
            tool_calls INTEGER DEFAULT 0,
            reasoning_applied INTEGER DEFAULT 0,
            tool_dialect TEXT,
            call_role TEXT DEFAULT 'worker',
            router_decision TEXT,
            embed_dim INTEGER,
            agent TEXT,
            session TEXT,
            retries INTEGER DEFAULT 0,
            channel TEXT,
            trust_level TEXT,
            policy_verdict TEXT,
            policy_rule TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ts ON calls(ts DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_prov_ts ON calls(provider, ts DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_role_ts ON calls(call_role, ts DESC)")
        # Backwards-compatible upgrades — add new columns if an older
        # database file is being reused (same pattern as the V8 columns).
        cols = {r["name"] for r in c.execute("PRAGMA table_info(calls)").fetchall()}
        if "embed_dim" not in cols:
            c.execute("ALTER TABLE calls ADD COLUMN embed_dim INTEGER")
        if "agent" not in cols:
            c.execute("ALTER TABLE calls ADD COLUMN agent TEXT")
        if "session" not in cols:
            c.execute("ALTER TABLE calls ADD COLUMN session TEXT")
        if "retries" not in cols:
            c.execute("ALTER TABLE calls ADD COLUMN retries INTEGER DEFAULT 0")
        # V10 (adaptors): channel + trust + policy verdict per call.
        if "channel" not in cols:
            c.execute("ALTER TABLE calls ADD COLUMN channel TEXT")
        if "trust_level" not in cols:
            c.execute("ALTER TABLE calls ADD COLUMN trust_level TEXT")
        if "policy_verdict" not in cols:
            c.execute("ALTER TABLE calls ADD COLUMN policy_verdict TEXT")
        if "policy_rule" not in cols:
            c.execute("ALTER TABLE calls ADD COLUMN policy_rule TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS idx_agent_ts ON calls(agent, ts DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_session_ts ON calls(session, ts DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_channel_ts ON calls(channel, ts DESC)")
        # Tool ledger: one row per completed model tool call (names the
        # `calls.tool_calls` counts could never identify). Created alongside
        # so old databases upgrade silently on next start.
        c.execute("""CREATE TABLE IF NOT EXISTS tool_uses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            tool TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT,
            agent TEXT,
            session TEXT,
            call_role TEXT DEFAULT 'worker',
            status TEXT DEFAULT 'ok'
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tool_ts ON tool_uses(tool, ts DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_toolprov_ts ON tool_uses(provider, ts DESC)")


def log_call(provider, model, input_tokens=0, output_tokens=0, latency_ms=0,
             status="ok", error=None, prompt_chars=0, response_chars=0,
             override=None, attempted=None,
             cache_create_tokens=0, cache_read_tokens=0,
             tool_calls=0, reasoning_applied=False, tool_dialect=None,
             call_role="worker", router_decision=None, embed_dim=None,
             agent=None, session=None, retries=0,
             channel=None, trust_level=None, policy_verdict=None,
             policy_rule=None):
    with conn() as c:
        c.execute(
            """INSERT INTO calls (ts, provider, model, input_tokens, output_tokens,
                                  cache_create_tokens, cache_read_tokens,
                                  latency_ms, status, error, prompt_chars, response_chars,
                                  override, attempted, tool_calls, reasoning_applied, tool_dialect,
                                  call_role, router_decision, embed_dim,
                                  agent, session, retries,
                                  channel, trust_level, policy_verdict, policy_rule)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), provider, model, input_tokens, output_tokens,
             cache_create_tokens, cache_read_tokens, latency_ms,
             status, error, prompt_chars, response_chars,
             override, attempted, tool_calls, 1 if reasoning_applied else 0, tool_dialect,
             call_role, router_decision, embed_dim,
             agent, session, retries,
             channel, trust_level, policy_verdict, policy_rule),
        )


def log_tool_use(tool, provider, model=None, agent=None, session=None,
                 call_role="worker", status="ok"):
    """Record one completed model tool call. Names are model-emitted
    catalog names (capped) — never arguments, which may carry secrets."""
    with conn() as c:
        c.execute(
            """INSERT INTO tool_uses (ts, tool, provider, model, agent,
                                      session, call_role, status)
               VALUES (?,?,?,?,?,?,?,?)""",
            (time.time(), str(tool or "tool")[:200], provider, model,
             agent, session, call_role, status),
        )


def tool_usage(since=None):
    """Per-tool completions since calendar day (or `since` epoch):
    [{tool, uses, ok, errors, providers, agents, last_ts}]."""
    day_start = since if since is not None else (time.time() - (time.time() % 86400))
    q = """SELECT tool,
                  COUNT(*) AS uses,
                  SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok,
                  SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
                  GROUP_CONCAT(DISTINCT provider) AS providers,
                  GROUP_CONCAT(DISTINCT agent) AS agents,
                  MAX(ts) AS last_ts
             FROM tool_uses WHERE ts >= ?
             GROUP BY tool ORDER BY uses DESC"""
    with conn() as c:
        out = []
        for r in c.execute(q, [day_start]).fetchall():
            d = dict(r)
            d["providers"] = (d["providers"] or "").split(",") if d["providers"] else []
            d["agents"] = [a for a in ((d["agents"] or "").split(",") if d["agents"] else []) if a]
            out.append(d)
        return out


def by_agent(session=None, since=None):
    """V8: per-agent cost/token rollup. When `session` is set, scopes the
    rollup to a single flow-run; otherwise rolls up the calendar day."""
    where = ["ts >= ?"]
    args = [since if since is not None else (time.time() - (time.time() % 86400))]
    if session:
        where.append("session=?"); args.append(session)
    q = (
        "SELECT agent, provider, COUNT(*) AS calls, "
        "SUM(input_tokens) AS in_tok, SUM(output_tokens) AS out_tok, "
        "SUM(latency_ms) AS total_latency_ms, "
        "SUM(retries) AS total_retries, "
        "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok, "
        "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors "
        "FROM calls WHERE " + " AND ".join(where) + " AND agent IS NOT NULL "
        "GROUP BY agent, provider"
    )
    with conn() as c:
        rows = c.execute(q, args).fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["agent"], []).append(dict(r))
        return out


def recent(limit=100, provider=None, status=None):
    q = "SELECT * FROM calls"
    where, args = [], []
    if provider:
        where.append("provider=?"); args.append(provider)
    if status:
        where.append("status=?"); args.append(status)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def aggregate(call_role=None):
    now = time.time()
    day_start = now - (now % 86400)
    q = """SELECT provider,
                  COUNT(*) AS calls,
                  SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_calls,
                  SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
                  SUM(input_tokens) AS in_tok,
                  SUM(output_tokens) AS out_tok,
                  SUM(cache_read_tokens) AS cache_reads,
                  SUM(cache_create_tokens) AS cache_creates,
                  SUM(tool_calls) AS tool_calls,
                  AVG(latency_ms) AS avg_latency,
                  MAX(ts) AS last_ts
             FROM calls WHERE ts >= ?"""
    args = [day_start]
    if call_role == "worker":
        q += " AND (call_role='worker' OR call_role IS NULL)"
    elif call_role == "router":
        q += " AND call_role LIKE 'router%'"
    elif call_role:
        q += " AND call_role=?"
        args.append(call_role)
    q += " GROUP BY provider"
    with conn() as c:
        rows = c.execute(q, args).fetchall()
        return {r["provider"]: dict(r) for r in rows}


def day_spend(agent=None, session=None):
    """Calendar-day spend in USD, scoped to one agent and/or one session.

    Feeds the policy engine's spend-cap rule (which was dead code until
    callers actually passed over_spend_cap). Returns
    {"agent_day_usd": .., "session_day_usd": ..} with 0.0 for scopes
    the caller didn't ask about.
    """
    import pricing as _pricing

    now = time.time()
    day_start = now - (now % 86400)
    out = {"agent_day_usd": 0.0, "session_day_usd": 0.0}

    def _scope(where, args):
        q = ("SELECT provider, SUM(input_tokens) AS i, SUM(output_tokens) AS o"
             " FROM calls WHERE ts >= ? AND " + where +
             " GROUP BY provider")
        total = 0.0
        with conn() as c:
            for r in c.execute(q, [day_start] + args).fetchall():
                total += _pricing.estimate_usd(
                    r["provider"], r["i"] or 0, r["o"] or 0)
        return round(total, 6)

    if agent:
        out["agent_day_usd"] = _scope("agent=?", [agent])
    if session:
        out["session_day_usd"] = _scope("session=?", [session])
    return out
