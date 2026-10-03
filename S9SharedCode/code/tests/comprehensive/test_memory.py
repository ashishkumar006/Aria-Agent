"""
Comprehensive test suite — memory layer.

Covers:
  - Memory load/save (atomic writes)
  - Vector retrieval (FAISS)
  - Keyword fallback
  - Identity-write synchronous path
  - Turn log (F10)
  - Memory-excluded skills
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


class MemoryClient(unittest.TestCase):
    """memory.py thin client over the gateway service.

    Durable load/save moved gateway-side (see
    llm_gatewayV9/tests/test_memory.py). These checks pin the client
    contract: search payload + parsing, and fail-soft reads.
    """

    def test_read_posts_search_and_parses(self):
        from unittest import mock
        import memory
        seen = {}

        def fake_post(path, body, timeout=60.0):
            seen.update(body)
            seen["_path"] = path
            return {"items": [{
                "id": "mem:1", "kind": "fact", "keywords": ["seville"],
                "descriptor": "Seville tapas", "value": {},
                "source": "t", "run_id": "r1"}]}

        orig_post, orig_ensure = memory._post, memory.ensure_gateway
        memory._post, memory.ensure_gateway = fake_post, lambda: None
        try:
            hits = memory.read("Seville food")
        finally:
            memory._post, memory.ensure_gateway = orig_post, orig_ensure
        self.assertEqual(len(hits), 1)
        self.assertEqual(seen["_path"], "/v1/memory/search")
        self.assertEqual(seen["query"], "Seville food")

    def test_read_fail_soft(self):
        from unittest import mock
        import memory

        def boom(*a, **k):
            raise ConnectionError("gateway down")

        orig_post, orig_ensure = memory._post, memory.ensure_gateway
        memory._post, memory.ensure_gateway = boom, lambda: None
        try:
            self.assertEqual(memory.read("anything"), [])
        finally:
            memory._post, memory.ensure_gateway = orig_post, orig_ensure

    def test_tokens_extraction(self):
        from memory import _tokens
        toks = _tokens("Hello World, this is a TEST!")
        self.assertIn("hello", toks)
        self.assertIn("test", toks)
        self.assertNotIn("is", toks)


class TurnLogF10(unittest.TestCase):
    """turnlog.py per-conversation turn log."""

    def test_append_and_recent(self):
        import turnlog
        with tempfile.TemporaryDirectory() as td:
            orig = turnlog.STATE_PATH
            turnlog.STATE_PATH = Path(td) / "turns.json"
            try:
                turnlog.append("s1", "q1", "a1")
                turnlog.append("s1", "q2", "a2")
                turns = turnlog.recent("s1")
                self.assertEqual(len(turns), 2)
                self.assertEqual(turns[0]["q"], "q1")
            finally:
                turnlog.STATE_PATH = orig

    def test_session_isolation(self):
        import turnlog
        with tempfile.TemporaryDirectory() as td:
            orig = turnlog.STATE_PATH
            turnlog.STATE_PATH = Path(td) / "turns.json"
            try:
                turnlog.append("alice", "q1", "a1")
                turnlog.append("bob", "qX", "aX")
                alice = turnlog.recent("alice")
                bob = turnlog.recent("bob")
                self.assertEqual(len(alice), 1)
                self.assertEqual(len(bob), 1)
                self.assertNotIn("qX", [t["q"] for t in alice])
            finally:
                turnlog.STATE_PATH = orig

    def test_trimming(self):
        import turnlog
        with tempfile.TemporaryDirectory() as td:
            orig = turnlog.STATE_PATH
            turnlog.STATE_PATH = Path(td) / "turns.json"
            try:
                for i in range(60):
                    turnlog.append("trim", f"q{i}", "")
                turns = turnlog.recent("trim", n=100)
                self.assertLessEqual(len(turns), turnlog._MAX_TURNS_PER_SESSION)
            finally:
                turnlog.STATE_PATH = orig


class IdentityWrite(unittest.TestCase):
    """Identity-bearing queries write synchronously."""

    def test_identity_markers_detected(self):
        from flow import _is_identity_write
        self.assertTrue(_is_identity_write("my name is Alice"))
        self.assertTrue(_is_identity_write("remember that I like Rust"))
        self.assertTrue(_is_identity_write("call me Bob"))
        self.assertFalse(_is_identity_write("what is the capital of France?"))


if __name__ == "__main__":
    unittest.main()
