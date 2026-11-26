"""A browser the studio can hold, watch and be measured by.

The skill that already exists in `browser/skill.py` is a DAG node: it launches
Chromium, drives it against one goal, and closes it — all inside a single
`_drive()` call. That is right for a research step and wrong for an IDE, for
four reasons this module exists to fix:

1. **Nothing survives a call.** A login, a dev server's second page, the state
   after a form submit — all gone, and every node pays a cold Chromium start.
   Here the context is per studio session and lives until it is closed or goes
   idle.
2. **Nobody can see it.** `headless=True` is hardcoded in the skill. Here
   `headed` is a choice, and even headless every action leaves a screenshot.
3. **It cannot see a JavaScript error.** There is no `page.on("console")` and
   no network listener anywhere in the skill, which is precisely what a dev
   loop needs after the agent edits a file. Both are captured here, per page,
   in bounded rings.
4. **There is no tool to grant.** Zero `@mcp.tool()` browser verbs exist, so no
   interactive surface can reach a browser at all. `mcp_server.py` registers
   these as tools; this module is only the mechanism.

One asyncio loop runs on one thread (`_loop`), because Playwright's objects are
bound to the loop that made them and MCP tools are called from whichever worker
thread the server happens to use. Every public function is synchronous and
marshals into that loop with a timeout written HERE — `TOOL_CALL_TIMEOUT_S`
does not bind a tool that is not an MCP-over-HTTP call, so a hung page would
otherwise hold the worker indefinitely.
"""
from __future__ import annotations

import asyncio
import os
import re
import threading
import time
import uuid
from pathlib import Path

# How long one tool call may take before the caller is told it did not finish.
ACTION_TIMEOUT_S = 25.0
NAV_TIMEOUT_S = 45.0
IDLE_CLOSE_S = 15 * 60.0
MAX_TABS = 8
# Ring sizes. A dev server that logs in a loop will fill one; the count that
# was dropped is reported rather than silently forgotten.
CONSOLE_CAP = 400
NETWORK_CAP = 400
TEXT_CAP = 200_000
BODY_SNIPPET_CAP = 300

_LOCK = threading.RLock()
_SESSIONS: dict = {}
_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_THREAD: threading.Thread | None = None
_LOOP_READY = threading.Event()

DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
              "Safari/537.36")


def _loop() -> asyncio.AbstractEventLoop:
    global _LOOP, _LOOP_THREAD
    with _LOCK:
        if _LOOP is not None and not _LOOP.is_closed():
            return _LOOP
        ready = threading.Event()

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            globals()["_LOOP"] = loop
            loop.call_soon(ready.set)
            loop.run_forever()

        t = threading.Thread(target=_run, daemon=True, name="aria-browser-loop")
        t.start()
        _LOOP_THREAD = t
        ready.wait(5.0)
        assert _LOOP is not None
        return _LOOP


def _call(coro_factory, timeout: float):
    """Run a coroutine on the browser loop and wait for it, with a bound."""
    loop = _loop()
    fut = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
    try:
        return fut.result(timeout=timeout + 2.0)
    except TimeoutError:
        fut.cancel()
        raise
    except Exception as e:  # surfaced as a value, never as a hung call
        raise


def _nav_url(raw: str) -> tuple[bool, str, str]:
    """(may navigate, the url to hand Chromium, why not).

    The rule used to be "does it have a colon", so `file://`, `chrome://`,
    `devtools://` and `view-source:` all navigated — and a file:// page reads
    this machine: the agent's state directory, its per-launch token, the
    gateway's loopback UI. A bare host still becomes https.

    `data:` is the one non-web scheme that stays, deliberately: it renders in
    an opaque origin, so it cannot read the disk or another origin's responses,
    and it is how a caller hands Chromium a page it GENERATED rather than one
    it fetched — which is also how every real-page test supplies its page.
    """
    url = str(raw or "").strip()
    if not url:
        return False, url, "no address given"
    if re.match(r"^https?://", url, re.I):
        return True, url, ""
    if url.lower().startswith("data:"):
        return True, url, ""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
        return False, url, "scheme refused: only http, https and data: navigate"
    return True, "https://" + url, ""


class Ring:
    """A bounded append-only log that remembers how much it threw away."""


    def __init__(self, cap: int):
        self.cap = cap
        self.items: list = []
        self.dropped = 0

    def add(self, row: dict) -> None:
        self.items.append(row)
        if len(self.items) > self.cap:
            # Drop from the front, but never silently: the count is part of
            # what a caller reads back.
            self.items.pop(0)
            self.dropped += 1

    def since(self, seq: int) -> tuple[list, int]:
        return list(self.items), self.dropped + seq


class Browser:
    """One session's browser: context, tabs, and what the pages said."""

    def __init__(self, sid: str, headed: bool = False, root: Path | None = None):
        self.sid = sid
        self.headed = headed
        self.root = root
        self.created = time.time()
        self.idle_after = time.time() + IDLE_CLOSE_S
        self.playwright = None
        self.browser = None
        self.context = None
        self.pages: list = []
        self.rings: list[Ring] = []      # one per page
        self.net: list[Ring] = []
        self.active = 0
        self.snapshots: dict = {}        # page index -> last PageSnapshot
        self.snap_epoch = 0
        self.snap_url = ""
        self.actions = 0
        self.closing = False
        # The live view. One frame held, never a queue: a screencast that
        # buffers is a screencast that lags, and lagging behind the agent by
        # ten seconds is not watching it work.
        self.cdp = None
        self.cast_latest = None
        self.cast_frames = 0
        self.cast_dropped = 0
        self.cast_at = 0.0
        self.cast_fps = 0.0
        self.cast_error = ""

    # ── lifecycle ────────────────────────────────────────────────────
    async def start(self) -> None:
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        launched = None
        last_err = None
        for attempt in (1, 2, 3):
            try:
                launched = await self.playwright.chromium.launch(
                    headless=not self.headed,
                    args=["--disable-blink-features=AutomationControlled"])
                break
            except Exception as e:
                last_err = e
                await asyncio.sleep(0.8 * attempt)
        if launched is None:
            await self.playwright.stop()
            self.playwright = None
            raise RuntimeError(f"chromium would not start: {last_err}")
        self.browser = launched
        self.context = await self.browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=DEFAULT_UA, locale="en-US",
            # A real content-type negotiation, so `page.goto` on an API that
            # answers JSON does not render a browser viewer over it.
            extra_http_headers={"Accept": "text/html,application/json,*/*"},
        )
        await self.context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        await self.new_tab()

    async def stop(self) -> None:
        self.closing = True
        for closer in (self.context, self.browser, self.playwright):
            try:
                if closer is None:
                    continue
                if hasattr(closer, "close"):
                    await closer.close()
                elif hasattr(closer, "stop"):
                    await closer.stop()
            except Exception:
                pass
        self.context = None
        self.browser = None
        self.playwright = None

    def touch(self) -> None:
        self.idle_after = time.time() + IDLE_CLOSE_S

    # ── tabs ─────────────────────────────────────────────────────────
    async def new_tab(self, url: str = "") -> dict:
        if self.context is None:
            await self.start()
        if len(self.pages) >= MAX_TABS:
            return {"ok": False,
                    "error": f"{MAX_TABS} tabs is the limit for one session; "
                             "close one first"}
        page = await self.context.new_page()
        i = len(self.pages)
        self.pages.append(page)
        self.rings.append(Ring(CONSOLE_CAP))
        self.net.append(Ring(NETWORK_CAP))
        self._wire(page, i)
        self.active = i
        if url:
            return await self.goto(url)
        return {"ok": True, "tab": i, "url": "", "tabs": len(self.pages)}

    def _wire(self, page, index: int) -> None:
        """Console and network, per page.

        These two listeners are the whole reason the dev loop was impossible
        before: without them the agent cannot see the error a page throws, so
        it edits the file again on a guess and calls it a fix.
        """
        ring = self.rings[index]
        net = self.net[index]

        def on_console(msg):
            try:
                ring.add({"at": round(time.time(), 3), "type": "console",
                          "level": msg.type, "text": (msg.text or "")[:600]})
            except Exception:
                pass

        def on_error(err):
            try:
                ring.add({"at": round(time.time(), 3), "type": "pageerror",
                          "level": "error", "text": str(err)[:600]})
            except Exception:
                pass

        def on_request(req):
            try:
                net.add({"at": round(time.time(), 3), "method": req.method,
                         "url": req.url[:400], "resource": req.resource_type,
                         "status": None})
            except Exception:
                pass

        def on_response(res):
            try:
                row = {"at": round(time.time(), 3), "method": res.request.method,
                       "url": res.url[:400], "resource": res.request.resource_type,
                       "status": res.status}
                for item in reversed(net.items):
                    if item.get("url") == row["url"] and item.get("status") is None:
                        item["status"] = res.status
                        item["at"] = row["at"]
                        return
                net.add(row)
            except Exception:
                pass

        page.on("console", on_console)
        page.on("pageerror", on_error)
        page.on("request", on_request)
        page.on("response", on_response)

    @property
    def page(self):
        if not self.pages:
            return None
        return self.pages[min(self.active, len(self.pages) - 1)]

    def page_ring(self) -> Ring:
        return self.rings[min(self.active, len(self.rings) - 1)]

    def page_net(self) -> Ring:
        return self.net[min(self.active, len(self.net) - 1)]

    # ── navigation ───────────────────────────────────────────────────
    async def goto(self, url: str, wait: str = "domcontentloaded") -> dict:
        # Refused before a Chromium exists: a navigation that will not happen
        # must not be able to start one.
        ok, target, why = _nav_url(url)
        if not ok:
            return {"ok": False, "url": url, "status": None, "title": "",
                    "tabs": len(self.pages), "error": why}
        url = target
        page = self.page
        if page is None:
            await self.new_tab()
            page = self.page
        err = None
        try:
            resp = await asyncio.wait_for(
                page.goto(url, wait_until=wait, timeout=NAV_TIMEOUT_S * 1000),
                timeout=NAV_TIMEOUT_S + 5)
        except Exception as e:
            resp = None
            err = f"{type(e).__name__}: {e}"
        self.snapshots[self.active] = None
        self.snap_epoch += 1
        self.touch()
        return {"ok": err is None, "url": page.url,
                "status": getattr(resp, "status", None),
                "title": await _safe_title(page), "tabs": len(self.pages),
                "error": err or ""}

    async def _history(self, which: str) -> dict:
        page = self.page
        if page is None:
            return {"ok": False, "url": "", "error": "no page"}
        err = None
        try:
            coro = (page.go_back(wait_until="domcontentloaded",
                                 timeout=NAV_TIMEOUT_S * 1000)
                    if which == "back" else
                    page.go_forward(wait_until="domcontentloaded",
                                    timeout=NAV_TIMEOUT_S * 1000))
            await asyncio.wait_for(coro, timeout=NAV_TIMEOUT_S + 5)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        self.snapshots[self.active] = None
        self.snap_epoch += 1
        self.touch()
        return {"ok": err is None, "url": page.url,
                "title": await _safe_title(page), "tabs": len(self.pages),
                "error": err or ""}

    async def back(self) -> dict:
        return await self._history("back")

    async def forward(self) -> dict:
        return await self._history("forward")

    async def reload(self) -> dict:
        page = self.page
        if page is None:
            return {"ok": False, "url": "", "error": "no page"}
        err = None
        try:
            await asyncio.wait_for(
                page.reload(wait_until="domcontentloaded",
                            timeout=NAV_TIMEOUT_S * 1000),
                timeout=NAV_TIMEOUT_S + 5)
        except Exception as e:
          err = f"{type(e).__name__}: {e}"
        self.snapshots[self.active] = None
        self.snap_epoch += 1
        self.touch()
        return {"ok": err is None, "url": page.url,
                "title": await _safe_title(page), "tabs": len(self.pages),
                "error": err or ""}

    # ── input forwarding (the screencast is a view; this is the mouse) ──
    async def click_xy(self, x: float, y: float) -> dict:
        page = self.page
        if page is None:
            return {"ok": False, "error": "no page"}
        try:
          await page.mouse.click(x, y)
          return {"ok": True}
        except Exception as e:
          return {"ok": False, "error": str(e)[:200]}

    async def wheel_xy(self, dx: float, dy: float) -> dict:
        page = self.page
        if page is None:
          return {"ok": False, "error": "no page"}
        try:
          await page.mouse.wheel(dx, dy)
          return {"ok": True}
        except Exception as e:
          return {"ok": False, "error": str(e)[:200]}

    async def type_text(self, text: str) -> dict:
        page = self.page
        if page is None:
          return {"ok": False, "error": "no page"}
        try:
          await page.keyboard.type(text)
          return {"ok": True}
        except Exception as e:
          return {"ok": False, "error": str(e)[:200]}

    async def press_key(self, key: str) -> dict:
        page = self.page
        if page is None:
          return {"ok": False, "error": "no page"}
        try:
          await page.keyboard.press(key)
          return {"ok": True}
        except Exception as e:
          return {"ok": False, "error": str(e)[:200]}

    # ── perception ───────────────────────────────────────────────────
    async def snapshot(self, max_elements: int = 120) -> dict:
        """The interactive-element legend, plus what the page is.

        Reuses `browser.dom.enumerate_interactives` — the same a11y layer the
        skill's cascade uses — rather than a second enumeration of the DOM.
        Marks are 1-based ordinals that are only meaningful for THIS snapshot,
        which is why the epoch travels with them: a click addressed to an old
        mark is a click on whatever now sits at those coordinates.
        """
        from browser.dom import enumerate_interactives
        page = self.page
        if page is None:
            return {"ok": False, "error": "no tab is open"}
        try:
            snap = await enumerate_interactives(page, timeout=ACTION_TIMEOUT_S)
        except Exception as e:
            return {"ok": False, "error": f"the page would not enumerate: {e}"}
        self.snapshots[self.active] = snap
        self.snap_url = page.url
        self.touch()
        shown = snap.elements[:max(1, min(int(max_elements or 120), 400))]
        legend = "\n".join(e.legend_line() for e in shown)
        if len(snap.elements) > len(shown):
            legend += (f"\n… {len(snap.elements) - len(shown)} further "
                       "interactive element(s) not shown; ask for more or "
                       "narrow with a screenshot")
        return {"ok": True, "url": page.url, "title": await _safe_title(page),
                "epoch": self.snap_epoch, "elements": len(snap.elements),
                "shown": len(shown), "viewport": [snap.viewport_w, snap.viewport_h],
                "legend": legend[:60_000],
                "text_excerpt": (await _text(page))[:1500]}

    async def read(self, kind: str = "text", limit: int = 6000) -> dict:
        page = self.page
        if page is None:
            return {"ok": False, "error": "no tab is open"}
        cap = max(500, min(int(limit or 6000), TEXT_CAP))
        if kind == "html":
            try:
                body = await page.content()
            except Exception as e:
                return {"ok": False, "error": str(e)}
        elif kind == "links":
            try:
                rows = await page.evaluate(
                    "() => Array.from(document.querySelectorAll('a[href]'))"
                    ".slice(0, 200).map(a => ({text: (a.innerText||'').trim()"
                    ".slice(0,80), href: a.href}))")
            except Exception as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "url": page.url, "links": rows,
                    "count": len(rows), "truncated": len(rows) >= 200}
        else:
            body = await _text(page)
        out = body[:cap]
        return {"ok": True, "url": page.url, "kind": kind, "text": out,
                "chars": len(body), "truncated": len(body) > cap}

    async def screenshot(self, full: bool = False) -> dict:
        page = self.page
        if page is None:
            return {"ok": False, "error": "no tab is open"}
        if self.root is None:
            return {"ok": False, "error": "no artifact directory for this session"}
        d = Path(self.root) / "state" / "sessions" / self.sid / "browser" / "studio"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {"ok": False, "error": f"the artifact dir is unwritable: {e}"}
        name = f"{int(time.time())}-{uuid.uuid4().hex[:6]}.png"
        path = d / name
        try:
            raw = await page.screenshot(full_page=bool(full), timeout=15_000)
            path.write_bytes(raw)
        except Exception as e:
            return {"ok": False, "error": f"the screenshot failed: {e}"}
        rel = f"studio/{name}"
        self.touch()
        # The URL form the console already serves and sandboxes
        # (`/api/artifacts/{sid}/{path}`), so the pane shows bytes the server
        # has already confined rather than a path it can be talked into open()
        # on.
        return {"ok": True, "path": str(path), "url": f"/api/artifacts/{self.sid}/{rel}",
                "bytes": len(raw), "full_page": bool(full),
                "viewport": [await _viewport(page, "w"),
                             await _viewport(page, "h")]}

    async def events(self, kind: str = "console", errors_only: bool = False) -> dict:
        ring = self.page_net() if kind == "network" else self.page_ring()
        rows = list(ring.items)
        if errors_only:
            rows = [r for r in rows
                    if str(r.get("level", "")) in ("error", "warning")
                    or (r.get("status") or 0) >= 400]
        return {"ok": True, "kind": kind, "events": rows[-200:],
                "count": len(rows), "dropped": ring.dropped,
                "showing": min(len(rows), 200),
                "url": getattr(self.page, "url", "")}

    async def wait(self, selector: str = "", state: str = "visible",
                   ms: int = 5000) -> dict:
        page = self.page
        if page is None:
            return {"ok": False, "error": "no tab is open"}
        budget = max(200, min(int(ms or 5000), 20_000))
        target = max(0.2, budget / 1000.0)
        try:
            if selector:
                await asyncio.wait_for(
                    page.wait_for_selector(selector, state=state,
                                           timeout=budget),
                    timeout=target + 5)
            else:
                await asyncio.wait_for(
                    page.wait_for_load_state("networkidle"), timeout=target + 5)
        except Exception as e:
            return {"ok": False, "error": f"still not {state} after {budget}ms: "
                                          f"{type(e).__name__}",
                    "url": page.url, "timeout_ms": budget}
        self.snap_epoch += 1
        self.touch()
        return {"ok": True, "selector": selector, "state": state,
                "url": page.url, "title": await _safe_title(page)}

    # ── acting ───────────────────────────────────────────────────────
    async def act(self, action: str, *, mark: int = 0, selector: str = "",
                  text: str = "", key: str = "", epoch: int = -1,
                  x: int = 0, y: int = 0) -> dict:
        """One interaction, addressed by snapshot mark OR by a real selector.

        The mark path keeps the coordinates honest: the element is looked up in
        the snapshot this session last took, and if the page has navigated
        since, the marks no longer describe anything and the action is refused
        rather than aimed at whatever now sits at those pixels. `selector` is
        the escape hatch that does not need a snapshot at all.
        """
        page = self.page
        if page is None:
            return {"ok": False, "error": "no tab is open"}
        action = (action or "").strip().lower()
        snap = self.snapshots.get(self.active)
        target_desc = ""
        if mark and not selector:
            if snap is None:
                return {"ok": False, "error": "there is no snapshot to take a "
                                              "mark from — call browser_snapshot"
                                              " first"}
            if epoch >= 0 and epoch != self.snap_epoch:
                return {"ok": False, "stale": True,
                        "error": f"that snapshot is superseded (epoch {epoch}, "
                                 f"now {self.snap_epoch}) — take a new one "
                                 "instead of clicking coordinates that moved"}
            el = snap.by_id(int(mark))
            if el is None:
                return {"ok": False,
                        "error": f"mark {mark} is not in this snapshot "
                                 f"({len(snap.elements)} elements)"}
            target_desc = el.legend_line()
        elif selector:
            target_desc = f"selector {selector}"
        elif action not in ("scroll", "keypress", "back", "forward", "reload"):
            return {"ok": False, "error": "give a mark, a selector, or an "
                                         "action that needs neither"}

        t = ACTION_TIMEOUT_S
        try:
            if selector:
                loc = page.locator(selector).first
                await asyncio.wait_for(loc.wait_for(timeout=t * 1000), timeout=t + 5)
            if action in ("click", "double", "hover"):
                if selector:
                    fn = page.locator(selector).first
                    if action == "double":
                        await asyncio.wait_for(fn.dblclick(timeout=t * 1000), timeout=t + 5)
                    elif action == "hover":
                        await asyncio.wait_for(fn.hover(timeout=t * 1000), timeout=t + 5)
                    else:
                        await asyncio.wait_for(fn.click(timeout=t * 1000), timeout=t + 5)
                else:
                    el = snap.by_id(int(mark))
                    if action == "double":
                        await page.mouse.dblclick(el.cx, el.cy)
                    elif action == "hover":
                        await page.mouse.move(el.cx, el.cy)
                    else:
                        await page.mouse.click(el.cx, el.cy)
            elif action in ("type", "fill"):
                value = text
                if selector:
                    fn = page.locator(selector).first
                    if action == "fill":
                        await asyncio.wait_for(fn.fill(value, timeout=t * 1000), timeout=t + 5)
                    else:
                        await asyncio.wait_for(fn.click(timeout=t * 1000), timeout=t + 5)
                        await page.keyboard.type(value)
                else:
                    el = snap.by_id(int(mark))
                    await page.mouse.click(el.cx, el.cy)
                    await page.keyboard.type(value)
            elif action == "press":
                await page.keyboard.press(key or "Enter")
            elif action == "scroll":
                await page.evaluate(
                    "([dx,dy]) => window.scrollBy(dx, dy)",
                    [int(x or 0), int(y or (600 if not x else 0))])
            elif action == "back":
                await page.go_back(timeout=t * 1000)
            elif action == "forward":
                await page.go_forward(timeout=t * 1000)
            elif action == "reload":
                await page.reload(wait_until="domcontentloaded", timeout=t * 1000)
            else:
                return {"ok": False, "error": f"unknown action {action!r}"}
        except Exception as e:
            self.touch()
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "action": action, "target": target_desc,
                    "url": page.url}
        self.actions += 1
        self.snap_epoch += 1
        self.touch()
        # The page after the action, so the next call can be decided from
        # evidence rather than from what the action was meant to do.
        return {"ok": True, "action": action, "target": target_desc,
                "url": page.url, "title": await _safe_title(page),
                "epoch": self.snap_epoch,
                "console_after": [r["text"] for r in self.page_ring().items
                                  if r.get("level") == "error"][-3:],
                "hint": "marks are spent — take a new browser_snapshot"}

    # ── tabs ─────────────────────────────────────────────────────────
    async def tab_list(self) -> dict:
        rows = []
        for i, p in enumerate(self.pages):
            rows.append({"tab": i, "url": getattr(p, "url", ""),
                         "title": await _safe_title(p),
                         "active": i == self.active,
                         "console_errors": sum(
                             1 for r in self.rings[i].items
                             if r.get("level") == "error")})
        vp = {}
        try:
            vp = self.page.viewport_size or {}
        except Exception:
            vp = {}
        return {"ok": True, "tabs": rows, "active": self.active,
                "limit": MAX_TABS, "viewport": vp}

    async def tab_select(self, index: int) -> dict:
        if not (0 <= int(index) < len(self.pages)):
            return {"ok": False, "error": f"no tab {index} (of {len(self.pages)})"}
        self.active = int(index)
        try:
            await self.pages[self.active].bring_to_front()
        except Exception:
            pass
        self.touch()
        return {"ok": True, "active": self.active,
                "url": getattr(self.page, "url", "")}

    async def tab_close(self, index: int) -> dict:
        i = int(index)
        if not (0 <= i < len(self.pages)):
            return {"ok": False, "error": f"no tab {i}"}
        if len(self.pages) == 1:
            return {"ok": False, "error": "the last tab stays: close the "
                                          "browser instead"}
        try:
            await self.pages[i].close()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        self.pages.pop(i)
        self.rings.pop(i)
        self.net.pop(i)
        self.snapshots.pop(i, None)
        self.active = max(0, min(self.active, len(self.pages) - 1))
        self.touch()
        return {"ok": True, "tabs": len(self.pages), "active": self.active}

    async def eval_js(self, script: str) -> dict:
        """A deliberate escape hatch, and the reason it is capped."""
        page = self.page
        if page is None:
            return {"ok": False, "error": "no tab is open"}
        if len(script or "") > 4000:
            return {"ok": False, "error": "the script is over 4000 characters"}
        try:
            out = await asyncio.wait_for(page.evaluate(script),
                                         timeout=ACTION_TIMEOUT_S)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self.touch()
        text = str(out)
        return {"ok": True, "result": text[:20_000], "chars": len(text),
                "truncated": len(text) > 20_000}

    # ── the live view ────────────────────────────────────────────────
    async def start_screencast(self, every_ms: int = 250) -> dict:
        """Stream the page as frames, so a person can WATCH the agent work.

        The skill's browser was invisible by construction — `headless=True`
        hardcoded and no surface at all — which makes "take over" a lie: you
        cannot take over what you cannot see. This is CDP
        `Page.startScreencast` over a real DevTools session, throttled to
        ~4 fps by default, and frames are dropped rather than queued when the
        viewer cannot keep up: a live view that lags eight seconds behind is
        not live, and it costs the memory of every frame in between.

        `screencast_supported` is false on anything that is not Chromium, and
        the pane then falls back to per-action screenshots and says which it
        is showing rather than implying it is watching the page.
        """
        page = self.page
        if page is None:
            return {"ok": False, "error": "no tab is open"}
        if getattr(self, "cdp", None) is not None:
            return {"ok": True, "already": True, "fps": self.cast_fps}
        try:
            cdp = await self.context.new_cdp_session(page)
        except Exception as e:
            self.cdp = None
            self.cast_error = str(e)[:200]
            return {"ok": False, "screencast_supported": False,
                    "error": f"no devtools session: {e}"}
        self.cast_frames = 0
        self.cast_dropped = 0
        self.cast_latest = None
        self.cast_at = 0.0
        every = max(33, min(int(every_ms or 50), 2000))
        self.cast_fps = round(1000.0 / every, 1)

        def _on_frame(params: dict, session_id=None):
            self.cast_frames += 1
            self.cast_latest = params.get("data") or ""
            self.cast_at = time.time()
            try:
                asyncio.run_coroutine_threadsafe(
                    cdp.send("Page.screencastFrameAck",
                             {"sessionId": params.get("sessionId", 0)}),
                    _loop())
            except Exception:
                pass

        cdp.on("Page.screencastFrame", _on_frame)
        try:
            await cdp.send("Page.startScreencast", {
                "format": "jpeg", "quality": 80,
                # Every frame the compositor makes. The pane serves the newest
                # one and drops the rest, so asking Chromium to skip frames
                # only makes a *repainted* page look frozen.
                "everyNthFrame": 1,
                "maxWidth": 1440, "maxHeight": 900,
            })
        except Exception as e:
            self.cdp = None
            return {"ok": False, "screencast_supported": False,
                    "error": f"screencast would not start: {e}"}
        self.cdp = cdp
        # One frame now, not "when the page next damages". A headless
        # compositor emits nothing for a page that is simply sitting there, so
        # without this the viewer is an empty box over a page that is
        # perfectly rendered — and an empty box is indistinguishable from a
        # blank page, which is the last thing a takeover surface should be.
        try:
            first = await cdp.send("Page.captureScreenshot",
                                   {"format": "jpeg", "quality": 60})
            self.cast_latest = first.get("data") or ""
            self.cast_at = time.time()
            self.cast_frames = 1 if self.cast_latest else 0
        except Exception as e:
            self.cast_error = f"the initial frame could not be captured: {e}"[:200]
        self.touch()
        return {"ok": True, "screencast_supported": True, "fps": self.cast_fps,
                "every_ms": every, "first_frame": bool(self.cast_latest)}

    async def screencast_frame(self) -> dict:
        """The newest frame only. Never a queue."""
        if getattr(self, "cdp", None) is None:
            return {"ok": False, "error": "no screencast is running",
                    "supported": False}
        if not self.cast_latest:
            return {"ok": True, "ready": False, "frames": self.cast_frames}
        return {"ok": True, "ready": True, "image": self.cast_latest,
                "frames": self.cast_frames, "at": self.cast_at,
                "url": getattr(self.page, "url", "")}

    async def stop_screencast(self) -> dict:
        cdp = getattr(self, "cdp", None)
        if cdp is None:
            return {"ok": True, "stopped": False}
        try:
            await cdp.send("Page.stopScreencast")
        except Exception:
            pass
        try:
            await cdp.detach()
        except Exception:
            pass
        self.cdp = None
        self.cast_latest = None
        return {"ok": True, "stopped": True,
                "frames": getattr(self, "cast_frames", 0)}


# ── small page helpers ────────────────────────────────────────────────
async def _safe_title(page) -> str:
    try:
        return (await asyncio.wait_for(page.title(), timeout=4.0) or "")[:200]
    except Exception:
        return ""


async def _text(page) -> str:
    try:
        return await asyncio.wait_for(page.inner_text("body"), timeout=12.0) or ""
    except Exception:
        try:
            return (await asyncio.wait_for(page.content(), timeout=12.0))[:TEXT_CAP]
        except Exception:
            return ""


async def _viewport(page, what: str) -> int:
    try:
        return int(await page.evaluate(
            f"() => window.{'innerWidth' if what == 'w' else 'innerHeight'}"))
    except Exception:
        return 0


# ── the façade the MCP tools call ─────────────────────────────────────
def _valid_sid(sid: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,64}$", str(sid or "")))


def _state_root() -> Path:
    """`ROOT` — the directory the artifact route resolves against.

    `/api/artifacts/{sid}/{path}` serves `ROOT/state/sessions/<sid>/browser`,
    so a screenshot has to be written under exactly that or the URL it hands
    back is a 404. Deriving it from `S9_STATE_DIR` would be tidier and wrong:
    the two are not the same directory when a test relocates one of them, and
    a pane showing a broken image is worse than a path spelled out here.
    """
    return Path(__file__).resolve().parent


def get(sid: str, root: Path | None = None, headed: bool | None = None) -> Browser:
    """This session's browser, starting it if there is none.

    Headed-ness is fixed for the life of a browser: turning an invisible
    context into a visible one is a restart, and a restart loses the login the
    person was in the middle of. `status()` reports which one is running and
    `restart_if_needed()` acts on the request, never silently.
    """
    if not _valid_sid(sid):
        raise ValueError(f"not a usable session id: {sid!r}")
    with _LOCK:
        b = _SESSIONS.get(sid)
        if b is not None and b.context is not None:
            if headed is not None and headed != b.headed:
                b.need_restart = True
            return b
        try:
            import flags
            want_headed = (bool(flags.is_enabled("browser.headed", False))
                           if headed is None else bool(headed))
        except Exception:
            want_headed = bool(headed) if headed is not None else False
        b = Browser(sid, headed=want_headed, root=root or _state_root())
        b.need_restart = False
        _SESSIONS[sid] = b
    try:
        _call(b.start, NAV_TIMEOUT_S)
    except Exception:
        with _LOCK:
            _SESSIONS.pop(sid, None)
        raise
    return b


def restart_if_needed(sid: str) -> bool:
    """Close a browser whose headed-ness changed, so the next call honours it."""
    with _LOCK:
        b = _SESSIONS.get(sid)
        if b is None or not getattr(b, "need_restart", False):
            return False
    close(sid)
    return True


def status(sid: str) -> dict:
    with _LOCK:
        b = _SESSIONS.get(sid)
    if b is None or b.context is None:
        return {"running": False, "sid": sid, "headed": None, "tabs": 0}
    return {"running": True, "sid": sid, "headed": b.headed,
            "tabs": len(b.pages), "active": b.active,
            "url": getattr(b.page, "url", ""),
            "actions": b.actions, "epoch": b.snap_epoch,
            "casting": b.cdp is not None,
            "cast_fps": b.cast_fps, "cast_frames": b.cast_frames,
            "cast_error": b.cast_error,
            "console_errors": sum(1 for r in b.page_ring().items
                                  if r.get("level") == "error"),
            "requests": len(b.page_net().items),
            "idle_in_s": max(0.0, round(b.idle_after - time.time(), 1)),
            "alive_s": round(time.time() - b.created, 1),
            "need_restart": bool(getattr(b, "need_restart", False))}


def close(sid: str) -> dict:
    with _LOCK:
        b = _SESSIONS.pop(sid, None)
    if b is None:
        return {"ok": True, "closed": False,
                "note": "this session has no browser open"}
    try:
        _call(b.stop, 20.0)
    except Exception as e:
        return {"ok": False, "closed": True, "error": str(e)}
    return {"ok": True, "closed": True, "sid": sid}


def sweep_idle() -> list:
    """Close browsers nobody has used in a while."""
    now = time.time()
    with _LOCK:
        stale = [sid for sid, b in _SESSIONS.items()
                 if b.context is not None and b.idle_after < now]
    for sid in stale:
        close(sid)
    return stale


def close_all() -> list:
    with _LOCK:
        sids = list(_SESSIONS)
    for sid in sids:
        close(sid)
    return sids


def _do(sid: str, method: str, timeout: float, /, *args, **kwargs):
    """Every verb that needs a page to exist.

    Deliberately `_peek`, not `get`: a browser is a Chromium process, and an
    OBSERVING call — the pane polling the console log, an agent asking what
    errors a page threw — must not mint one as a side effect of looking. The
    answer to "what did the page say" when nothing is open is "nothing is
    open", which is also the honest message for a model that forgot to call
    `browser_open`.
    """
    b = _peek(sid)
    if b is None:
        return {"ok": False, "not_open": True,
                "error": "no browser is open for this session — call "
                         "browser_open first"}

    async def _run():
        return await getattr(b, method)(*args, **kwargs)

    return _call(_run, timeout)


def open_url(sid: str, url: str, headed: bool | None = None) -> dict:
    b = get(sid, headed=headed)

    async def _run():
        return await b.goto(url)
    return _call(_run, NAV_TIMEOUT_S)


def snapshot(sid: str, max_elements: int = 120) -> dict:
    return _do(sid, "snapshot", ACTION_TIMEOUT_S, max_elements=max_elements)


def read(sid: str, kind: str = "text", limit: int = 6000) -> dict:
    return _do(sid, "read", ACTION_TIMEOUT_S, kind=kind, limit=limit)


def screenshot(sid: str, full: bool = False) -> dict:
    return _do(sid, "screenshot", ACTION_TIMEOUT_S, full=full)


def events(sid: str, kind: str = "console", errors_only: bool = False) -> dict:
    return _do(sid, "events", ACTION_TIMEOUT_S, kind=kind, errors_only=errors_only)


def wait(sid: str, selector: str = "", state: str = "visible",
         ms: int = 5000) -> dict:
    return _do(sid, "wait", ACTION_TIMEOUT_S + 6,
               selector=selector, state=state, ms=ms)


def act(sid: str, action: str, **kwargs) -> dict:
    return _do(sid, "act", ACTION_TIMEOUT_S + 8, action=action, **kwargs)


def tabs(sid: str) -> dict:
    return _do(sid, "tab_list", ACTION_TIMEOUT_S)


def tab_select(sid: str, index: int) -> dict:
    return _do(sid, "tab_select", ACTION_TIMEOUT_S, index=index)


def tab_new(sid: str, url: str = "") -> dict:
    b = get(sid)

    async def _run():
        return await b.new_tab(url)
    return _call(_run, NAV_TIMEOUT_S)


def back(sid: str) -> dict:
    return _do(sid, "back", NAV_TIMEOUT_S)


def forward(sid: str) -> dict:
    return _do(sid, "forward", NAV_TIMEOUT_S)


def reload(sid: str) -> dict:
    return _do(sid, "reload", NAV_TIMEOUT_S)


def click_xy(sid: str, x: float, y: float) -> dict:
    return _do(sid, "click_xy", ACTION_TIMEOUT_S, x=x, y=y)


def wheel_xy(sid: str, dx: float, dy: float) -> dict:
    return _do(sid, "wheel_xy", ACTION_TIMEOUT_S, dx=dx, dy=dy)


def type_text(sid: str, text: str) -> dict:
    return _do(sid, "type_text", ACTION_TIMEOUT_S, text=text)


def press_key(sid: str, key: str) -> dict:
    return _do(sid, "press_key", ACTION_TIMEOUT_S, key=key)


def tab_close(sid: str, index: int) -> dict:
    return _do(sid, "tab_close", ACTION_TIMEOUT_S, index=index)


def evaluate(sid: str, script: str) -> dict:
    return _do(sid, "eval_js", ACTION_TIMEOUT_S, script=script)


def start_cast(sid: str, every_ms: int = 250) -> dict:
    return _do(sid, "start_screencast", ACTION_TIMEOUT_S, every_ms=every_ms)


def cast_frame(sid: str) -> dict:
    b = _peek(sid)
    if b is None:
        return {"ok": False, "error": "this session has no browser open",
                "supported": False}

    async def _run():
        return await b.screencast_frame()
    return _call(_run, 5.0)


def stop_cast(sid: str) -> dict:
    b = _peek(sid)
    if b is None:
        return {"ok": True, "stopped": False}

    async def _run():
        return await b.stop_screencast()
    return _call(_run, 10.0)


def _peek(sid: str):
    """The session's browser if one exists, without starting one.

    A pane polling `/frame` must not be able to mint a Chromium as a side
    effect of looking; only an explicit open, navigate or tool call does that.
    """
    with _LOCK:
        b = _SESSIONS.get(sid)
    return b if (b is not None and b.context is not None) else None


def shots(sid: str, limit: int = 24) -> dict:
    """The screenshots this session's browser has filed, newest first.

    Read from the artifact directory rather than from memory, so the filmstrip
    is the same evidence a reload or a second tab sees — and the same bytes the
    `/api/artifacts` route serves.
    """
    safe = sid if _valid_sid(sid) else "default"
    d = _state_root() / "state" / "sessions" / safe / "browser" / "studio"
    try:
        files = sorted((p for p in d.glob("*.png") if p.is_file()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return {"ok": True, "shots": [], "dir": str(d), "error": ""}
    cap = max(1, min(int(limit or 24), 200))
    rows = [{"name": p.name,
             "url": f"/api/artifacts/{safe}/studio/{p.name}",
             "at": round(p.stat().st_mtime, 3),
             "bytes": p.stat().st_size} for p in files[:cap]]
    return {"ok": True, "shots": rows, "count": len(rows),
            "more": max(0, len(files) - cap), "dir": str(d)}


_SWEEP_THREAD: threading.Thread | None = None


def _sweeper() -> None:
    while True:
        try:
            sweep_idle()
        except Exception:
            pass
        time.sleep(60.0)


def start_sweeper() -> None:
    """A browser is a Chromium process. A session left open in a browser tab
    must not leave one running forever, so idle ones are reaped."""
    global _SWEEP_THREAD
    with _LOCK:
        if _SWEEP_THREAD is not None and _SWEEP_THREAD.is_alive():
            return
        _SWEEP_THREAD = threading.Thread(target=_sweeper, daemon=True,
                                         name="aria-browser-sweep")
        _SWEEP_THREAD.start()

