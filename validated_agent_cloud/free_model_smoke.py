#!/usr/bin/env python3
import json
import sys
import urllib.error
import urllib.request

URL = "https://opencode.ai/zen/v1/chat/completions"
CANDIDATES = [
    "mimo-v2.5-free",
    "hy3-free",
    "nemotron-3.5-lightning-free",
    "nemotron-3-ultra-free",
    "big-pickle",
    "north-mini-code-free",
]


def try_model(model):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly CLOUD_OK and nothing else."}],
        "max_tokens": 32,
        "stream": False,
    }
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "validated-agent-cloud-smoke/2"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            text = data["choices"][0]["message"]["content"].strip()
            print(f"MODEL={model} HTTP={r.status} TEXT={text[:300]!r}")
            return "CLOUD_OK" in text
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"MODEL={model} HTTP_ERROR={exc.code} BODY={body[:700]!r}")
        return False
    except Exception as exc:
        print(f"MODEL={model} ERROR={type(exc).__name__}: {exc}")
        return False


for candidate in CANDIDATES:
    if try_model(candidate):
        print(f"FREE_MODEL_SMOKE_PASS model={candidate}")
        raise SystemExit(0)

print("FREE_MODEL_SMOKE_FAIL: no candidate returned CLOUD_OK", file=sys.stderr)
raise SystemExit(1)
