"""The researcher must hand the formatter evidence, not a digest.

Measured from the gateway ledger: 1,112 completed researcher finals, a median
of ~1,033 output tokens, and only 3.1% above 2,000 — while the node held 6-20
complete pages. Output did not grow with input. The fix is a second writing
pass over the pages the run actually fetched (`skills._sectioned_findings`),
so these tests cover the three properties that make it work: only real pages
count as sources, every page gets its own write-up, and the merged write-up
replaces a short `findings` without touching a long one.
"""
import asyncio
import json

import skills


def _page(url: str, text: str = "Evidence. " * 200, status: int = 200) -> str:
    return json.dumps({"status": status, "text": text, "url": url})


def _call(name: str, url: str, text: str = "Evidence. " * 200,
          ok: bool = True, status: int = 200) -> dict:
    return {"name": name, "arguments": {"url": url}, "ok": ok,
            "result": _page(url, text, status)}


def test_tool_sources_keeps_only_real_pages():
    """A failed fetch, a 4xx and a stub page are not material: writing them up
    would pad the findings with nothing, and a refetch is the same source."""
    transcript = [
        _call("fetch_url", "https://a.example/one"),
        _call("fetch_url", "https://a.example/dead", ok=False),
        _call("fetch_url", "https://a.example/missing", status=404),
        _call("fetch_url", "https://a.example/stub", text="Too short."),
        _call("fetch_url", "https://a.example/one"),          # refetch
        _call("web_search", "https://a.example/search"),      # not a page
        _call("fetch_pdf", "https://a.example/paper.pdf"),
        _call("extract_tables", "https://a.example/table"),
    ]
    got = skills._tool_sources(transcript)
    urls = [s["url"] for s in got]
    assert urls == ["https://a.example/one", "https://a.example/paper.pdf",
                    "https://a.example/table"], urls


def test_extract_tables_rows_reach_the_write_up_as_values():
    """A table's numbers are the evidence; a described table is not."""
    table = {"url": "https://a.example/table", "n_tables": 1,
             "tables": [{"headers": ["City", "Population"],
                         "rows": [["Rome", "2,873,000"],
                                  ["Milan", "1,371,000"]]}]}
    srcs = skills._tool_sources([{
        "name": "extract_tables", "arguments": {"url": table["url"]},
        "ok": True, "result": json.dumps(table)}])
    assert len(srcs) == 1
    assert "2,873,000" in srcs[0]["text"]
    assert "Milan" in srcs[0]["text"]


def test_sectioned_findings_writes_every_page_up_in_fetch_order():
    """Four pages at two per call means two calls, and the merged text keeps
    the order the pages were fetched in — not the order the calls finished."""
    class _FakeLLM:
        def __init__(self):
            self.prompts = []

        def chat(self, prompt=None, **_kw):
            self.prompts.append(prompt or "")
            pages = sorted(set(
                p.split('"')[0]
                for p in (prompt or "").split('"url": "https://example.com/')[1:]))
            return {"text": ("Write-up of "
                             + ", ".join(f"page {p}" for p in pages) + ". "
                             + "detail " * 60),
                    "input_tokens": 10, "output_tokens": 20}

    transcript = [_call("fetch_url", f"https://example.com/{i}")
                  for i in range(1, 5)]
    reg = skills.SkillRegistry()
    rs = reg.get("researcher")
    assert rs.sectioned_findings is True

    fake = _FakeLLM()
    orig = skills.LLM
    skills.LLM = lambda *a, **k: fake
    try:
        text, srcs, tin, tout = asyncio.run(skills._sectioned_findings(
            rs, "PROMPT", transcript, "s9-test"))
    finally:
        skills.LLM = orig

    assert len(srcs) == 4
    assert len(fake.prompts) == 2, len(fake.prompts)
    assert tin == 20 and tout == 40
    # In fetch order, no page dropped.
    assert "Write-up of page 1, page 2." in text
    assert "Write-up of page 3, page 4." in text
    # The instruction is the point: write up, do not digest.
    joined = "\n".join(fake.prompts)
    assert "WRITE UP THESE PAGES IN FULL" in joined
    assert "CANNOT open these pages" in joined
    assert "at least 800 words" in joined.lower(), "no falsifiable floor"
    assert "No headings" in joined


def test_sectioned_findings_degrades_to_the_single_call_answer():
    """A failed section call must not lose the pages the others wrote up, and
    a run with no usable pages must decline rather than invent text."""
    transcript = [_call("fetch_url", f"https://example.com/{i}")
                  for i in range(1, 4)]

    class _Flaky:
        def __init__(self):
            self.n = 0

        def chat(self, prompt=None, **_kw):
            self.n += 1
            if "https://example.com/1" in (prompt or ""):
                raise RuntimeError("gateway down")
            return {"text": "Surviving section. " * 40}

    reg = skills.SkillRegistry()
    rs = reg.get("researcher")
    orig = skills.LLM
    skills.LLM = lambda *a, **k: _Flaky()
    try:
        text, srcs, _tin, _tout = asyncio.run(skills._sectioned_findings(
            rs, "P", transcript, "s9-test"))
        empty = asyncio.run(skills._sectioned_findings(rs, "P", [], "s9-test"))
    finally:
        skills.LLM = orig
    assert len(srcs) == 3
    assert text.count("Surviving section.") > 0
    assert empty == ("", [], 0, 0)


def test_run_skill_replaces_a_digest_with_the_page_write_up():
    """End to end: a 120-word `findings` over 4 pages becomes the full
    write-up, and every fetched URL survives in `sources` — the formatter can
    only stitch evidence it was handed."""
    skill = skills.SkillRegistry().get("researcher")
    pages = [f"https://example.com/{i}" for i in range(1, 5)]
    transcript = [_call("fetch_url", u) for u in pages]
    short = {"question": "q",
             "sources": [{"url": pages[0], "title": "one"}],
             "findings": "A tidy digest. " * 40,
             "conflicts": [], "caveats": []}

    class _FakeLLM:
        def chat(self, prompt=None, **_kw):
            pages = sorted(set(
                p.split('"')[0]
                for p in (prompt or "").split('"url": "https://example.com/')[1:]))
            return {"text": ("Full write-up for "
                             + ", ".join(f"page {p}" for p in pages) + ". "
                             + "fact " * 300)}

    async def fake_run_with_tools(**_kw):
        return {"text": json.dumps(short), "provider": "test",
                "tool_calls": [], "transcript": transcript}

    import mcp_runner
    orig_rt = mcp_runner.run_with_tools
    orig_llm = skills.LLM
    mcp_runner.run_with_tools = fake_run_with_tools
    skills.LLM = lambda *a, **k: _FakeLLM()
    nodes = {"n:1": {"skill": "researcher", "inputs": {}, "status": "running"}}
    try:
        result, _rendered = asyncio.run(skills.run_skill(
            skill, "n:1", nodes, "s9-test", "what happened", None))
    finally:
        mcp_runner.run_with_tools = orig_rt
        skills.LLM = orig_llm

    assert result.success, result.error
    findings = result.output["findings"]
    assert skills._word_count(findings) > skills._word_count(short["findings"])
    for i in range(1, 5):
        assert f"page {i}" in findings, findings[:300]
    assert "A tidy digest" not in findings
    srcs = [s["url"] for s in result.output["sources"]]
    assert srcs == pages, srcs


def test_run_skill_keeps_a_findings_that_already_meets_the_floor():
    """No extra calls when the model already wrote the pages up: the pass is a
    repair for the digest failure, not a tax on every run."""
    skill = skills.SkillRegistry().get("researcher")
    pages = [f"https://example.com/{i}" for i in range(1, 3)]
    transcript = [_call("fetch_url", u) for u in pages]
    long_findings = "Real evidence. " * 900          # ~1,800 words
    body = {"question": "q", "sources": [{"url": u} for u in pages],
            "findings": long_findings, "conflicts": [], "caveats": []}

    class _Never:
        def chat(self, *a, **k):
            raise AssertionError("a complete findings was rewritten")

    async def fake_run_with_tools(**_kw):
        return {"text": json.dumps(body), "provider": "test",
                "tool_calls": [], "transcript": transcript}

    import mcp_runner
    orig_rt = mcp_runner.run_with_tools
    orig_llm = skills.LLM
    mcp_runner.run_with_tools = fake_run_with_tools
    skills.LLM = lambda *a, **k: _Never()
    nodes = {"n:1": {"skill": "researcher", "inputs": {}, "status": "running"}}
    try:
        result, _ = asyncio.run(skills.run_skill(
            skill, "n:1", nodes, "s9-test", "q", None))
    finally:
        mcp_runner.run_with_tools = orig_rt
        skills.LLM = orig_llm
    assert result.success, result.error
    assert result.output["findings"] == long_findings


def test_a_hop_cap_after_fetching_still_yields_findings():
    """Observed live: the model spent its turns fetching and never produced
    final text, so the node failed holding a dozen complete pages. The pages
    are the evidence; write them up instead of throwing them away."""
    skill = skills.SkillRegistry().get("researcher")
    pages = [f"https://example.com/{i}" for i in range(1, 4)]
    transcript = [_call("fetch_url", u) for u in pages]

    class _FakeLLM:
        def chat(self, prompt=None, **_kw):
            return {"text": "Recovered evidence. " * 120}

    async def capped(**_kw):
        return {"text": "", "tool_calls": [], "provider": "test",
                "error": "tool-use hop cap (6) reached without final text",
                "transcript": transcript}

    import mcp_runner
    orig_rt = mcp_runner.run_with_tools
    orig_llm = skills.LLM
    mcp_runner.run_with_tools = capped
    skills.LLM = lambda *a, **k: _FakeLLM()
    nodes = {"n:1": {"skill": "researcher", "inputs": {}, "status": "running"}}
    try:
        result, _ = asyncio.run(skills.run_skill(
            skill, "n:1", nodes, "s9-test", "the question", None))
    finally:
        mcp_runner.run_with_tools = orig_rt
        skills.LLM = orig_llm
    assert result.success, result.error
    assert "Recovered evidence." in result.output["findings"]
    assert len(result.output["sources"]) == 3
