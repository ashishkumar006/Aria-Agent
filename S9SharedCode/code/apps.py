"""Config-driven tracker apps for the Apps page.

An app is a JSON spec (no code): what to watch, where to fetch it, how
often to refresh, and what to show. The runner executes specs on schedule,
diffs item ids so the UI can highlight added/removed entries, and persists
snapshots + short history under state/apps/.

Kinds (v1):
- openrouter_free: OpenRouter's public models endpoint, filtered to free
  models (pricing.prompt == "0"). Zero-config beyond name/schedule.
- json_feed: any JSON URL + dot-path to the item list, an optional
  {field, equals} match, and a field allowlist.

Schedules reuse the scheduler's cron mini-language ("daily@HH:MM",
"every Nm/Nh") plus "manual". A tiny worker thread (30s tick) fires due
refreshes — deliberately separate from the LLM scheduler, which fires
agent queries, not code.

Conventions match agent_server: S9_STATE_DIR overrides the state dir so
tests never touch live apps.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent
STATE_DIR = Path(os.environ.get("S9_STATE_DIR") or (ROOT / "state"))
APPS_DIR = STATE_DIR / "apps"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

_lock = threading.Lock()
_stop = threading.Event()
_worker: threading.Thread | None = None


# ── specs ─────────────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug[:40] or "app"


def validate_spec(raw: dict) -> dict:
    """Validate a posted app spec, returning a normalised spec dict.
    Raises ValueError with a human message on any problem (→ HTTP 400)."""
    if not isinstance(raw, dict):
        raise ValueError("spec must be a JSON object")
    kind = (raw.get("kind") or "").strip()
    if kind not in ("openrouter_free", "json_feed"):
        raise ValueError("kind must be 'openrouter_free' or 'json_feed'")
    name = (raw.get("name") or "").strip()
    if not name:
        raise ValueError("name required")
    app_id = (raw.get("id") or _slug(name)).strip().lower()
    if not _ID_RE.match(app_id):
        raise ValueError("id must match [a-z0-9][a-z0-9_-]{0,40}")
    schedule = (raw.get("schedule") or "manual").strip()
    if schedule != "manual":
        try:
            _next_fire(schedule, time.time())
        except Exception:
            raise ValueError("schedule must be 'manual', 'daily@HH:MM', 'every Ns/Nm/Nh', 'in Nm/Nh', 'tomorrow HH:MM', ISO datetime or epoch seconds")
        # "every 1s" is a valid recurring interval for the
        # scheduler, but a tracker on it fetches a third-party
        # feed every second of every day. The floor for app
        # schedules is one minute.
        _im = re.match(r"^every\s+(\d+)\s*([smh]?)$",
                       schedule.strip().lower())
        if _im:
            _secs = int(_im.group(1)) * {"s": 1, "m": 60, "h": 3600}[_im.group(2) or "m"]
            if _secs < 60:
                raise ValueError("app schedule interval must be >= 60s")
    spec: dict = {
        "id": app_id,
        "name": name[:80],
        "kind": kind,
        "schedule": schedule,
        "id_field": (raw.get("id_field") or "id").strip() or "id",
        "fields": raw.get("fields") or (["id", "name", "context_length"]
                                        if kind == "openrouter_free" else ["id"]),
        "max_items": int(raw.get("max_items") or 200),
        "history": int(raw.get("history") or 30),
    }
    if not isinstance(spec["fields"], list) or not spec["fields"]:
        raise ValueError("fields must be a non-empty list")
    spec["fields"] = [str(f)[:40] for f in spec["fields"]][:12]
    if not 1 <= spec["max_items"] <= 2000:
        raise ValueError("max_items must be 1..2000")
    if not 1 <= spec["history"] <= 200:
        raise ValueError("history must be 1..200")
    # Mini-UI block: per-app view config (table/cards, columns, sort).
    # Pure data — the frontend renders it, so a bad value can never crash
    # the console; validation still rejects junk loudly (→ HTTP 400).
    ui = raw.get("ui") or {}
    if not isinstance(ui, dict):
        raise ValueError("ui must be an object")
    view = (ui.get("view") or "table").strip()
    if view not in ("table", "cards"):
        raise ValueError("ui.view must be 'table' or 'cards'")
    raw_cols = ui.get("columns") or []
    if not isinstance(raw_cols, list):
        raise ValueError("ui.columns must be a list")
    columns = []
    for c in raw_cols[:12]:
        if isinstance(c, str) and c.strip():
            columns.append({"field": c.strip()[:40], "label": c.strip()[:40]})
        elif isinstance(c, dict) and str(c.get("field") or "").strip():
            f = str(c["field"]).strip()[:40]
            columns.append({"field": f, "label": str(c.get("label") or f)[:40]})
        else:
            raise ValueError("ui.columns entries must be field names or {field, label}")
    if not columns:
        columns = [{"field": f, "label": f} for f in spec["fields"]]
    _hl = ui.get("highlight_new", True)
    if isinstance(_hl, str):
        _hl = _hl.strip().lower() in ("1", "true", "yes", "on")
    spec["ui"] = {"view": view, "columns": columns,
                  "sort": str(ui.get("sort") or spec["id_field"])[:40],
                  "highlight_new": bool(_hl)}
    if kind == "json_feed":
        url = (raw.get("url") or "").strip()
        if not re.match(r"^https?://", url):
            raise ValueError("url must be an http(s) URL")
        # The SSRF guard used to run only at fetch time, so a
        # loopback / link-private / metadata URL was accepted at
        # CREATE, persisted into the spec and echoed by every
        # read — the refusal only surfaced (as a 200 error body)
        # on refresh. Refuse it where the spec is written.
        _guard_feed_url(url)
        spec["url"] = url
        spec["items_path"] = (raw.get("items_path") or "data").strip() or "data"
        match = raw.get("match") or {}
        if match:
            if not isinstance(match, dict) or "field" not in match or "equals" not in match:
                raise ValueError("match must be {field, equals}")
            spec["match"] = {"field": str(match["field"])[:80],
                             "equals": match["equals"]}
    else:
        spec["url"] = _OPENROUTER_MODELS_URL
    return spec


def _next_fire(schedule: str, base: float) -> float | None:
    if schedule == "manual":
        return None
    import scheduler as _sched
    return _sched._next_fire_from_cron(schedule, base)


# ── storage ───────────────────────────────────────────────────────────────

def _spec_path(app_id: str) -> Path:
    return APPS_DIR / f"{app_id}.json"


def _data_path(app_id: str) -> Path:
    return APPS_DIR / f"{app_id}.data.json"


def _read_json(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default
    return data


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def list_specs() -> list[dict]:
    if not APPS_DIR.exists():
        return []
    out = []
    for p in sorted(APPS_DIR.glob("*.json")):
        if p.name.endswith(".data.json"):
            continue
        spec = _read_json(p, None)
        if isinstance(spec, dict) and spec.get("id"):
            out.append(spec)
    return out


def get_spec(app_id: str) -> dict | None:
    if not _ID_RE.match(app_id or ""):
        return None
    spec = _read_json(_spec_path(app_id), None)
    return spec if isinstance(spec, dict) else None


# Board ceiling: one spec + one data file per app, and the
# worker re-fetches every due app on every 30s tick.
_MAX_APPS = 50


def create_app(raw: dict) -> dict:
    spec = validate_spec(raw)
    with _lock:
        if _spec_path(spec["id"]).exists():
            raise ValueError(f"app '{spec['id']}' already exists")
        # No ceiling existed: one small JSON file per app,
        # forever, and every due one is fetched again each
        # tick. Cap the board the way the scheduler caps
        # its rows.
        if len(list_specs()) >= _MAX_APPS:
            raise ValueError(f"app limit is {_MAX_APPS}; delete one first")
        now = time.time()
        spec["created"] = now
        spec["updated"] = now
        spec["next_refresh"] = _next_fire(spec["schedule"], now)
        _write_json(_spec_path(spec["id"]), spec)
    return spec


def delete_app(app_id: str) -> bool:
    with _lock:
        sp, dp = _spec_path(app_id), _data_path(app_id)
        if not sp.exists():
            return False
        try:
            sp.unlink()
        except OSError:
            pass
        try:
            dp.unlink()
        except OSError:
            pass
        return True


# ── runner ────────────────────────────────────────────────────────────────

# A spec's URL is operator-supplied, but specs are also
# created through POST /api/apps — so the fetch must not
# reach loopback / link-local / private targets: the agent
# process can reach the gateway, cloud metadata endpoints
# and other internal hosts a browser never could. Refuse
# them up front, refuse credentials embedded in the URL,
# and re-check after redirects (a public host can 302 to
# an internal one).
_MAX_FEED_BYTES = 8 * 1024 * 1024


def _refuse_nonpublic_host(host: str) -> None:
    import ipaddress
    import socket
    h = (host or "").strip().lower()
    if not h:
        raise ValueError("feed URL has no host")
    if h == "localhost" or h.endswith(".localhost"):
        raise ValueError(f"feed URL host {h!r} is loopback")
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        ip = None
    if ip is not None:
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_unspecified or ip.is_reserved):
            raise ValueError(
                f"feed URL host {h!r} is not a public address")
        return
    # A hostname: resolve it and check every address it
    # maps to. Unresolvable here is not a refusal — the
    # fetch itself fails on it, with the real error.
    try:
        infos = socket.getaddrinfo(h, None)
    except socket.gaierror:
        return
    for _fam, _type, _proto, _canon, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError):
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_unspecified or ip.is_reserved):
            raise ValueError(
                f"feed URL host {h!r} resolves to a "
                f"non-public address")


def _guard_feed_url(url: str) -> None:
    import urllib.parse as _up
    p = _up.urlsplit(url)
    if p.scheme not in ("http", "https"):
        raise ValueError(
            f"feed URL scheme must be http/https, not {p.scheme!r}")
    if p.username or p.password:
        # Credentials in the URL are persisted into the spec
        # file and echoed into error messages; keep them out
        # of both.
        raise ValueError("feed URLs must not embed credentials")
    _refuse_nonpublic_host(p.hostname)


def _http_get_json(url: str) -> dict:
    import httpx
    _guard_feed_url(url)
    r = httpx.get(url, timeout=25.0, follow_redirects=True,
                  headers={"User-Agent": "Aria-apps/1.0",
                           "Accept": "application/json"})
    # A public host can redirect to an internal one —
    # re-check the URL the request actually landed on.
    _guard_feed_url(str(r.url))
    if len(r.content) > _MAX_FEED_BYTES:
        raise ValueError(
            f"feed response exceeds {_MAX_FEED_BYTES // (1024 * 1024)}MB")
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError("top-level JSON is not an object")
    return data


def _dotpath(obj, path: str):
    cur = obj
    for part in (path or "").split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _is_free(pricing) -> bool:
    if not isinstance(pricing, dict):
        return False
    try:
        return float(pricing.get("prompt") or 1) == 0
    except (TypeError, ValueError):
        return False


def _fetch_items(spec: dict) -> list[dict]:
    data = _http_get_json(spec["url"])
    if spec["kind"] == "openrouter_free":
        raw_items = data.get("data") or []
        id_field, fields = "id", spec["fields"]
        items = []
        for m in raw_items:
            if not isinstance(m, dict) or not _is_free(m.get("pricing")):
                continue
            items.append({f: m.get(f) for f in fields})
        return items
    raw_items = _dotpath(data, spec.get("items_path") or "data")
    if raw_items is None:
        raise ValueError(f"items_path '{spec.get('items_path')}' not found")
    if not isinstance(raw_items, list):
        raise ValueError("items_path did not resolve to a list")
    match, id_field = spec.get("match") or {}, spec.get("id_field") or "id"
    items = []
    for m in raw_items:
        if not isinstance(m, dict):
            continue
        if match and _dotpath(m, match["field"]) != match["equals"]:
            continue
        items.append({f: _dotpath(m, f) for f in spec["fields"]})
    return items


def refresh_app(app_id: str) -> dict:
    """Run one refresh now. Returns a summary; persists snapshot + history.
    Failures are recorded in the data file, never raised out of the worker
    (manual POSTs surface the error field instead of a 500)."""
    spec = get_spec(app_id)
    if spec is None:
        raise ValueError(f"unknown app '{app_id}'")
    now = time.time()
    prev = _read_json(_data_path(app_id), {}) or {}
    prev_ids = {str(i.get(spec.get("id_field") or "id")) for i in prev.get("items", [])
                if isinstance(i, dict)}
    try:
        items = _fetch_items(spec)[:spec.get("max_items", 200)]
        ids = [str(i.get(spec.get("id_field") or "id")) for i in items]
        added = [i for i in items if str(i.get(spec.get("id_field") or "id")) not in prev_ids]
        removed = sorted(prev_ids - set(ids))
        hist = prev.get("history", []) or []
        hist.append({"ts": now, "count": len(items),
                     "added": [str(i.get(spec.get("id_field") or "id")) for i in added][:50],
                     "removed": removed[:50]})
        hist = hist[-max(1, spec.get("history", 30)):]
        payload = {"refreshed_at": now, "items": items, "count": len(items),
                   "added": added[:50], "removed": removed,
                   "error": None, "history": hist}
    except Exception as e:
        payload = {"refreshed_at": now,
                   "items": prev.get("items", []), "count": len(prev.get("items", [])),
                   "added": [], "removed": [],
                   "error": f"{type(e).__name__}: {e}"[:300],
                   "history": prev.get("history", [])}
    with _lock:
        _write_json(_data_path(app_id), payload)
        cur = get_spec(app_id)
        if cur is not None:
            try:
                cur["next_refresh"] = _next_fire(cur.get("schedule", "manual"), now)
            except Exception:
                cur["next_refresh"] = None
            cur["updated"] = now
            _write_json(_spec_path(app_id), cur)
    return {"id": app_id, "count": payload["count"],
            "added": len(payload["added"]), "removed": len(payload["removed"]),
            "error": payload["error"], "refreshed_at": payload["refreshed_at"]}


def app_status(spec: dict) -> dict:
    data = _read_json(_data_path(spec["id"]), {}) or {}
    return {"id": spec["id"], "name": spec["name"], "kind": spec["kind"],
            "schedule": spec.get("schedule", "manual"),
            "next_refresh": spec.get("next_refresh"),
            "refreshed_at": data.get("refreshed_at"),
            "count": data.get("count", 0),
            "added": len(data.get("added", [])), "removed": len(data.get("removed", [])),
            "error": data.get("error")}


def list_apps() -> list[dict]:
    return [app_status(s) for s in list_specs()]


def get_app(app_id: str) -> dict | None:
    spec = get_spec(app_id)
    if spec is None:
        return None
    data = _read_json(_data_path(app_id), {}) or {}
    return {**app_status(spec), "spec": spec,
            "items": data.get("items", []),
            "added_items": data.get("added", []),
            "removed_ids": data.get("removed", []),
            "history": data.get("history", [])}


# ── worker ────────────────────────────────────────────────────────────────

def _worker() -> None:
    while not _stop.wait(timeout=30.0):
        try:
            now = time.time()
            for spec in list_specs():
                if spec.get("schedule", "manual") == "manual":
                    continue
                nxt = spec.get("next_refresh")
                if nxt is None:
                    continue
                if now >= float(nxt):
                    try:
                        refresh_app(spec["id"])
                    except Exception:
                        pass
        except Exception:
            pass


def _seed() -> None:
    if list_specs():
        return
    try:
        create_app({"name": "Free OpenRouter models",
                    "kind": "openrouter_free",
                    "schedule": "daily@09:00"})
        threading.Thread(target=_seed_refresh, daemon=True).start()
    except Exception:
        pass


def _seed_refresh() -> None:
    time.sleep(20.0)  # let boot finish before the first network pull
    try:
        refresh_app("free-openrouter-models")
    except Exception:
        pass


def start() -> None:
    """Seed the default tracker + start the refresh worker. Safe to call
    twice; never raises."""
    global _worker
    try:
        _seed()
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_worker, daemon=True)
            _worker.start()
    except Exception:
        pass
