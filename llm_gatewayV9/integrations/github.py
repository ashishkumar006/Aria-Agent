"""GitHub integration. Moved verbatim from agent mcp_server.

Key (gateway .env): GITHUB_TOKEN.
"""
from __future__ import annotations

from typing import Any

import httpx

from . import _MissingKey, _fail, _need


def query(*, api_method: str, owner: str = "", repo: str = "",
          issue_number: int = 0, title: str = "", body: str = "",
          state: str = "open") -> dict[str, Any]:
    try:
        token = _need("GITHUB_TOKEN")
    except _MissingKey:
        return _fail("GITHUB_TOKEN not set")
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"}
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
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
