"""Probe the Aria agent page with Playwright and report UI/API inconsistencies."""
import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "http://localhost:8500"
REPORT = []


def note(category, msg):
    REPORT.append(f"[{category}] {msg}")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        # ── 1. Load the page ────────────────────────────────────────────────
        await page.goto(f"{BASE}/", wait_until="networkidle", timeout=30000)
        title = await page.title()
        note("page", f"title={title!r}")
        assert "Aria" in title, f"Unexpected title: {title}"

        # ── 2. Static asset sanity ─────────────────────────────────────────
        for url in ["/app.js", "/style.css"]:
            r = await page.request.get(f"{BASE}{url}")
            note("asset", f"{url} -> {r.status}")
            assert r.status == 200, f"Missing asset {url}: {r.status}"

        # ── 3. API health probe ────────────────────────────────────────────
        r = await page.request.get(f"{BASE}/api/health")
        d = await r.json()
        note("api", f"/api/health -> {d}")
        assert d.get("agent") == "ready"

        # ── 4. Sidebar elements ────────────────────────────────────────────
        sidebar = page.locator("#sidebar")
        assert await sidebar.count() == 1
        note("sidebar", "present")

        # ── 5. Views nav tabs ──────────────────────────────────────────────
        tabs = await page.locator(".views-nav .tab").all()
        note("ui", f"view tabs count={len(tabs)}")
        expected_views = {"chat", "sessions", "analytics", "tools", "config", "computer", "templates"}
        actual_views = set()
        for t in tabs:
            v = await t.get_attribute("data-view")
            if v:
                actual_views.add(v)
        note("ui", f"view tabs data-view={sorted(actual_views)}")
        missing = expected_views - actual_views
        if missing:
            note("INCONSISTENCY", f"Missing view tabs: {missing}")

        # ── 6. Chat composer ───────────────────────────────────────────────
        composer = page.locator("#composer")
        assert await composer.count() == 1
        textarea = page.locator("#input")
        assert await textarea.count() == 1

        # ── 7. Send a message and stream response ──────────────────────────
        await textarea.fill("What is 2+2? Reply with the number only.")
        await page.locator("#send").click()
        # Wait for the newest assistant bubble to appear (last one)
        answer = page.locator(".msg.assistant .answer").last
        await answer.wait_for(timeout=60000)
        text = await answer.inner_text()
        note("chat", f"assistant reply={text!r}")
        if "4" not in text:
            note("INCONSISTENCY", f"Expected '4' in reply, got: {text!r}")

        # ── 8. Session list API ────────────────────────────────────────────
        r = await page.request.get(f"{BASE}/api/sessions?limit=10")
        assert r.status == 200
        sessions = await r.json()
        note("api", f"/api/sessions -> {len(sessions.get('sessions', []))} sessions")

        # ── 9. Sessions view ───────────────────────────────────────────────
        await page.locator(".views-nav .tab[data-view='sessions']").click()
        await page.wait_for_timeout(500)
        table = page.locator("#sessTable")
        if await table.count():
            txt = await table.inner_text()
            note("ui", f"sessions table text={txt[:120]!r}")
            if "Loading" in txt and not sessions.get("sessions"):
                note("INCONSISTENCY", "Sessions view stuck on Loading with no sessions")

        # ── 10. Analytics view ─────────────────────────────────────────────
        await page.locator(".views-nav .tab[data-view='analytics']").click()
        await page.wait_for_timeout(500)
        stat_cards = page.locator("#statCards")
        if await stat_cards.count():
            txt = await stat_cards.inner_text()
            note("ui", f"analytics stat cards text={txt[:120]!r}")

        # ── 11. Tools view ─────────────────────────────────────────────────
        await page.locator(".views-nav .tab[data-view='tools']").click()
        await page.wait_for_timeout(500)
        tools_grid = page.locator("#toolsGrid")
        if await tools_grid.count():
            txt = await tools_grid.inner_text()
            note("ui", f"tools grid text={txt[:120]!r}")

        # ── 12. Config view ────────────────────────────────────────────────
        await page.locator(".views-nav .tab[data-view='config']").click()
        await page.wait_for_timeout(500)
        config_pre = page.locator("#configPre")
        if await config_pre.count():
            txt = await config_pre.inner_text()
            note("ui", f"config pre text={txt[:120]!r}")
            if txt.strip() == "loading…":
                note("INCONSISTENCY", "Config view stuck on 'loading…'")

        # ── 13. Computer view ──────────────────────────────────────────────
        await page.locator(".views-nav .tab[data-view='computer']").click()
        await page.wait_for_timeout(500)
        cu_cap = page.locator("#cuCap")
        if await cu_cap.count():
            txt = await cu_cap.inner_text()
            note("ui", f"computer cap text={txt!r}")

        # ── 14. Templates view ─────────────────────────────────────────────
        await page.locator(".views-nav .tab[data-view='templates']").click()
        await page.wait_for_timeout(500)
        tmpl_list = page.locator("#tmplList")
        if await tmpl_list.count():
            txt = await tmpl_list.inner_text()
            note("ui", f"templates list text={txt[:120]!r}")

        # ── 15. Quick action panels ────────────────────────────────────────
        for chip_id, panel_id in [
            ("costToggle", "costPanel"),
            ("schedToggle", "schedPanel"),
            ("memToggle", "memPanel"),
            ("compToggle", "compPanel"),
        ]:
            chip = page.locator(f"#{chip_id}")
            if await chip.count():
                await chip.click()
                await page.wait_for_timeout(300)
                panel = page.locator(f"#{panel_id}")
                if await panel.count():
                    hidden = await panel.get_attribute("hidden")
                    note("panel", f"{chip_id} -> hidden={hidden}")
                    if hidden is not None:
                        note("INCONSISTENCY", f"Panel {panel_id} did not open after clicking {chip_id}")
                await chip.click()  # close again

        # ── 16. Side panel close ───────────────────────────────────────────
        side_close = page.locator("#sideClose")
        if await side_close.count():
            await side_close.click()
            await page.wait_for_timeout(200)

        # ── 17. Command palette ────────────────────────────────────────────
        await page.keyboard.press("Control+k")
        palette = page.locator("#paletteWrap")
        if await palette.count():
            hidden = await palette.get_attribute("hidden")
            note("palette", f"hidden after Ctrl+K={hidden}")
            if hidden is not None:
                note("INCONSISTENCY", "Command palette did not open on Ctrl+K")
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(100)

        # ── 18. DAG load after chat ────────────────────────────────────────
        dag = page.locator("#dag")
        if await dag.count():
            txt = await dag.inner_text()
            note("dag", f"dag text={txt[:120]!r}")
            if "No run yet" in txt:
                note("INCONSISTENCY", "DAG still shows 'No run yet' after a completed chat")

        # ── 19. Notifications endpoint ─────────────────────────────────────
        r = await page.request.get(f"{BASE}/api/notifications?limit=5")
        assert r.status == 200
        nd = await r.json()
        note("api", f"/api/notifications -> {len(nd.get('notifications', []))} entries")

        # ── 20. Cost endpoint ──────────────────────────────────────────────
        r = await page.request.get(f"{BASE}/api/cost")
        assert r.status == 200
        cd = await r.json()
        note("api", f"/api/cost -> totals={cd.get('totals')}")

        # ── 21. Tools endpoint ─────────────────────────────────────────────
        r = await page.request.get(f"{BASE}/api/tools")
        assert r.status == 200
        td = await r.json()
        note("api", f"/api/tools -> {len(td.get('tools', []))} tools")

        # ── 22. Config endpoint ────────────────────────────────────────────
        r = await page.request.get(f"{BASE}/api/config")
        assert r.status == 200
        cfg = await r.json()
        note("api", f"/api/config -> raw_len={len(cfg.get('raw', ''))}, parsed={cfg.get('parsed') is not None}")

        # ── 23. Browser console errors ─────────────────────────────────────
        console_errors = []
        page.on("console", lambda msg: console_errors.append(
            f"{msg.type}: {msg.text}" if msg.type == "error" else None))
        await page.reload(wait_until="networkidle", timeout=30000)
        console_errors = [m for m in console_errors if m]
        if console_errors:
            for e in console_errors[:10]:
                note("console", e)
        else:
            note("console", "no JS console errors")

        # ── 24. Network failures / 404s ────────────────────────────────────
        for url in ["/api/does-not-exist", "/static/missing.css"]:
            r = await page.request.get(f"{BASE}{url}")
            note("http", f"{url} -> {r.status}")
            if r.status == 200:
                note("INCONSISTENCY", f"Expected 404 for {url}, got 200")

        # ── 25. Theme toggle ───────────────────────────────────────────────
        theme_btn = page.locator("#themeBtn")
        if await theme_btn.count():
            initial = await page.evaluate("document.documentElement.dataset.theme")
            await theme_btn.click()
            await page.wait_for_timeout(200)
            after = await page.evaluate("document.documentElement.dataset.theme")
            note("ui", f"theme toggle: {initial} -> {after}")
            if initial == after:
                note("INCONSISTENCY", "Theme did not change after clicking theme button")

        # ── 26. Check for broken links in HTML ─────────────────────────────
        links = await page.locator("a[href]").all()
        bad_links = []
        for link in links[:30]:
            href = await link.get_attribute("href")
            if href and href.startswith("/") and not href.startswith("//"):
                r = await page.request.get(f"{BASE}{href}")
                if r.status >= 400:
                    bad_links.append((href, r.status))
        if bad_links:
            for href, st in bad_links[:10]:
                note("INCONSISTENCY", f"Broken link {href} -> {st}")

        await browser.close()

    print("\n=== AGENT PAGE PROBE REPORT ===\n")
    for line in REPORT:
        print(line)
    print(f"\nTotal: {len(REPORT)} items, {sum(1 for r in REPORT if 'INCONSISTENCY' in r)} inconsistencies")


if __name__ == "__main__":
    asyncio.run(main())
