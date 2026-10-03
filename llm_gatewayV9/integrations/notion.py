"""Notion integration. Moved verbatim from agent mcp_server.

Key (gateway .env): NOTION_TOKEN. Includes the create_page body-append fix.
"""
from __future__ import annotations

from typing import Any

import httpx

from . import _MissingKey, _fail, _need


def _page_title(p: dict) -> str:
    props = p.get("properties", {}) or {}
    for _name, prop in props.items():
        if isinstance(prop, dict) and prop.get("type") == "title":
            titles = prop.get("title") or []
            if titles:
                return titles[0].get("plain_text", "")
    return ""


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
        with httpx.Client(timeout=20, follow_redirects=True) as client:
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
                    return {"ok": False, "error": "create_page requires a parent page_id"}
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
