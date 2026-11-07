"""Launch the Aria bridge inside Blender.

Usage:
  blender.exe [file.blend] --python bootstrap.py -- \
      --port 9876 --token <token> --workspace <dir> [--allow-python] [--headless]

In GUI mode the script returns and a persistent timer pumps commands. In
background mode it blocks the main thread in the pump loop so the process
stays alive for the agent.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


def _args() -> argparse.Namespace:
    argv = sys.argv
    tail = argv[argv.index("--") + 1:] if "--" in argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--blend", default="")
    parser.add_argument("--allow-python", action="store_true")
    parser.add_argument("--headless", action="store_true")
    return parser.parse_args(tail)


def main() -> None:
    args = _args()
    import bpy

    if args.blend:
        blend = Path(args.blend)
        if not blend.exists():
            raise FileNotFoundError(blend)
        bpy.ops.wm.open_mainfile(filepath=str(blend))

    addon_path = Path(__file__).with_name("addon.py")
    spec = importlib.util.spec_from_file_location("aria_blender_bridge", addon_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    bridge = module.start(
        port=args.port, token=args.token, workspace=args.workspace,
        allow_python=args.allow_python)
    if args.headless or bpy.app.background:
        bridge.run_forever()
    print("[aria.blender] bridge ready", flush=True)


if __name__ == "__main__":
    main()
