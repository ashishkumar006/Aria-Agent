"""Verify agent_server exposes the new computer-use endpoints."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_server import app

EXPECTED = [
    "/api/computer/approvals",
    "/api/computer/approvals/{approval_id}",
    "/api/computer/runs",
    "/api/computer/runs/{run_id}",
    "/api/computer/replay/{run_id}",
]


def test_computer_routes_present():
    routes = [r.path for r in app.routes]
    for e in EXPECTED:
        assert e in routes, f"Missing route: {e}"
