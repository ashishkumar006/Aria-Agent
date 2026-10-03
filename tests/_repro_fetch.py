import asyncio, sys, json, traceback
sys.path.insert(0, ".")
from mcp_server import _crawl4ai_fetch

async def main():
    result = {}
    try:
        r = await _crawl4ai_fetch("https://en.wikipedia.org/wiki/Artemis_II")
        text = r.get("text", "")
        result = {
            "ok": True,
            "len": len(text),
            "has_arrow": "→" in text,
            "snippet": (text[text.find("→")-40:text.find("→")+40] if "→" in text else "")[:120],
            "status": r.get("status"),
        }
    except Exception:
        result = {"ok": False, "error": traceback.format_exc()[-1500:]}
    with open("_repro_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

asyncio.run(main())
