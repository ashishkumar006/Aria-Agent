from __future__ import annotations

import json
import sys
import time
import urllib.request

query = sys.argv[1] if len(sys.argv) > 1 else "What is the capital of France?"
conversation_id = sys.argv[2] if len(sys.argv) > 2 else f"monitor-{int(time.time())}"
body = json.dumps({"query": query, "conversation_id": conversation_id}).encode()
request = urllib.request.Request(
    "http://localhost:8500/api/chat",
    data=body,
    headers={"Content-Type": "application/json"},
    method="POST",
)
started = time.time()
answer = ""
print(f"TASK START: {query}", flush=True)
print(f"CONVERSATION: {conversation_id}", flush=True)
with urllib.request.urlopen(request, timeout=300) as response:
    buffer = b""
    while True:
        chunk = response.read1(65536)
        if not chunk:
            break
        buffer += chunk
        while b"\n\n" in buffer:
            frame, buffer = buffer.split(b"\n\n", 1)
            line = frame.strip()
            if not line.startswith(b"data: "):
                continue
            try:
                payload = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            kind = payload.get("type")
            if kind == "log":
                text = str(payload.get("text", "")).strip()
                if text:
                    print(f"[{time.time() - started:6.1f}s] LOG: {text}", flush=True)
            elif kind == "meta":
                print(f"[{time.time() - started:6.1f}s] META: {payload}", flush=True)
            elif kind == "error":
                print(f"[{time.time() - started:6.1f}s] ERROR: {payload}", flush=True)
            elif kind == "done":
                answer = payload.get("answer", "")
                print(f"[{time.time() - started:6.1f}s] DONE: {answer}", flush=True)
print(f"TASK END: elapsed={time.time() - started:.1f}s")
sys.exit(0 if answer else 1)
