import sys, json, requests
sys.path.insert(0, ".")

url = "http://localhost:8109/v1/chat"
payload = {
    "messages": [
        {"role": "system", "content": "You are a planner. Respond with ONE valid JSON object: {\"nodes\": [{\"id\": \"n1\", \"skill\": \"researcher\", \"inputs\": [\"USER_QUERY\"]}]}"},
        {"role": "user", "content": "Research the latest news about NASA Artemis program in 2026."}
    ],
    "provider": "kilo",
    "model": "stepfun/step-3.7-flash:free",
    "max_tokens": 800,
    "temperature": 0.3,
    # NOTE: no "reasoning": "off" — mirrors what the agent skill calls do
}

r = requests.post(url, json=payload, timeout=60)
print("STATUS:", r.status_code)
data = r.json()
text = data.get("text", "")
print("REASONING FIELD:", repr(data.get("reasoning")))
print("RAW TEXT LEN:", len(text))
print("HAS THINK TAG:", "<think>" in text or "<think:6124c78e>" in text)
print("STARTS WITH BRACE:", text.strip().startswith("{"))
print("---- RAW TEXT (first 800) ----")
print(repr(text[:800]))
try:
    json.loads(text)
    print("PARSES AS JSON: YES")
except Exception as e:
    print("PARSES AS JSON: NO ->", str(e)[:120])
