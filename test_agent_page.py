"""Open and test the Aria agent page with Playwright."""
import asyncio
import json

from playwright.async_api import async_playwright

BASE = "http://localhost:8500"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        findings = []

        def find(category, msg):
            findings.append(f"[{category}] {msg}")

        # Open the page
        await page.goto(f"{BASE}/", wait_until="networkidle", timeout=30000)
        find("page", f"title={await page.title()!r}")

        # Check all view tabs
        tabs = await page.locator(".views-nav .tab").all()
        tab_labels = []
        for t in tabs:
            label = await t.inner_text()
            tab_labels.append(label.strip())
        find("ui", f"tabs={tab_labels}")

        # Check that all expected panels exist
        panels = {
            "costPanel": "Spend",
            "schedPanel": "Schedule",
            "memPanel": "Memory",
            "compPanel": "Computer",
        }
        for pid, label in panels.items():
            el = page.locator(f"#{pid}")
            if await el.count() == 0:
                find("BUG", f"Missing panel: {pid} ({label})")
            else:
                find("panel", f"{pid} exists")

        # Test quick action chips
        chips = ["costToggle", "schedToggle", "memToggle", "compToggle"]
        for cid in chips:
            chip = page.locator(f"#{cid}")
            if await chip.count() == 0:
                find("BUG", f"Missing chip: {cid}")
            else:
                await chip.click()
                await page.wait_for_timeout(300)
                # Check if panel opened
                panel_id = {"costToggle": "costPanel", "schedToggle": "schedPanel",
                            "memToggle": "memPanel", "compToggle": "compPanel"}[cid]
                panel = page.locator(f"#{panel_id}")
                hidden = await panel.get_attribute("hidden")
                if hidden is not None:
                    find("BUG", f"Panel {panel_id} did not open after clicking {cid}")
                else:
                    find("panel", f"{panel_id} opened")
                # Close
                await chip.click()
                await page.wait_for_timeout(200)

        # Test theme toggle
        theme_btn = page.locator("#themeBtn")
        if await theme_btn.count():
            initial = await page.evaluate("document.documentElement.dataset.theme")
            await theme_btn.click()
            after = await page.evaluate("document.documentElement.dataset.theme")
            find("ui", f"theme={initial}->{after}")
        else:
            find("BUG", "Missing theme button")

        # Test command palette
        await page.keyboard.press("Control+k")
        palette = page.locator("#paletteWrap")
        if await palette.count():
            hidden = await palette.get_attribute("hidden")
            if hidden is not None:
                find("BUG", "Palette did not open on Ctrl+K")
            else:
                find("ui", "palette opened")
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(100)
        else:
            find("BUG", "Missing palette")

        # Send a message and observe streaming
        textarea = page.locator("#input")
        await textarea.fill("What is 2+2? Reply with just the number.")
        await page.locator("#send").click()

        # Wait up to 60s for the assistant answer to appear
        answer = page.locator(".msg.assistant .answer").last
        try:
            await answer.wait_for(timeout=60000)
            text = await answer.inner_text()
            find("chat", f"reply={text!r}")
            if not text.strip():
                find("BUG", "Assistant reply is empty after waiting")
            elif "4" not in text:
                find("BUG", f"Expected '4' in reply, got: {text!r}")
        except Exception as e:
            find("BUG", f"Assistant answer never appeared: {e}")

        # Check DAG after chat
        await page.wait_for_timeout(2000)
        dag = page.locator("#dag")
        if await dag.count():
            dag_html = await dag.inner_html()
            if "No run yet" in dag_html:
                find("BUG", "DAG still shows 'No run yet' after completed chat")
            elif "<svg" not in dag_html:
                find("BUG", "DAG has no SVG after chat")
            else:
                find("dag", "SVG rendered")

        # Check sessions view
        await page.locator(".views-nav .tab[data-view='sessions']").click()
        await page.wait_for_timeout(1000)
        sess_table = page.locator("#sessTable")
        if await sess_table.count():
            txt = await sess_table.inner_text()
            if "Loading" in txt and "What is 2+2?" not in txt:
                find("BUG", "Sessions table stuck on Loading")

        # Check analytics
        await page.locator(".views-nav .tab[data-view='analytics']").click()
        await page.wait_for_timeout(500)
        stat_cards = page.locator("#statCards")
        if await stat_cards.count():
            txt = await stat_cards.inner_text()
            find("analytics", f"stat_cards={txt[:100]!r}")

        # Check tools
        await page.locator(".views-nav .tab[data-view='tools']").click()
        await page.wait_for_timeout(500)
        tools_grid = page.locator("#toolsGrid")
        if await tools_grid.count():
            txt = await tools_grid.inner_text()
            if "Loading" in txt:
                find("BUG", "Tools grid stuck on Loading")
            else:
                find("tools", f"loaded={len(txt)} chars")

        # Check config
        await page.locator(".views-nav .tab[data-view='config']").click()
        await page.wait_for_timeout(500)
        config_pre = page.locator("#configPre")
        if await config_pre.count():
            txt = await config_pre.inner_text()
            if txt.strip() == "loading…":
                find("BUG", "Config stuck on 'loading…'")
            else:
                find("config", f"loaded={len(txt)} chars")

        # Check computer view
        await page.locator(".views-nav .tab[data-view='computer']").click()
        await page.wait_for_timeout(500)
        cu_cap = page.locator("#cuCap")
        if await cu_cap.count():
            txt = await cu_cap.inner_text()
            find("computer", f"cap={txt!r}")

        # Check templates
        await page.locator(".views-nav .tab[data-view='templates']").click()
        await page.wait_for_timeout(500)
        tmpl_list = page.locator("#tmplList")
        if await tmpl_list.count():
            txt = await tmpl_list.inner_text()
            find("templates", f"list={txt[:100]!r}")

        # Check new chat button
        new_chat = page.locator("#newChat")
        if await new_chat.count():
            find("ui", "new chat button exists")
        else:
            find("BUG", "Missing new chat button")

        # Check suggestions
        suggestions = page.locator("#suggestions")
        if await suggestions.count():
            chips = await suggestions.locator(".chip").all()
            find("ui", f"suggestion chips={len(chips)}")
        else:
            find("BUG", "Missing suggestions")

        # Console errors
        console_errs = []
        page.on("console", lambda msg: console_errs.append(msg) if msg.type == "error" else None)
        await page.reload(wait_until="networkidle", timeout=30000)
        if console_errs:
            for e in console_errs[:5]:
                find("console", f"ERROR: {e.text}")
        else:
            find("console", "no errors")

        await browser.close()

    print("\n=== AGENT PAGE TEST REPORT ===\n")
    bugs = [f for f in findings if "BUG" in f]
    for f in findings:
        print(f)
    print(f"\nTotal: {len(findings)} items")
    print(f"BUGS: {len(bugs)}")
    for b in bugs:
        print(f"  - {b}")


if __name__ == "__main__":
    asyncio.run(main())
