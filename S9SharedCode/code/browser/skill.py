"""Session 9: the Browser skill — cascade wrapper around the layered drivers.

The wrapper translates the orchestrator's NodeSpec contract into the
typed BrowserOutput / AgentResult contract, and owns the layer cascade:

    Layer 1  — HTML extract via trafilatura (no LLM)
    Layer 2a — deterministic selectors (only if metadata.selectors is given)
    Layer 2b — A11yDriver        (text-only, V9 /v1/chat)
    Layer 3  — SetOfMarksDriver  (vision, V9 /v1/vision)

Escalation rule: a layer escalates when its output is empty or evidently
insufficient. The skill stops at the first layer that produces a useful
answer.

Gateway-access is a first-class failure: if Layer 1's fetch returns a
known CAPTCHA / login-wall / hCaptcha marker, the skill returns
immediately with error_code="gateway_blocked" and does not attempt
the later layers. The orchestrator's recovery path picks this up via
the failure_report (it contains the literal token "gateway_blocked")
and re-invokes the Planner.

This file is the ONLY new code in the integration. The four files
already on disk (client.py, dom.py, highlight.py, driver.py) are
ported verbatim from S9SharedCode/code/browser/ and untouched.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import trafilatura
from playwright.async_api import async_playwright

from schemas import AgentResult, BrowserOutput, NodeSpec

from .client import V9Client
from .driver import A11yDriver, DriverConfig, DriverResult, SetOfMarksDriver


# ── gateway-block detection ──────────────────────────────────────────────────
# Kept here (next to the cascade that uses it) rather than mutating the
# ported dom.py.  Short, obvious patterns — when this list grows past a
# screenful we should consolidate, but for now explicit is better.
_GATEWAY_BLOCK_MARKERS = (
    # Generic CAPTCHA / hCaptcha / reCAPTCHA. Needles MUST be specific
    # enough that an article ABOUT captchas does not false-positive — we
    # match on class/attribute strings the widgets emit, not their names.
    ("captcha",                "Let's confirm you are human"),
    ("captcha",                "Enter the characters you see below"),
    ("captcha",                "Robot Check"),
    ("captcha",                "Please verify you are a human"),
    ("captcha",                "/errors/validateCaptcha"),
    ("hcaptcha",               'class="h-captcha"'),
    ("hcaptcha",               "data-hcaptcha-widget-id"),
    ("recaptcha",              'class="g-recaptcha"'),
    ("recaptcha",              "g-recaptcha-response"),
    # Cloudflare interstitials.
    ("cloudflare",             "Checking your browser before accessing"),
    ("cloudflare",             "cf-browser-verification"),
    ("cloudflare",             "cf-challenge-running"),
    # Login walls.  Conservative — only the literal sign-in-required pages.
    ("login_wall",             "You must be logged in"),
    ("login_wall",             "Sign in to continue"),
    ("login_wall",             "Please log in to continue"),
)


def detect_gateway_block(html: str) -> str | None:
    """Return the block type when `html` looks like a gateway-access page
    (CAPTCHA / Cloudflare / login wall), else None. Conservative — false
    positives would mis-route real content to recovery."""
    if not html:
        return None
    h = html.lower()
    for kind, needle in _GATEWAY_BLOCK_MARKERS:
        if needle.lower() in h:
            return kind
    return None


# ── Layer 1: pure-HTTP extraction ────────────────────────────────────────────
_UA = (
    "Mozilla/5.0 (compatible; S9-Browser-Skill/0.1; +llm_gatewayV9)"
)


async def _fetch_html(url: str, timeout: float = 30.0) -> tuple[str, str]:
    """Returns (html, final_url)."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 headers={"User-Agent": _UA}) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.text, str(r.url)


def _extract(html: str) -> str:
    text = trafilatura.extract(
        html, include_links=True, include_formatting=False, favor_recall=True,
    )
    return (text or "").strip()


# ── Layer 1b: structured card extraction (DOM) ───────────────────────────────
# Supplement to the prose extraction above. On list/ranking pages (model
# galleries, search results, leaderboards) the meaningful entities live in
# card elements whose names trafilatura frequently drops from the body text.
# We pull those cards' verbatim innerText + entity id straight from the DOM
# so the downstream distiller/critic see faithful names instead of prose the
# article body happened to contain. This is a Browser-skill extension only —
# the orchestrator (flow.py) is untouched.
_STRUCT_START = "\n\n--- STRUCTURED CARD DATA (verbatim from rendered DOM) ---\n"
_STRUCT_END = "\n--- END STRUCTURED CARD DATA ---\n"


async def _launch_browser(p, retries: int = 2):
    """Launch headless Chromium with a small retry.

    A bare `p.chromium.launch()` occasionally fails with
    'Connection closed while reading from t' — the browser process died on
    startup (typically OOM or a leftover zombie from a prior run). Retrying
    with a fresh launch almost always succeeds, so we don't let a transient
    launch crash abort the whole skill run.
    """
    last = None
    for attempt in range(1, retries + 1):
        try:
            return await p.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                await asyncio.sleep(1.0 * attempt)
    raise last


async def _structured_extract(page) -> str:
    """Return a delimited block of card rows (name + figures) from the live
    page, or '' when the page has no card-like structure. Best-effort and
    fully exception-safe: any failure yields '' so the cascade never breaks."""
    try:
        rows = await page.evaluate(
            r"""() => {
              const out = [];
              const cards = Array.from(document.querySelectorAll('article'));
              const src = cards.length
                ? cards
                : Array.from(document.querySelectorAll(
                    'main [role="listitem"], main li, [class*="grid"] > *'));
              for (const el of src.slice(0, 40)) {
                const link = el.matches('a')
                  ? el
                  : (el.querySelector('a[href^="/"]') || el.querySelector('a'));
                const id = link ? (link.getAttribute('href') || '') : '';
                const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
                if (text) {
                  out.push(text + (id.startsWith('/') ? '  [id:' + id + ']' : ''));
                }
              }
              return out.slice(0, 30);
            }"""
        )
        if not rows:
            return ""
        return _STRUCT_START + "\n".join(rows) + _STRUCT_END
    except Exception:                       # noqa: BLE001
        return ""


def _is_useful_extract(content: str, goal: str) -> bool:
    """Coarse usefulness check. We trust the gateway/recovery to catch
    genuine no-content failures; this gate only filters obvious nothing
    (< ~200 chars) or the case where the page rendered but the goal asks
    for an interaction (`click`, `fill`, `select`, etc.) — for those goals
    extraction is never sufficient regardless of content length."""
    if len(content) < 200:
        return False
    interactive_verbs = ("click", "fill", "select", "type", "drag",
                         "filter", "sort", "submit", "navigate")
    if any(v in goal.lower() for v in interactive_verbs):
        return False
    return True


# ── the skill ────────────────────────────────────────────────────────────────
class BrowserSkill:
    NAME = "browser"

    def __init__(self, *, gateway_url: str = "http://localhost:8109",
                 agent_tag: str = "browser",
                 a11y_provider_pin: str | None = "gemini",
                 vision_provider_pin: str | None = None,
                 artifacts_root: str | None = None,
                 max_steps_a11y: int = 12,
                 max_steps_vision: int = 12,
                 wall_clock_s: float = 90.0,
                 session: str | None = None):
        self.gateway_url = gateway_url
        self.agent_tag = agent_tag
        self.a11y_provider_pin = a11y_provider_pin
        self.vision_provider_pin = vision_provider_pin
        self.artifacts_root = Path(artifacts_root) if artifacts_root else None
        self.max_steps_a11y = max_steps_a11y
        self.max_steps_vision = max_steps_vision
        self.wall_clock_s = wall_clock_s
        # Forwarded to V9 so the gateway ledger can attribute each call to
        # the orchestrator session that drove it.
        self.session = session

    def _deadline_exceeded(self, t0: float) -> bool:
        return (time.time() - t0) >= self.wall_clock_s

    def _remaining(self, t0: float) -> float:
        return max(1.0, self.wall_clock_s - (time.time() - t0))

    # ── public entry point ─────────────────────────────────────────────────
    async def run(self, node: NodeSpec) -> AgentResult:
        url = node.metadata.get("url") or (node.inputs[0] if node.inputs else "")
        goal = node.metadata.get("goal") or "extract main content"
        # Optional escape hatch: skip the natural cascade and pin to a specific
        # layer. Values: 'extract' | 'a11y' | 'vision'. Anything else is ignored.
        # 'extract' never escalates; 'a11y' never runs vision; 'vision' skips a11y.
        force_path = node.metadata.get("force_path")
        if not url:
            return self._pack_error("", goal, "interaction_failed",
                                    "no url given (metadata.url or inputs[0])",
                                    path="extract")
        t0 = time.time()
        import uuid as _uuid
        client = V9Client(base_url=self.gateway_url, agent=self.agent_tag,
                          session=self.session)
        artifacts_dir = (
            str(self.artifacts_root / f"browser_{int(t0)}_{_uuid.uuid4().hex[:6]}")
            if self.artifacts_root else None
        )

        # ── Layer 1: extract ────────────────────────────────────────────────
        layer1_http_error: str | None = None
        try:
            html, final_url = await _fetch_html(url)
        except httpx.HTTPError as e:
            layer1_http_error = f"layer1 fetch failed: {e}"
            html, final_url = "", url
        except Exception as e:  # DNS, invalid URL, etc — fail soft
            return self._pack_error(url, goal, "interaction_failed",
                                    f"layer1 fetch failed: {type(e).__name__}: {e}",
                                    elapsed=time.time() - t0, path="extract")

        if html:
            block = detect_gateway_block(html)
            if block:
                return self._pack_error(url, goal, "gateway_blocked",
                                        f"gateway_blocked: {block} marker on {final_url}",
                                        elapsed=time.time() - t0, path="extract")
            content = _extract(html)
            if _is_useful_extract(content, goal):
                return self._pack(url, goal, "extract", turns=0,
                                  content=content, final_url=final_url,
                                  elapsed=time.time() - t0)

        # force_path=extract: return L1 verbatim, never escalate.
        if force_path == "extract":
            content = _extract(html) if html else ""
            if content:
                return self._pack(url, goal, "extract", turns=0,
                                  content=content, final_url=final_url,
                                  elapsed=time.time() - t0)
            return self._pack_error(url, goal, "extraction_failed",
                                    layer1_http_error or "extract forced but no content",
                                    elapsed=time.time() - t0, path="extract")

        if self._deadline_exceeded(t0):
            return self._pack_error(url, goal, "timeout",
                                    f"wall-clock {self.wall_clock_s}s exceeded before L2",
                                    elapsed=time.time() - t0, path="extract")

        # ── Layer 2a: deterministic selectors (only if caller gave any) ────
        selectors = node.metadata.get("selectors") or []
        if selectors:
            try:
                det = await asyncio.wait_for(
                    self._try_deterministic(url, goal, selectors),
                    timeout=self._remaining(t0),
                )
            except asyncio.TimeoutError:
                return self._pack_error(url, goal, "timeout",
                                        f"wall-clock {self.wall_clock_s}s exceeded in L2a",
                                        elapsed=time.time() - t0, path="deterministic")
            except Exception as e:
                det = None
                layer1_http_error = (layer1_http_error or "") + f" | L2a error: {e}"
            if det is not None:
                return det if det.success else self._pack_error(
                    url, goal, "interaction_failed",
                    det.error or "deterministic path failed",
                    elapsed=time.time() - t0, path="deterministic",
                )

        # ── Layer 2b: a11y ──────────────────────────────────────────────────
        if force_path == "vision":
            a11y_result = DriverResult(success=False, note="skipped by force_path=vision")
        else:
            try:
                a11y_result = await asyncio.wait_for(
                    self._drive(
                        A11yDriver, url, goal, client, artifacts_dir,
                        self.a11y_provider_pin, self.max_steps_a11y,
                        layer_agent="browser:a11y",
                    ),
                    timeout=self._remaining(t0),
                )
            except asyncio.TimeoutError:
                return self._pack_error(url, goal, "timeout",
                                        f"wall-clock {self.wall_clock_s}s exceeded in a11y",
                                        elapsed=time.time() - t0, path="a11y")
            except Exception as e:
                a11y_result = DriverResult(success=False,
                                           note=f"a11y driver error: {type(e).__name__}: {e}")
        if a11y_result.gateway_blocked:
            return self._pack_error(url, goal, "gateway_blocked",
                                    a11y_result.note or "gateway_blocked after JS render",
                                    elapsed=time.time() - t0, path="a11y")
        if a11y_result.success:
            return self._pack_driver("a11y", url, goal, a11y_result,
                                     final_url=a11y_result.final_url,
                                     elapsed=time.time() - t0)
        # force_path=a11y: never run vision.
        if force_path == "a11y":
            return self._pack_error(url, goal, "interaction_failed",
                                    f"a11y forced but failed: {a11y_result.note}",
                                    elapsed=time.time() - t0, path="a11y")

        if self._deadline_exceeded(t0):
            return self._pack_error(url, goal, "timeout",
                                    f"wall-clock {self.wall_clock_s}s exceeded before vision",
                                    elapsed=time.time() - t0, path="a11y")

        # ── Layer 3: vision ─────────────────────────────────────────────────
        try:
            vis_result = await asyncio.wait_for(
                self._drive(
                    SetOfMarksDriver, url, goal, client, artifacts_dir,
                    self.vision_provider_pin, self.max_steps_vision,
                    layer_agent="browser:vision",
                ),
                timeout=self._remaining(t0),
            )
        except asyncio.TimeoutError:
            return self._pack_error(url, goal, "timeout",
                                    f"wall-clock {self.wall_clock_s}s exceeded in vision",
                                    elapsed=time.time() - t0, path="vision")
        except Exception as e:
            vis_result = DriverResult(success=False,
                                      note=f"vision driver error: {type(e).__name__}: {e}")
        if vis_result.gateway_blocked:
            return self._pack_error(url, goal, "gateway_blocked",
                                    vis_result.note or "gateway_blocked after JS render",
                                    elapsed=time.time() - t0, path="vision")
        if vis_result.success:
            return self._pack_driver("vision", url, goal, vis_result,
                                     final_url=vis_result.final_url,
                                     elapsed=time.time() - t0)

        last_err = (vis_result.note or a11y_result.note
                    or layer1_http_error or "all layers exhausted")
        # Distinguish VLM outage from plain interaction failure.
        code = "interaction_failed"
        if "vision" in last_err.lower() and ("503" in last_err or "429" in last_err):
            code = "vlm_unavailable"
        return self._pack_error(url, goal, code,
                                f"all layers exhausted; last: {last_err}",
                                elapsed=time.time() - t0, path="vision")

    # ── per-layer driver runs ──────────────────────────────────────────────
    async def _drive(self, DriverCls, url, goal, client, artifacts_dir,
                     provider_pin, max_steps, layer_agent: str | None = None):
        # Place each layer's per-turn artifacts under its own subdir so
        # turn_##_* filenames from one layer don't overwrite another's.
        if artifacts_dir:
            from pathlib import Path as _P
            sub = _P(artifacts_dir) / DriverCls.LAYER_NAME
            sub.mkdir(parents=True, exist_ok=True)
            artifacts_dir = str(sub)
        # Per-layer ledger tagging so cost can be split L2b vs L3.
        if layer_agent:
            from copy import copy as _copy
            client = _copy(client)
            client.agent = layer_agent
        async with async_playwright() as p:
            browser = await _launch_browser(p)
            ctx = await browser.new_context(
                viewport={"width": 1366, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',"
                "{get:()=>undefined});"
            )
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                # Last-chance gateway-block check on the rendered page (some
                # walls only show up after JS executes).
                kind = detect_gateway_block(await page.content())
                if kind:
                    return DriverResult(
                        success=False,
                        note=f"gateway_blocked ({kind}) detected after JS render at {page.url}",
                        gateway_blocked=True,
                    )
                await asyncio.sleep(1.0)
                cfg = DriverConfig(
                    goal=goal, max_steps=max_steps, max_failures=3,
                    artifacts_dir=artifacts_dir, provider=provider_pin,
                )
                drv = DriverCls(page, client, cfg)
                # Augment the result with final_url + extracted text so
                # _pack_driver can fill BrowserOutput uniformly.
                result = await drv.run()
                result.final_url = page.url
                result.extracted = ""
                try:
                    result.extracted = _extract(await page.content())
                except Exception:                          # noqa: BLE001
                    pass
                result.turns = len(drv.steps)
                result.actions = [
                    {"turn": s.turn, "actions": s.actions, "outcome": s.outcome}
                    for s in drv.steps
                ]
                # Token/cost rollup for observability (previously dropped).
                try:
                    result.tokens_in = sum(getattr(s, "tokens_in", 0) for s in drv.steps)
                    result.tokens_out = sum(getattr(s, "tokens_out", 0) for s in drv.steps)
                except Exception:
                    pass
                # Augment the prose extraction with faithful, verbatim card
                # data from the DOM so the distiller never has to guess names.
                try:
                    structured = await _structured_extract(page)
                    if structured:
                        result.extracted = (result.extracted or "") + structured
                except Exception:                          # noqa: BLE001
                    pass
                # Enforce artifact size cap (~30MB per layer).
                try:
                    from pathlib import Path as _P2
                    if artifacts_dir:
                        total = sum(f.stat().st_size for f in _P2(artifacts_dir).glob("*") if f.is_file())
                        if total > 30_000_000:
                            # Prune oldest turn files, keep newest.
                            files = sorted(_P2(artifacts_dir).glob("turn_*"), key=lambda f: f.name)
                            while total > 25_000_000 and files:
                                f = files.pop(0)
                                try:
                                    total -= f.stat().st_size
                                    f.unlink()
                                except Exception:
                                    break
                except Exception:
                    pass
                return result
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    async def _try_deterministic(self, url, goal, selectors) -> AgentResult | None:
        """Runs caller-supplied selector instructions through Playwright. Each
        step is `{action, selector, value?}`. Returns AgentResult on success
        or None to let the cascade fall through to a11y."""
        browser = None
        try:
            async with async_playwright() as p:
                browser = await _launch_browser(p)
                ctx = await browser.new_context(
                    viewport={"width": 1366, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                )
                await ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',"
                    "{get:()=>undefined});"
                )
                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                # Rendered gateway-block check (parity with _drive).
                try:
                    kind = detect_gateway_block(await page.content())
                    if kind:
                        return None
                except Exception:
                    pass
                for i, step in enumerate(selectors, start=1):
                    sel = step.get("selector")
                    action = step.get("action")
                    if not sel or action not in ("click", "fill", "key"):
                        return None
                    loc = page.locator(sel).first
                    try:
                        await loc.wait_for(state="visible", timeout=8000)
                        try:
                            await loc.scroll_into_view_if_needed(timeout=2000)
                        except Exception:
                            pass
                    except Exception:                          # noqa: BLE001
                        return None
                    if action == "fill":
                        await loc.fill(step.get("value", ""))
                    elif action == "click":
                        await loc.click()
                    elif action == "key":
                        await page.keyboard.press(step.get("value", "Enter"))
                content = _extract(await page.content())
                final = page.url
                return self._pack(
                    url, goal, "deterministic", turns=len(selectors),
                    content=content, final_url=final, elapsed=0.0,
                )
        except Exception:                          # noqa: BLE001
            return None
        finally:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass

    # ── packers ────────────────────────────────────────────────────────────
    def _pack(self, url, goal, path, *, turns, content=None, actions=None,
              final_url=None, elapsed=0.0) -> AgentResult:
        out = BrowserOutput(
            url=url, goal=goal, path=path, turns=turns,
            content=content, actions=actions or [], final_url=final_url,
        )
        return AgentResult(
            success=True, agent_name=self.NAME,
            output=out.model_dump(), elapsed_s=elapsed,
        )

    def _pack_driver(self, path, url, goal, drv_result,
                     *, final_url, elapsed) -> AgentResult:
        out = BrowserOutput(
            url=url, goal=goal, path=path,
            turns=getattr(drv_result, "turns", 0) or 0,
            content=getattr(drv_result, "extracted", None) or None,
            actions=getattr(drv_result, "actions", []) or [],
            final_url=final_url,
        )
        # Cost rollup: sum per-turn tokens so the ledger + dashboard can show
        # real spend instead of 0.0. Provider tagged per-layer.
        tokens_in = 0
        tokens_out = 0
        try:
            for s in getattr(drv_result, "steps", []) or []:
                tokens_in += int(getattr(s, "tokens_in", 0) or 0)
                tokens_out += int(getattr(s, "tokens_out", 0) or 0)
        except Exception:
            pass
        out_dict = out.model_dump()
        out_dict["tokens_in"] = tokens_in
        out_dict["tokens_out"] = tokens_out
        return AgentResult(
            success=True, agent_name=self.NAME,
            output=out_dict, elapsed_s=elapsed,
            provider=f"browser:{path}",
            cost=float(tokens_in + tokens_out),
        )

    def _pack_error(self, url, goal, code, msg, *, elapsed=0.0,
                      path: str = "extract") -> AgentResult:
        out = BrowserOutput(
            url=url or "", goal=goal, path=path, turns=0, content=None,
        )
        return AgentResult(
            success=False, agent_name=self.NAME,
            output=out.model_dump(), error=msg, error_code=code,
            elapsed_s=elapsed,
        )
