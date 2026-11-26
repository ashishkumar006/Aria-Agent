"""GitHub integration. Moved verbatim from agent mcp_server.

Key (gateway .env): GITHUB_TOKEN.
"""
from __future__ import annotations

from typing import Any

import httpx
from adaptors.http import shared as pooled_http


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
        with pooled_http(20.0) as client:
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


def call(*, api_method: str, **kw) -> dict[str, Any]:
    """Generic GitHub REST dispatcher. `api_method` is one of: get_user,
    list_repos, get_repo, read_file, list_issues, get_issue, create_issue,
    comment_issue, list_prs, get_pr, create_pr, search_repos."""
    try:
        token = _need("GITHUB_TOKEN")
    except _MissingKey:
        return _fail("GITHUB_TOKEN not set")
    h = {"Authorization": f"Bearer {token}",
         "Accept": "application/vnd.github+json", "User-Agent": "AriaGateway"}
    owner, repo = kw.get("owner", ""), kw.get("repo", "")
    base = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        with pooled_http(20.0) as c:
            if api_method == "get_user":
                r = c.get("https://api.github.com/user", headers=h); r.raise_for_status()
                x = r.json()
                return {"ok": True, "login": x.get("login"), "id": x.get("id"),
                        "name": x.get("name"), "public_repos": x.get("public_repos")}
            if api_method == "list_repos":
                r = c.get("https://api.github.com/user/repos", headers=h,
                          params={"per_page": min(int(kw.get("limit", 30)), 100), "sort": "updated"})
                r.raise_for_status()
                return {"ok": True, "repos": [
                    {"name": x["name"], "full_name": x["full_name"],
                     "private": x.get("private"), "url": x["html_url"]}
                    for x in r.json()]}
            if api_method == "get_repo":
                r = c.get(base, headers=h); r.raise_for_status()
                x = r.json()
                return {"ok": True, "repo": {"full_name": x["full_name"],
                                             "description": x.get("description"),
                                             "default_branch": x.get("default_branch"),
                                             "open_issues": x.get("open_issues_count")}}
            if api_method == "read_file":
                r = c.get(f"{base}/contents/{kw['path']}", headers=h,
                          params={"ref": kw.get("ref") or None})
                r.raise_for_status()
                import base64 as _b64
                x = r.json()
                content = (_b64.b64decode(x.get("content", "")).decode("utf-8", "replace")
                           if x.get("encoding") == "base64" else "")
                return {"ok": True, "path": x.get("path"), "content": content, "sha": x.get("sha")}
            if api_method == "list_issues":
                r = c.get(f"{base}/issues", headers=h,
                          params={"state": kw.get("state", "open"),
                                  "per_page": min(int(kw.get("limit", 30)), 100)})
                r.raise_for_status()
                return {"ok": True, "issues": [
                    {"number": x["number"], "title": x["title"], "state": x["state"],
                     "url": x["html_url"]}
                    for x in r.json() if "pull_request" not in x]}
            if api_method == "get_issue":
                r = c.get(f"{base}/issues/{kw['number']}", headers=h); r.raise_for_status()
                x = r.json()
                return {"ok": True, "issue": {"number": x["number"], "title": x["title"],
                                              "body": x.get("body") or "", "state": x["state"]}}
            if api_method == "create_issue":
                r = c.post(f"{base}/issues", headers=h,
                           json={"title": kw["title"], "body": kw.get("body", "")})
                r.raise_for_status()
                x = r.json()
                return {"ok": True, "number": x["number"], "url": x["html_url"]}
            if api_method == "update_issue":
                payload = {"state": kw.get("state", "closed")}
                if kw.get("title"):
                    payload["title"] = kw["title"]
                if kw.get("body") is not None:
                    payload["body"] = kw["body"]
                r = c.patch(f"{base}/issues/{kw['number']}", headers=h, json=payload)
                r.raise_for_status()
                x = r.json()
                return {"ok": True, "number": x.get("number"), "state": x.get("state")}
            if api_method == "comment_issue":
                r = c.post(f"{base}/issues/{kw['number']}/comments", headers=h,
                           json={"body": kw["body"]})
                r.raise_for_status()
                return {"ok": True, "id": r.json().get("id")}
            if api_method == "list_prs":
                r = c.get(f"{base}/pulls", headers=h,
                          params={"state": kw.get("state", "open"),
                                  "per_page": min(int(kw.get("limit", 30)), 100)})
                r.raise_for_status()
                return {"ok": True, "prs": [
                    {"number": x["number"], "title": x["title"], "state": x["state"],
                     "head": x["head"]["ref"], "base": x["base"]["ref"]}
                    for x in r.json()]}
            if api_method == "get_pr":
                r = c.get(f"{base}/pulls/{kw['number']}", headers=h); r.raise_for_status()
                x = r.json()
                return {"ok": True, "pr": {"number": x["number"], "title": x["title"],
                                           "body": x.get("body") or "", "state": x["state"],
                                           "mergeable": x.get("mergeable")}}
            if api_method == "create_pr":
                r = c.post(f"{base}/pulls", headers=h,
                           json={"title": kw["title"], "head": kw["head"],
                                 "base": kw["base"], "body": kw.get("body", "")})
                r.raise_for_status()
                x = r.json()
                return {"ok": True, "number": x["number"], "url": x["html_url"]}
            if api_method == "search_repos":
                r = c.get("https://api.github.com/search/repositories", headers=h,
                          params={"q": kw["query"], "per_page": min(int(kw.get("limit", 20)), 100)})
                r.raise_for_status()
                return {"ok": True, "repos": [
                    {"full_name": x["full_name"], "url": x["html_url"],
                     "stars": x.get("stargazers_count")}
                    for x in r.json().get("items", [])]}
            return {"ok": False, "error": f"unknown api_method '{api_method}'"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
