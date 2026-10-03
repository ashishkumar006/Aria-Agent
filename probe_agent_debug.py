"""Probe the agent page — debug SSE and DAG."""
import asyncio
import json

from playwright.async_api import async_playwright

BASE = "http://localhost:8500"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        # Log all console messages
        def on_console(msg):
            print(f"CONSOLE {msg.type}: {msg.text}")

        page.on("console", on_console)

        # Log all requests
        def on_request(req):
            if "/api/" in req.url:
                print(f"REQUEST {req.method} {req.url}")

        page.on("request", on_request)

        # Log all responses
        async def on_response(resp):
            if "/api/" in resp.url:
                print(f"RESPONSE {resp.status} {resp.url}")
                try:
                    txt = await resp.text()
                    print(f"  BODY (first 300): {txt[:300]!r}")
                except Exception as e:
                    print(f"  BODY read error: {e}")

        page.on("response", on_response)

        await page.goto(f"{BASE}/", wait_until="networkidle", timeout=30000)
        print("PAGE LOADED")

        # Send a message
        textarea = page.locator("#input")
        await textarea.fill("2+2 equals?")
        await page.locator("#send").click()
        print("MESSAGE SENT")

        # Wait for completion
        await page.wait_for_timeout(45000)

        # Check final answer
        answer = page.locator(".msg.assistant .answer").last
        text = await answer.inner_text()
        print(f"FINAL ANSWER: {text!r}")

        # Check DAG
        dag = page.locator("#dag")
        dag_html = await dag.inner_html()
        print(f"DAG HTML (first 300): {dag_html[:300]!r}")

        # Direct API check
        r = await page.request.get(f"{BASE}/api/sessions?limit=5")
        sessions = await r.json()
        print(f"SESSIONS: {json.dumps(sessions, indent=2)[:800]}")

        # Check graph for the latest session
        if sessions.get("sessions"):
            sid = sessions["sessions"][0]["session_id"]
            r2 = await page.request.get(f"{BASE}/api/sessions/{sid}/graph")
            gd = await r2.json()
            print(f"GRAPH for {sid}: nodes={len(gd.get('nodes', []))}, edges={len(gd.get('edges', []))}")
            print(f"  NODES: {json.dumps(gd.get('nodes', [])[:3], indent=2)}")
            print(f"  EDGES: {json.dumps(gd.get('edges', [])[:3], indent=2)}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
