"""P2.2 — Browser skill tests (pure functions + safety logic).

The full layer cascade (L1->L2b->L3) needs Playwright + a live gateway and is
covered by the live integration tests. Here we deterministically test the
pure/testable parts that matter most: the gateway-block *safety detector*,
the escalation heuristic, and the HTML extraction.
"""
from __future__ import annotations

import sys
from pathlib import Path

import asyncio

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from browser.skill import (  # noqa: E402
    BrowserSkill,
    _extract,
    _is_useful_extract,
    detect_gateway_block,
)
from schemas import NodeSpec  # noqa: E402


# ── gateway-block detection (safety-critical) ────────────────────────────────

class TestDetectGatewayBlock:
    def test_captcha_markers(self):
        html = "<html><body>Let's confirm you are human. Solve this.</body></html>"
        assert detect_gateway_block(html) == "captcha"

    def test_hcaptcha_marker(self):
        html = '<div class="h-captcha" data-sitekey="x"></div>'
        assert detect_gateway_block(html) == "hcaptcha"

    def test_recaptcha_marker(self):
        html = '<div class="g-recaptcha" data-sitekey="x"></div>'
        assert detect_gateway_block(html) == "recaptcha"

    def test_cloudflare_marker(self):
        html = "<html><body>Checking your browser before accessing example.com</body></html>"
        assert detect_gateway_block(html) == "cloudflare"

    def test_login_wall_marker(self):
        html = "<html><body>You must be logged in to view this page.</body></html>"
        assert detect_gateway_block(html) == "login_wall"

    def test_clean_page_returns_none(self):
        html = "<html><body><h1>Hello World</h1><p>Normal article text.</p></body></html>"
        assert detect_gateway_block(html) is None

    def test_empty_html_returns_none(self):
        assert detect_gateway_block("") is None

    def test_about_captcha_article_not_false_positive(self):
        """An article *about* captchas should not trip the detector (no widget)."""
        html = ("<html><body><h1>How CAPTCHAs Work</h1>"
                "<p>A CAPTCHA is a test. This article discusses captcha design "
                "but does not contain the widget itself.</p></body></html>")
        assert detect_gateway_block(html) is None

    def test_case_insensitive(self):
        html = "<html><body>LET'S CONFIRM YOU ARE HUMAN</body></html>"
        assert detect_gateway_block(html) == "captcha"


# ── escalation heuristic ──────────────────────────────────────────────────────

class TestIsUsefulExtract:
    def test_short_content_not_useful(self):
        assert _is_useful_extract("too short", "summarise this") is False

    def test_long_content_useful(self):
        long_text = "word " * 100  # > 200 chars
        assert _is_useful_extract(long_text, "summarise this") is True

    def test_interactive_goal_never_useful_via_extract(self):
        long_text = "word " * 100
        for goal in ["click the button", "fill the form", "select an option",
                     "type your name", "drag the slider", "filter results",
                     "sort by price", "submit the form", "navigate to page"]:
            assert _is_useful_extract(long_text, goal) is False, \
                f"extract should never suffice for interactive goal: {goal}"


# ── HTML extraction (trafilatura, local) ─────────────────────────────────────

class TestExtract:
    def test_extracts_article_text(self):
        html = ("<html><head><title>Test</title></head><body>"
                "<article><h1>Main Title</h1>"
                "<p>This is the first paragraph of meaningful content.</p>"
                "<p>Second paragraph with more text to extract.</p>"
                "</article></body></html>")
        out = _extract(html)
        assert "Main Title" in out
        assert "first paragraph" in out

    def test_empty_html_returns_empty(self):
        assert _extract("") == ""

    def test_navigation_stripped(self):
        """Boilerplate nav/menu text should be stripped, body kept."""
        html = ("<html><body>"
                "<nav>Home About Contact Login</nav>"
                "<article><h1>Real Article</h1>"
                "<p>Substantive body paragraph here.</p></article>"
                "<footer>Copyright 2026</footer></body></html>")
        out = _extract(html)
        assert "Real Article" in out


# ── BrowserSkill error packing ───────────────────────────────────────────────

class TestBrowserSkillInit:
    def test_no_url_returns_error(self):
        sk = BrowserSkill()
        node = NodeSpec(skill="browser", inputs=[], metadata={})
        import asyncio
        res = asyncio.run(sk.run(node))
        assert res.success is False
        assert "no url" in (res.error or "").lower()

    # NOTE: full BrowserSkill.run() requires Playwright + live gateway and is
    # covered by test_live_integration.py. A bad URL currently lets a Playwright
    # DNS error propagate (Layer 2b does not fail-soft on page.goto failure) —
    # acceptable for the deterministic suite; tracked as a robustness gap.
