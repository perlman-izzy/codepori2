#!/usr/bin/env python3
import json
import sys
import urllib.error
import urllib.request

URL = "https://opencode.ai/zen/v1/chat/completions"
payload = {
    "model": "deepseek-v4-flash-free",
    "messages": [{"role": "user", "content": "Reply with exactly CLOUD_OK and nothing else."}],
    "max_tokens": 32,
    "stream": False,
}
req = urllib.request.Request(
    URL,
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json", "User-Agent": "validated-agent-cloud-smoke/1"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read().decode("utf-8", errors="replace")
        print(f"HTTP {r.status}")
except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8", errors="replace")
    print(f"REQUEST_FAILED: HTTP {exc.code} {exc.reason}", file=sys.stderr)
    print("ERROR_BODY:", body[:4000], file=sys.stderr)
    raise
except Exception as exc:
    print(f"REQUEST_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
    raise

data = json.loads(raw)
text = data["choices"][0]["message"]["content"].strip()
print("MODEL_TEXT:", text[:500])
if "CLOUD_OK" not in text:
    raise SystemExit("free-model smoke did not return CLOUD_OK")
print("FREE_MODEL_SMOKE_PASS")
