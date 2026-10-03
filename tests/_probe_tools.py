"""Probe: does every gateway provider honor the tools channel for agent=action?"""
import httpx

tools = [{
    "name": "schedule_task",
    "description": "Schedule a task",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}, "when": {"type": "string"}},
        "required": ["query", "when"],
    },
}]

for i in range(4):
    r = httpx.post(
        "http://localhost:8109/v1/chat",
        json={
            "messages": [{"role": "user", "content":
                          "Schedule a reminder to drink water in 1 hour using the schedule_task tool."}],
            "agent": "action",
            "tools": tools,
            "max_tokens": 300,
        },
        timeout=90,
    )
    d = r.json()
    tcs = d.get("tool_calls") or []
    names = [t.get("name") for t in tcs]
    text = (d.get("text") or "")[:80]
    print(f"try{i}: provider={d.get('provider')} tool_calls={names} text={text!r}")
