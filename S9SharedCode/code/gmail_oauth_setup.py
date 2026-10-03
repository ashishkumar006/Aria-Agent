"""Moved to the gateway: llm_gatewayV9/gmail_oauth_setup.py.

Gmail credentials belong to the gateway .env (the agent never holds them).
Run:  cd llm_gatewayV9 && uv run python gmail_oauth_setup.py

This shim forwards there so old instructions keep working.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> None:
    target = Path(__file__).resolve().parents[2] / "llm_gatewayV9" / "gmail_oauth_setup.py"
    if not target.exists():
        raise SystemExit(f"gateway setup helper not found at {target}")
    sys.argv = [str(target)]
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()
