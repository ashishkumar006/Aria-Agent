"""
MCP server for EAGV3 Session 7.

Eleven tools, stdio transport:
    web_search, fetch_url, get_time, currency_convert,
    read_file, list_dir, create_file, update_file, edit_file,
    index_document, search_knowledge

web_search:        Tavily primary, DuckDuckGo fallback. Hard-capped at 5 results.
fetch_url:         crawl4ai only. Clean markdown via headless Chromium.
index_document:    Chunks a sandbox file or artifact and writes the chunks as
                   fact records into Memory, where they become FAISS-searchable.
search_knowledge:  Vector search over indexed facts. Same backend as
                   memory.read but exposed to the model as a tool.

Usage for tavily and duckduckgo is logged to ./usage.json with monthly
rollover and a soft cap of 950/1000 on Tavily.

File tools are sandboxed under ./sandbox/. Run:  python mcp_server.py
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from ddgs import DDGS
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Same-directory imports for the Memory and Artifact services so that the
# new index_document / search_knowledge tools can delegate into them.
import sys
sys.path.insert(0, str(Path(__file__).parent))
import artifacts as _artifacts  # noqa: E402
import memory as _memory  # noqa: E402

MAX_SEARCH_RESULTS = 5  # hard cap — Tavily prices per result

load_dotenv(Path(__file__).parent / ".env")

mcp = FastMCP("eagv3-s7-server")

SANDBOX = Path(__file__).parent / "sandbox"
SANDBOX.mkdir(exist_ok=True)

USAGE_PATH = Path(__file__).parent / "usage.json"
MONTHLY_CAP = 950  # leave 50/mo headroom on Tavily
_usage_lock = threading.Lock()


def _safe(path: str) -> Path:
    p = (SANDBOX / path).resolve()
    base = SANDBOX.resolve()
    if p != base and base not in p.parents:
        raise ValueError(f"Path '{path}' escapes the sandbox")
    return p


def _empty_usage(month: str) -> dict:
    return {
        "month": month,
        "tavily": {"count": 0, "errors": 0},
        "duckduckgo": {"count": 0, "errors": 0},
    }


def _load_usage() -> dict:
    month = datetime.now().strftime("%Y-%m")
    if not USAGE_PATH.exists():
        return _empty_usage(month)
    try:
        data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_usage(month)
    if data.get("month") != month:
        return _empty_usage(month)
    for k in ("tavily", "duckduckgo"):
        data.setdefault(k, {"count": 0, "errors": 0})
    return data


def _save_usage(data: dict) -> None:
    USAGE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _bump(provider: str, field: str = "count") -> None:
    with _usage_lock:
        data = _load_usage()
        data[provider][field] = data[provider].get(field, 0) + 1
        _save_usage(data)


def _under_cap(provider: str) -> bool:
    return _load_usage()[provider]["count"] < MONTHLY_CAP


def _tavily_search(query: str, max_results: int) -> list[dict]:
    from tavily import TavilyClient

    client = TavilyClient(os.environ["TAVILY_API_KEY"])
    resp = client.search(query=query, max_results=max_results, search_depth="advanced")
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
        }
        for r in resp.get("results", [])
    ]


def _ddg_search(query: str, max_results: int) -> list[dict]:
    hits: list[dict] = []
    with DDGS() as ddgs:
        for backend in ("auto", "html", "lite"):
            try:
                hits = list(ddgs.text(query, max_results=max_results, backend=backend))
            except Exception:
                hits = []
            if hits:
                break
    return [
        {
            "title": h.get("title", ""),
            "url": h.get("href", ""),
            "snippet": h.get("body", ""),
        }
        for h in hits
    ]


async def _crawl4ai_fetch(url: str) -> dict:
    from crawl4ai import AsyncWebCrawler

    # crawl4ai uses Rich which writes via its own captured stdout reference, so
    # contextlib.redirect_stdout doesn't catch it. Redirect at the file-descriptor
    # level — crawl4ai's banner / [FETCH] / [SCRAPE] markers would otherwise
    # corrupt the MCP stdio JSON-RPC stream.
    saved_fd = os.dup(1)
    os.dup2(2, 1)
    try:
        async with AsyncWebCrawler(verbose=False) as crawler:
            r = await crawler.arun(url=url)
    finally:
        os.dup2(saved_fd, 1)
        os.close(saved_fd)
    # r.markdown is a str subclass (StringCompatibleMarkdown) that Pydantic
    # serializes as {} because its real field is private. Pull the raw string
    # out and force a plain str so FastMCP serializes correctly.
    md = r.markdown
    raw = (
        getattr(md, "raw_markdown", None)
        or getattr(md, "fit_markdown", None)
        or md
        or r.cleaned_html
        or r.html
        or ""
    )
    text = str(raw)
    return {
        "status": int(getattr(r, "status_code", None) or 200),
        "content_type": "text/markdown",
        "length_bytes": len(text.encode("utf-8")),
        "text": text,
    }


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Search the web (Tavily primary, DDG fallback). Hard-capped at 5 results. Example: web_search("python asyncio tutorial", 3)."""
    max_results = max(1, min(max_results, MAX_SEARCH_RESULTS))
    if os.environ.get("TAVILY_API_KEY") and _under_cap("tavily"):
        try:
            results = _tavily_search(query, max_results)
            if results:
                _bump("tavily")
                return results
        except Exception:
            _bump("tavily", "errors")
    results = _ddg_search(query, max_results)
    _bump("duckduckgo")
    return results


@mcp.tool()
async def fetch_url(url: str, timeout: int = 20) -> dict:
    """Fetch clean markdown from a URL via crawl4ai (headless Chromium). Example: fetch_url("https://example.com")."""
    return await _crawl4ai_fetch(url)


@mcp.tool()
def get_time(timezone: str = "UTC") -> dict:
    """Current time in a named IANA timezone. Example: get_time("Asia/Kolkata")."""
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    offset = now.utcoffset()
    offset_hours = offset.total_seconds() / 3600 if offset else 0.0
    return {
        "iso": now.isoformat(),
        "human": now.strftime("%A, %d %B %Y %H:%M:%S %Z"),
        "timezone": timezone,
        "offset_hours": offset_hours,
    }


@mcp.tool()
def currency_convert(amount: float, from_currency: str, to_currency: str) -> dict:
    """Convert money between ISO-3 currencies via frankfurter.dev. Example: currency_convert(100, "USD", "INR")."""
    f = from_currency.upper()
    t = to_currency.upper()
    url = f"https://api.frankfurter.dev/v1/latest?amount={amount}&base={f}&symbols={t}"
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        data = r.json()
    try:
        converted = data["rates"][t]
    except KeyError:
        return {"ok": False, "error": f"currency '{t}' not found in rates"}
    return {
        "amount": amount,
        "from": f,
        "to": t,
        "rate": converted / amount if amount else 0.0,
        "converted": converted,
        "date": data["date"],
        "source": "frankfurter.dev",
    }


@mcp.tool()
def read_file(path: str) -> dict:
    """Read a UTF-8 text file from the sandbox. Example: read_file("notes.txt")."""
    p = _safe(path)
    text = p.read_text(encoding="utf-8")
    return {
        "path": path,
        "size_bytes": p.stat().st_size,
        "content": text,
        "encoding": "utf-8",
    }


@mcp.tool()
def list_dir(path: str = ".") -> dict:
    """List a directory inside the sandbox. Example: list_dir(".")."""
    # NOTES_RUNS §6 (1): a list[dict] return was being rendered as one MCP
    # TextContent per entry. After agent7.py's 300-char clip and decision.py's
    # downstream slicing, only the first 2-3 file dicts survived into the
    # Decision prompt, and Decision then declared the directory complete at
    # whatever it could see. Returning a single dict with `count` and a flat
    # `names` list keeps the cardinality visible even under truncation.
    p = _safe(path)
    entries = []
    names: list[str] = []
    for child in sorted(p.iterdir()):
        is_dir = child.is_dir()
        entries.append({
            "name": child.name,
            "type": "dir" if is_dir else "file",
            "size_bytes": 0 if is_dir else child.stat().st_size,
        })
        names.append(child.name)
    return {"path": path, "count": len(entries), "names": names, "entries": entries}


@mcp.tool()
def create_file(path: str, content: str) -> dict:
    """Create a new file in the sandbox; errors if it exists. Example: create_file("hello.txt", "hi")."""
    p = _safe(path)
    if p.exists():
        raise ValueError(f"File '{path}' already exists")
    if not p.parent.exists():
        raise ValueError(f"Parent directory of '{path}' does not exist")
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "size_bytes": p.stat().st_size}


@mcp.tool()
def update_file(path: str, content: str) -> dict:
    """Overwrite an existing sandbox file. Example: update_file("hello.txt", "new body")."""
    p = _safe(path)
    if not p.exists():
        raise ValueError(f"File '{path}' does not exist")
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "size_bytes": p.stat().st_size}


@mcp.tool()
def edit_file(path: str, find: str, replace: str, replace_all: bool = False) -> dict:
    """Find-and-replace inside a sandbox file. Example: edit_file("hello.txt", "foo", "bar")."""
    p = _safe(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(find)
    if count == 0:
        raise ValueError(f"'{find}' not found in '{path}'")
    if count > 1 and not replace_all:
        raise ValueError(
            f"'{find}' occurs {count} times in '{path}'; pass replace_all=True"
        )
    new_text = text.replace(find, replace) if replace_all else text.replace(find, replace, 1)
    p.write_text(new_text, encoding="utf-8")
    replacements = count if replace_all else 1
    return {
        "ok": True,
        "path": path,
        "replacements": replacements,
        "size_bytes": p.stat().st_size,
    }


# ── document indexing (Session 7) ───────────────────────────────────────────

def _read_for_index(path: str) -> tuple[str, str]:
    """Return (content, source_label) for an indexable file or artifact."""
    if path.startswith("art:"):
        return _artifacts.get_bytes(path).decode("utf-8", errors="replace"), path
    p = _safe(path)
    return p.read_text(encoding="utf-8"), f"sandbox:{path}"


def _chunk_text(text: str, size: int = 400, overlap: int = 80) -> list[str]:
    """Sliding-window chunking by word count. S7 default; semantic chunking
    arrives in Session 8."""
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    stride = max(1, size - overlap)
    i = 0
    while i < len(words):
        chunks.append(" ".join(words[i:i + size]))
        if i + size >= len(words):
            break
        i += stride
    return chunks


@mcp.tool()
def index_document(path: str, chunk_size: int = 400, overlap: int = 80) -> dict:
    """Chunk a sandbox file or artifact and write each chunk into Memory as a searchable `fact`. Use this when the content must remain retrievable across later turns or runs (an indexing step before later vector queries). For one-shot inspection of a known file's contents in this turn, prefer `read_file` instead. Example: index_document("notes/spec.md")."""
    text, source = _read_for_index(path)
    if not text.strip():
        return {"path": path, "source": source, "chunks_indexed": 0, "warning": "empty content"}
    chunks = _chunk_text(text, size=chunk_size, overlap=overlap)
    run_id = f"index-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    indexed = 0
    for i, chunk in enumerate(chunks):
        preview = chunk[:120].replace("\n", " ")
        descriptor = f"[{source} chunk {i+1}/{len(chunks)}] {preview}"
        _memory.add_fact(
            descriptor=descriptor,
            value={
                "chunk": chunk,
                "chunk_index": i,
                "total_chunks": len(chunks),
                "source": source,
            },
            source=source,
            run_id=run_id,
        )
        indexed += 1
    return {
        "path": path,
        "source": source,
        "chunks_indexed": indexed,
        "chunk_size": chunk_size,
        "overlap": overlap,
    }


# ── integration tools (Session 9: general assistant) ────────────────────────
#
# These give the agent real-world "do something" capabilities. Every tool is
# fail-soft: if the relevant credentials are missing it returns a clear
# "not configured" dict instead of raising, so the agent can tell the user
# what to set up rather than crashing the run. Credentials come from env
# vars (see .env.example additions at the bottom of this file's docstring).

@mcp.tool()
def send_telegram(chat_id: str, message: str) -> dict:
    """Send a text message to a Telegram chat via the Bot API. Requires
    TELEGRAM_BOT_TOKEN in the environment. `chat_id` is the numeric id
    (or @channel). Example: send_telegram("123456789", "Reminder: standup in 5 min")."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        r = client.post(url, json={"chat_id": chat_id, "text": message[:4096]})
        r.raise_for_status()
        data = r.json()
    return {"ok": bool(data.get("ok")), "result": data.get("result", {})}


@mcp.tool()
def send_email(to: str, subject: str, body: str) -> dict:
    """Send an email via the Gmail API using OAuth (no SMTP / app password).
    Requires GMAIL_TOKEN — a Google OAuth2 access token with the
    https://www.googleapis.com/auth/gmail.send (or gmail.compose) scope.
    This is the SAME token family as gmail_query (gmail.readonly); request
    both scopes together when you generate it. Example:
    send_email("friend@example.com", "Hello", "Sent by Aria")."""
    import base64
    from email.message import EmailMessage

    token = (os.environ.get("GMAIL_TOKEN") or "").strip().strip('"').strip("'")
    if not token:
        return {"ok": False, "error": "GMAIL_TOKEN not set (Gmail OAuth access token)"}
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    # Gmail API expects a base64url-encoded RFC822 message.
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        try:
            r = client.post(url, headers={"Authorization": f"Bearer {token}"},
                            json={"raw": raw})
            if r.status_code == 401:
                return {"ok": False, "error": "GMAIL_TOKEN expired or invalid (re-auth needed)"}
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "to": to, "subject": subject, "id": data.get("id")}


@mcp.tool()
def create_calendar_event(summary: str, start: str, end: str, description: str = "",
                         location: str = "", timezone: str = "UTC") -> dict:
    """Create a Google Calendar event via the REST API. Requires
    GOOGLE_CALENDAR_TOKEN (an OAuth access token) in the environment.
    `start`/`end` are ISO-8601 datetimes. Example:
    create_calendar_event("Dentist", "2026-09-01T10:00:00", "2026-09-01T11:00:00")."""
    token = (os.environ.get("GOOGLE_CALENDAR_TOKEN") or "").strip().strip('"').strip("'")
    if not token:
        return {"ok": False, "error": "GOOGLE_CALENDAR_TOKEN not set"}
    event = {
        "summary": summary,
        "description": description,
        "location": location,
        "start": {"dateTime": start, "timeZone": timezone},
        "end": {"dateTime": end, "timeZone": timezone},
    }
    url = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        r = client.post(url, json=event,
                        headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        data = r.json()
    return {"ok": True, "id": data.get("id"), "htmlLink": data.get("htmlLink")}


@mcp.tool()
def get_weather(location: str, units: str = "metric") -> dict:
    """Current weather for a city via Open-Meteo (no API key needed).
    `units` is 'metric' (°C) or 'imperial' (°F). Example:
    get_weather("London", "metric")."""
    geo_url = "https://geocoding-api.open-meteo.com/v1/search"
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        g = client.get(geo_url, params={"name": location, "count": 1})
        g.raise_for_status()
        results = g.json().get("results") or []
        if not results:
            return {"ok": False, "error": f"location '{location}' not found"}
        lat = results[0]["latitude"]
        lon = results[0]["longitude"]
        w = client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
                    "temperature_unit": "celsius" if units == "metric" else "fahrenheit"},
        )
        w.raise_for_status()
        cur = w.json().get("current", {})
    return {
        "ok": True,
        "location": results[0].get("name"),
        "country": results[0].get("country"),
        "temperature": cur.get("temperature_2m"),
        "units": units,
        "humidity": cur.get("relative_humidity_2m"),
        "wind_speed": cur.get("wind_speed_10m"),
        "weather_code": cur.get("weather_code"),
    }


@mcp.tool()
def computer_action(action: str, params: dict) -> dict:
    """Request a gated computer-use action on the user's machine.

    High-level actions (routed through the layered engine + safety gates):
      - drive_app   : run a natural-language GOAL against a desktop app.
                     params: {"goal": str, "app": str|null}
      - run_command : params: {"command": str}
      - read_file   : params: {"path": str}
      - write_file  : params: {"path": str, "content": str}
      - open_app    : params: {"app": str}

    Low-level daemon primitives (thin wrappers over cua-driver; require the
    daemon to be running): launch_app, get_accessibility_tree, get_window_state,
    click, type_text, press_key, hotkey, scroll, get_desktop_state, bring_to_front,
    kill_app, start_recording, stop_recording, replay_trajectory, list_apps.

    Returns a result dict; if `status` is 'pending' the action is waiting for
    the user to approve it in the web UI. Computer-use is disabled unless the
    host opted in via COMPUTER_USE_ENABLED. Example:
    computer_action("run_command", {"command": "echo hello"})."""
    from computer_use import get_computer_use, call as daemon_call, DaemonError

    params = params or {}
    high_level = {"drive_app", "run_command", "read_file", "write_file", "open_app"}
    if action in high_level:
        return get_computer_use().request(action, params)

    # Low-level daemon primitives.
    try:
        return daemon_call(action, params, timeout=60)
    except DaemonError as e:
        return {"status": "error", "message": f"daemon error: {e}"}


@mcp.tool()
def search_knowledge(query: str, k: int = 5) -> list[dict]:
    """Vector search over indexed `fact` chunks. Returns up to k ranked chunks with provenance. Call this rather than re-fetching URLs or re-reading source files whenever Memory already contains indexed chunks for the topic — that is the whole point of having indexed the corpus. Example: search_knowledge("authentication flow", 5)."""
    items = _memory.read(query, kinds=["fact"], top_k=k)
    return [
        {
            "id": item.id,
            "descriptor": item.descriptor,
            "source": item.source,
            "chunk": item.value.get("chunk") or "",
            "metadata": {k_: v for k_, v in item.value.items() if k_ != "chunk"},
        }
        for item in items
    ]


# ── Tier-1 integrations: GitHub, Slack, Notion, Gmail read ───────────────
# All follow the same convention as the existing tools: read the credential
# from the environment, return {"ok": False, "error": "..."} when unset, and
# never crash the MCP stdio stream on a missing key.

@mcp.tool()
def github_query(api_method: str, owner: str = "", repo: str = "",
                issue_number: int = 0, title: str = "", body: str = "",
                state: str = "open") -> dict:
    """GitHub via REST API. Requires GITHUB_TOKEN. `api_method` is one of:
    list_issues, get_issue, create_issue, list_repos, search_code.
    Examples:
      github_query("list_issues", owner="octocat", repo="Hello-World")
      github_query("create_issue", owner="me", repo="proj", title="Bug", body="x")
      github_query("list_repos")"""
    token = (os.environ.get("GITHUB_TOKEN") or "").strip().strip('"').strip("'")
    if not token:
        return {"ok": False, "error": "GITHUB_TOKEN not set"}
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"}
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        try:
            if api_method == "list_repos":
                r = client.get("https://api.github.com/user/repos", headers=headers)
                r.raise_for_status()
                return {"ok": True, "repos": [{"name": x["name"], "url": x["html_url"]}
                                              for x in r.json()[:20]]}
            if api_method == "list_issues":
                url = f"https://api.github.com/repos/{owner}/{repo}/issues"
                r = client.get(url, headers=headers, params={"state": state})
                r.raise_for_status()
                return {"ok": True, "issues": [{"number": x["number"], "title": x["title"],
                                                 "state": x["state"]} for x in r.json()[:20]]}
            if api_method == "get_issue":
                url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}"
                r = client.get(url, headers=headers)
                r.raise_for_status()
                x = r.json()
                return {"ok": True, "issue": {"number": x["number"], "title": x["title"],
                                              "body": x.get("body", ""), "state": x["state"]}}
            if api_method == "create_issue":
                url = f"https://api.github.com/repos/{owner}/{repo}/issues"
                r = client.post(url, headers=headers, json={"title": title, "body": body})
                r.raise_for_status()
                x = r.json()
                return {"ok": True, "number": x["number"], "url": x["html_url"]}
            if api_method == "search_code":
                r = client.get("https://api.github.com/search/code",
                               headers=headers, params={"q": title})
                r.raise_for_status()
                return {"ok": True, "items": [{"repo": i["repository"]["full_name"],
                                               "path": i["path"]} for i in r.json().get("items", [])[:10]]}
            return {"ok": False, "error": f"unknown api_method '{api_method}'"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def slack_message(channel: str, text: str) -> dict:
    """Post a message to a Slack channel. Requires SLACK_BOT_TOKEN.
    `channel` is '#general' or a channel id. Example:
    slack_message("#general", "Deploy finished")."""
    token = (os.environ.get("SLACK_BOT_TOKEN") or "").strip().strip('"').strip("'")
    if not token:
        return {"ok": False, "error": "SLACK_BOT_TOKEN not set"}
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        r = client.post("https://slack.com/api/chat.postMessage",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"channel": channel, "text": text})
        r.raise_for_status()
        data = r.json()
    if not data.get("ok"):
        return {"ok": False, "error": data.get("error", "slack error")}
    return {"ok": True, "ts": data.get("ts"), "channel": data.get("channel")}


@mcp.tool()
def notion_query(api_method: str, page_id: str = "", database_id: str = "",
                 title: str = "", body: str = "") -> dict:
    """Notion via REST API. Requires NOTION_TOKEN. `api_method` is one of:
    list_pages, get_page, create_page, query_database, append_text.
    Examples:
      notion_query("list_pages")
      notion_query("create_page", title="Meeting notes", body="Agenda...")
      notion_query("query_database", database_id="<id>")"""
    token = (os.environ.get("NOTION_TOKEN") or "").strip().strip('"').strip("'")
    if not token:
        return {"ok": False, "error": "NOTION_TOKEN not set"}
    headers = {"Authorization": f"Bearer {token}",
               "Notion-Version": "2022-06-28",
               "Content-Type": "application/json"}
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        try:
            if api_method == "list_pages":
                r = client.get("https://api.notion.com/v1/search",
                               headers=headers, json={"filter": {"property": "object",
                                                                "value": "page"}})
                r.raise_for_status()

                def _page_title(p: dict) -> str:
                    # Notion pages don't always use a property literally named
                    # "title"; find the first title-type property instead.
                    props = p.get("properties", {}) or {}
                    for _name, prop in props.items():
                        if isinstance(prop, dict) and prop.get("type") == "title":
                            titles = prop.get("title") or []
                            if titles:
                                return titles[0].get("plain_text", "")
                    return ""
                return {"ok": True, "pages": [{"id": p["id"], "title": _page_title(p)}
                                               for p in r.json().get("results", [])[:10]]}
            if api_method == "get_page":
                r = client.get(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
                r.raise_for_status()
                return {"ok": True, "page": r.json()}
            if api_method == "create_page":
                if not page_id:
                    return {"ok": False, "error": "create_page requires a parent page_id"}
                payload = {"parent": {"type": "page_id", "page_id": page_id},
                           "properties": {"title": [{"text": {"content": title}}]}}
                r = client.post("https://api.notion.com/v1/pages", headers=headers, json=payload)
                r.raise_for_status()
                new_page = r.json()
                # BUG-FIX: the original call discarded `body`. Append it as a
                # paragraph block so the page isn't created empty.
                if body:
                    try:
                        client.patch(
                            f"https://api.notion.com/v1/blocks/{new_page['id']}/children",
                            headers=headers,
                            json={"children": [{"object": "block", "type": "paragraph",
                                                "paragraph": {"rich_text": [{"type": "text",
                                                                            "text": {"content": body}}]}}]},
                        )
                    except Exception:
                        pass
                return {"ok": True, "id": new_page.get("id")}
            if api_method == "query_database":
                r = client.post(f"https://api.notion.com/v1/databases/{database_id}/query",
                                headers=headers, json={})
                r.raise_for_status()
                return {"ok": True, "results": r.json().get("results", [])[:10]}
            if api_method == "append_text":
                r = client.patch(f"https://api.notion.com/v1/blocks/{page_id}/children",
                                 headers=headers,
                                 json={"children": [{"object": "block", "type": "paragraph",
                                                     "paragraph": {"rich_text": [{"type": "text",
                                                                                  "text": {"content": body}}]}}]})
                r.raise_for_status()
                return {"ok": True}
            return {"ok": False, "error": f"unknown api_method '{api_method}'"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def gmail_query(api_method: str, query: str = "", max_results: int = 5) -> dict:
    """Gmail via Google REST API. Requires GMAIL_TOKEN (OAuth access token).
    `api_method` is 'list' or 'read'. For 'list', `query` is a Gmail search
    string (e.g. "is:unread from:boss"). For 'read', `query` is the message id.
    Examples:
      gmail_query("list", "is:unread")
      gmail_query("read", "<messageId>")"""
    token = (os.environ.get("GMAIL_TOKEN") or "").strip().strip('"').strip("'")
    if not token:
        return {"ok": False, "error": "GMAIL_TOKEN not set"}
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        try:
            if api_method == "list":
                r = client.get("https://gmail.googleapis.com/gmail/v1/users/me/messages",
                               headers=headers, params={"q": query, "maxResults": max_results})
                r.raise_for_status()
                ids = [m["id"] for m in r.json().get("messages", [])]
                return {"ok": True, "message_ids": ids}
            if api_method == "read":
                r = client.get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{query}",
                               headers=headers, params={"format": "full"})
                r.raise_for_status()
                msg = r.json()
                parts = msg.get("payload", {}).get("parts", [])
                snippet = msg.get("snippet", "")
                return {"ok": True, "id": query, "snippet": snippet,
                        "headers": {h["name"]: h["value"] for h in
                                    msg.get("payload", {}).get("headers", []) if h["name"] in
                                    ("Subject", "From", "Date")}}
            return {"ok": False, "error": f"unknown api_method '{api_method}'"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def gmail_refresh_token(write_env: bool = True) -> dict:
    """Exchange a Google OAuth2 refresh token for a fresh Gmail access token.
    Requires GMAIL_REFRESH_TOKEN, GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET (from a
    Google Cloud OAuth client). When write_env=True, the new access token is
    written back to GMAIL_TOKEN in the .env file so send_email / gmail_query
    keep working past the ~1-hour expiry. Returns the new token in the result.
    Example: gmail_refresh_token()"""
    import json
    import re

    refresh = (os.environ.get("GMAIL_REFRESH_TOKEN") or "").strip().strip('"').strip("'")
    cid = (os.environ.get("GMAIL_CLIENT_ID") or "").strip().strip('"').strip("'")
    csec = (os.environ.get("GMAIL_CLIENT_SECRET") or "").strip().strip('"').strip("'")
    if not (refresh and cid and csec):
        return {"ok": False, "error": "GMAIL_REFRESH_TOKEN / GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET not set"}
    data = {
        "client_id": cid,
        "client_secret": csec,
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            r = client.post("https://oauth2.googleapis.com/token", data=data)
            r.raise_for_status()
            resp = r.json()
        new_token = resp.get("access_token")
        if not new_token:
            return {"ok": False, "error": f"no access_token in response: {resp}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if write_env:
        env_path = Path(__file__).resolve().parent / ".env"
        try:
            text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
            if re.search(r"^\s*GMAIL_TOKEN=", text, re.M):
                text = re.sub(r"^\s*GMAIL_TOKEN=.*$", f"GMAIL_TOKEN={new_token}",
                              text, flags=re.M)
            else:
                text = text.rstrip() + f"\nGMAIL_TOKEN={new_token}\n"
            env_path.write_text(text, encoding="utf-8")
        except Exception as e:
            return {"ok": True, "access_token": new_token,
                    "warning": f"token refreshed but .env write failed: {e}"}
    return {"ok": True, "access_token": new_token, "expires_in": resp.get("expires_in")}


if __name__ == "__main__":
    mcp.run(transport="stdio")
