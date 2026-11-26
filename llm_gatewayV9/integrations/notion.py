"""Notion integration. Moved verbatim from agent mcp_server.

Key (gateway .env): NOTION_TOKEN. Includes the create_page body-append fix.
"""
from __future__ import annotations

from typing import Any

import httpx
from adaptors.http import shared as pooled_http


from . import _MissingKey, _fail, _need


def _page_title(p: dict) -> str:
    props = p.get("properties", {}) or {}
    for _name, prop in props.items():
        if isinstance(prop, dict) and prop.get("type") == "title":
            titles = prop.get("title") or []
            if titles:
                return titles[0].get("plain_text", "")
    return ""


def _candidate_pages(client, headers: dict, limit: int = 8) -> list[dict]:
    """Pages a caller could use as a parent, id + title only.

    A create_page that forgets its parent used to dead-end on a bare
    "requires a parent page_id", so the model had no way to recover in the
    same run even though it had just listed pages. Returning a few
    candidates makes the retry trivial for any caller, not just this one.
    """
    try:
        r = client.post("https://api.notion.com/v1/search", headers=headers,
                        json={"filter": {"property": "object", "value": "page"},
                              "page_size": limit})
        r.raise_for_status()
        return [{"id": p["id"], "title": _page_title(p)}
                for p in r.json().get("results", [])[:limit]]
    except Exception:
        return []


def query(*, api_method: str, page_id: str = "", database_id: str = "",
          title: str = "", body: str = "") -> dict[str, Any]:
    try:
        token = _need("NOTION_TOKEN")
    except _MissingKey:
        return _fail("NOTION_TOKEN not set")
    headers = {"Authorization": f"Bearer {token}",
               "Notion-Version": "2022-06-28",
               "Content-Type": "application/json"}

    def _para(text: str) -> dict:
        return {"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text",
                                             "text": {"content": text}}]}}

    try:
        with pooled_http(20.0) as client:
            if api_method == "list_pages":
                # Notion's /v1/search is POST-only (GET drops the body on
                # most stacks, silently unfiltering the query).
                r = client.post("https://api.notion.com/v1/search",
                                headers=headers, json={"filter": {"property": "object",
                                                                  "value": "page"}})
                r.raise_for_status()
                return {"ok": True, "pages": [{"id": p["id"], "title": _page_title(p)}
                                              for p in r.json().get("results", [])[:10]]}
            if api_method == "get_page":
                r = client.get(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
                r.raise_for_status()
                return {"ok": True, "page": r.json()}
            if api_method == "create_page":
                if not page_id:
                    cands = _candidate_pages(client, headers)
                    out: dict[str, Any] = {
                        "ok": False,
                        "error": "create_page requires a parent page_id",
                    }
                    if cands:
                        out["candidates"] = cands
                        out["hint"] = ("retry with page_id set to one of "
                                       "these existing pages")
                    return out
                payload = {"parent": {"type": "page_id", "page_id": page_id},
                           "properties": {"title": [{"text": {"content": title}}]}}
                r = client.post("https://api.notion.com/v1/pages", headers=headers, json=payload)
                r.raise_for_status()
                new_page = r.json()
                out: dict[str, Any] = {"ok": True, "id": new_page.get("id")}
                if body:
                    try:
                        client.patch(
                            f"https://api.notion.com/v1/blocks/{new_page['id']}/children",
                            headers=headers, json={"children": [_para(body)]})
                        out["body_appended"] = True
                    except Exception as e:
                        # Page exists but the body never landed — say so
                        # loudly instead of reporting a clean ok.
                        out["body_appended"] = False
                        out["warning"] = (f"page created but body append "
                                          f"failed: {type(e).__name__}: {e}")
                return out
            if api_method == "query_database":
                r = client.post(f"https://api.notion.com/v1/databases/{database_id}/query",
                                headers=headers, json={})
                r.raise_for_status()
                return {"ok": True, "results": r.json().get("results", [])[:10]}
            if api_method == "append_text":
                r = client.patch(f"https://api.notion.com/v1/blocks/{page_id}/children",
                                 headers=headers, json={"children": [_para(body)]})
                r.raise_for_status()
                return {"ok": True}
            return {"ok": False, "error": f"unknown api_method '{api_method}'"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def call(*, api_method: str, **kw) -> dict[str, Any]:
    """Generic Notion dispatcher. `api_method` is one of: search, get_page,
    get_page_markdown, create_page, update_page, get_blocks, append_blocks,
    query_database."""
    try:
        token = _need("NOTION_TOKEN")
    except _MissingKey:
        return _fail("NOTION_TOKEN not set")
    h = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28",
         "Content-Type": "application/json"}
    page_id = kw.get("page_id", "")
    block_id = kw.get("block_id", "") or page_id
    db_id = kw.get("database_id", "")
    try:
        with pooled_http(20.0) as c:
            if api_method == "search":
                r = c.post("https://api.notion.com/v1/search", headers=h,
                           json={"query": kw.get("query", ""),
                                 "page_size": min(int(kw.get("page_size", 20)), 100)})
                r.raise_for_status(); x = r.json()
                return {"ok": True, "results": [
                    {"id": i["id"], "title": _page_title(i), "object": i.get("object")}
                    for i in x.get("results", [])], "has_more": x.get("has_more")}
            if api_method == "get_page":
                r = c.get(f"https://api.notion.com/v1/pages/{page_id}", headers=h)
                r.raise_for_status(); x = r.json()
                return {"ok": True, "page": {"id": x["id"], "title": _page_title(x),
                                             "url": x.get("url")}}
            if api_method == "get_page_markdown":
                r = c.get(f"https://api.notion.com/v1/pages/{page_id}/markdown", headers=h)
                r.raise_for_status()
                return {"ok": True, "markdown": r.json().get("markdown", "")}
            if api_method == "create_page":
                parent = kw.get("parent") or ({"type": "page_id", "page_id": page_id}
                                              if page_id else None)
                if not parent:
                    cands = _candidate_pages(c, h)
                    out = {"ok": False,
                           "error": "create_page needs page_id or parent"}
                    if cands:
                        out["candidates"] = cands
                        out["hint"] = ("retry with parent={page_id} or a "
                                       "page_id from these existing pages")
                    return out
                r = c.post("https://api.notion.com/v1/pages", headers=h,
                           json={"parent": parent,
                                 "properties": {"title": [{"text": {"content": kw.get("title", "")}}]}})
                r.raise_for_status()
                return {"ok": True, "id": r.json().get("id")}
            if api_method == "update_page":
                r = c.patch(f"https://api.notion.com/v1/pages/{page_id}", headers=h,
                            json={"archived": bool(kw.get("archived", False))})
                r.raise_for_status()
                return {"ok": True, "id": r.json().get("id")}
            if api_method == "get_blocks":
                r = c.get(f"https://api.notion.com/v1/blocks/{block_id}/children", headers=h,
                          params={"page_size": min(int(kw.get("page_size", 100)), 100)})
                r.raise_for_status(); x = r.json()
                return {"ok": True, "results": x.get("results", []),
                        "has_more": x.get("has_more")}
            if api_method == "append_blocks":
                children = kw.get("children") or []
                if not children and kw.get("text"):
                    children = [{"object": "block", "type": "paragraph",
                                 "paragraph": {"rich_text": [{"type": "text",
                                                              "text": {"content": kw["text"]}}]}}]
                r = c.patch(f"https://api.notion.com/v1/blocks/{block_id}/children",
                            headers=h, json={"children": children})
                r.raise_for_status()
                return {"ok": True}
            if api_method == "query_database":
                r = c.post(f"https://api.notion.com/v1/databases/{db_id}/query", headers=h,
                           json={"filter": kw["filter"]} if kw.get("filter") else {})
                r.raise_for_status()
                return {"ok": True, "results": r.json().get("results", [])[:20]}
            return {"ok": False, "error": f"unknown api_method '{api_method}'"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
