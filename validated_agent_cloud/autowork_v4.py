#!/usr/bin/env python3
"""V4 launcher: preserve v1-v3 and bound provider latency.

V3 established correct package identity but inherited v1's 180s timeout across a
six-model fallback list. For a five-minute autonomous cadence that makes provider
latency itself an opaque stall. V4 keeps v2's path hardening and correction logic,
but caps each free-provider attempt at 60s and response budget at 3000 tokens.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from validated_agent_cloud import autowork as base
from validated_agent_cloud import autowork_v2 as v2  # applies path/prompt hardening

MODEL_CANDIDATES = [
    "mimo-v2.5-free",
    "hy3-free",
    "nemotron-3.5-lightning-free",
    "nemotron-3-ultra-free",
]
PROVIDER_TIMEOUT_SECONDS = 60
MAX_RESPONSE_TOKENS = 3000


def bounded_call_model(prompt: str) -> tuple[str, str]:
    errors: list[str] = []
    for model in MODEL_CANDIDATES:
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a bounded coding executor. Return only one JSON object. "
                        "Do not claim tests passed unless supplied evidence says so."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "max_tokens": MAX_RESPONSE_TOKENS,
            "stream": False,
        }
        req = urllib.request.Request(
            base.MODEL_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "validated-agent-autowork/4",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=PROVIDER_TIMEOUT_SECONDS) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
            text = data["choices"][0]["message"]["content"]
            if text and text.strip():
                return model, text
            errors.append(f"{model}: empty response")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            errors.append(f"{model}: HTTP {exc.code}: {body[:500]}")
        except Exception as exc:
            errors.append(f"{model}: {type(exc).__name__}: {exc}")
    raise RuntimeError("bounded free-provider pool exhausted: " + " | ".join(errors))


base.call_model = bounded_call_model


if __name__ == "__main__":
    v2.record_v1_infrastructure_correction()
    raise SystemExit(base.main())
