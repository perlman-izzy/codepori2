#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CodePori (Proxy/Gemini) - Drop-in main.py

- Uses your Flask proxy at http://localhost:8000 (override with GEMINI_PROXY_BASE)
- Primary model: gemini-2.5-pro ; fallback: gemini-2.5-flash
- No Google SDK required; talks to proxy via requests
- Reads the same *.txt prompt files the CodePori repo ships with
- Pipeline: plan -> code -> finalize -> debug-loop with pytest
- Upfront verification: checks /health on the proxy and logs status
"""

import json
import os
import pathlib
import subprocess
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

import requests

# ---------------------- CONFIG ----------------------
PROJECT_DIR = pathlib.Path(__file__).resolve().parent
PROMPTS = {
    "project": PROJECT_DIR / "project_description.txt",
    "manager": PROJECT_DIR / "manager_bot.txt",
    "dev1": PROJECT_DIR / "dev_1.txt",
    "dev2": PROJECT_DIR / "dev_2.txt",
    "final1": PROJECT_DIR / "finalizer_bot_1.txt",
    "final2": PROJECT_DIR / "finalizer_bot_2.txt",
    # note: the repo spells this with a missing "i" as below
    "verify": PROJECT_DIR / "verfication_bot.txt",
}
OUT_DIR = PROJECT_DIR / "output"
CODE_DIR = OUT_DIR / "code"
LOG_FILE = OUT_DIR / "run.log"

# Proxy + Models
PROXY_BASE = os.getenv("GEMINI_PROXY_BASE", "http://localhost:8000")
PRIMARY_MODEL = "models/gemini-2.5-pro"
FALLBACK_MODEL = "models/gemini-2.5-flash"

TIMEOUT_SECONDS = 180
MAX_DEBUG_ITERS = 3

# ---------------------- UTIL ------------------------
def read_text(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def ensure_dirs() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CODE_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    ensure_dirs()
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def proxy_healthcheck() -> None:
    url = f"{PROXY_BASE}/health"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            log(
                "Proxy health: "
                f"status={data.get('status')} "
                f"valid_keys={data.get('valid_keys')} "
                f"cooldown={data.get('cooldown_keys')} "
                f"exhausted={data.get('exhausted_keys_per_model')}"
            )
        else:
            log(f"Warning: /health HTTP {r.status_code}")
    except Exception as e:
        log(f"Warning: could not reach proxy /health: {e}")


# ----------------- PROXY CLIENT ---------------------
class ProxyGemini:
    """
    Minimal client that talks to your Flask proxy, which handles key rotation,
    Tor, and rate limiting. We just send prompts; no API key needed here.
    """

    def __init__(self, base: str, primary_model: str, fallback_model: str):
        self.base = base.rstrip("/")
        self.primary_model = primary_model
        self.fallback_model = fallback_model
        self.session = requests.Session()

    def _endpoint(self, model: str, stream: bool = False) -> str:
        action = "streamGenerateContent" if stream else "generateContent"
        return f"{self.base}/v1beta/models/{model}:{action}"

    def _payload(self, prompt: str) -> Dict[str, Any]:
        return {
            "contents": [
                {"role": "user", "parts": [{"text": prompt}]}
            ]
        }

    def _extract_text(self, data: Dict[str, Any]) -> str:
        """
        Extract text from Google GL response JSON: candidates[0].content.parts[*].text
        """
        try:
            cands = data.get("candidates") or []
            if not cands:
                return ""
            parts = cands[0].get("content", {}).get("parts", [])
            texts: List[str] = []
            for p in parts:
                t = p.get("text")
                if isinstance(t, str):
                    texts.append(t)
            return "\n".join(texts).strip()
        except Exception:
            return ""

    def generate_content(self, prompt: str, stream: bool = False) -> Optional[str]:
        """
        Try primary model; on failure, fall back to fallback model.
        Returns text or None on fatal failure.
        """
        payload = self._payload(prompt)

        # Try primary
        try:
            url = self._endpoint(self.primary_model, stream=False)
            r = self.session.post(url, json=payload, timeout=TIMEOUT_SECONDS)
            if r.ok:
                text = self._extract_text(r.json())
                if text:
                    return text
                log("Empty response text from primary; trying fallback model.")
            else:
                log(f"Primary model failed: HTTP {r.status_code} - {r.text[:200]}")
        except Exception as e:
            log(f"Primary model exception: {e}")

        # Fallback
        try:
            url = self._endpoint(self.fallback_model, stream=False)
            r = self.session.post(url, json=payload, timeout=TIMEOUT_SECONDS)
            if r.ok:
                text = self._extract_text(r.json())
                if text:
                    log("Used fallback model successfully.")
                    return text
                log("Fallback returned empty text.")
            else:
                log(f"Fallback model failed: HTTP {r.status_code} - {r.text[:200]}")
        except Exception as e:
            log(f"Fallback model exception: {e}")

        return None


# ------------------ PIPELINE STEPS ------------------
def step_plan(model: ProxyGemini) -> str:
    prompt = f"""
You are the MANAGER orchestrator.

Project description:
```
{read_text(PROMPTS['project'])}
```

Manager directives:
```
{read_text(PROMPTS['manager'])}
```

Produce a concise JSON plan with keys:
- architecture: bullet list of modules/files to implement
- files: list of objects {{path, purpose}}
- tests: list of objects {{path, purpose}}
- notes: short risks/assumptions

Return ONLY JSON.
"""
    text = model.generate_content(prompt)
    if not text:
        raise RuntimeError("Manager/plan step returned no text.")
    plan = try_parse_json(text)
    if plan is None:
        raise RuntimeError("Manager/plan step did not return valid JSON.")
    return json.dumps(plan, ensure_ascii=False)


def step_generate_files(model: ProxyGemini, plan_json: str) -> None:
    plan = json.loads(plan_json)

    # Code files
    for f in plan.get("files", []):
        rel = f["path"].strip().lstrip("/\\")
        path = CODE_DIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)

        dev_prompt = f"""
You are a SENIOR DEVELOPER.

General developer guidance:
```
{read_text(PROMPTS['dev1'])}
{read_text(PROMPTS['dev2'])}
```

Write the COMPLETE file for:
- path: {f['path']}
- purpose: {f['purpose']}

Constraints:
- Return ONLY the file content.
- Include imports and main entrypoints if relevant.
- No placeholders or ellipses.
"""
        text = model.generate_content(dev_prompt)
        if not text:
            raise RuntimeError(f"Developer step produced no text for {rel}")
        path.write_text(text, encoding="utf-8")
        log(f"WROTE {path}")

    # Test files
    for t in plan.get("tests", []):
        rel = t["path"].strip().lstrip("/\\")
        path = CODE_DIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)

        test_prompt = f"""
You are a TEST ENGINEER.

Verification guidance:
```
{read_text(PROMPTS['verify'])}
```

Write a COMPLETE pytest file for:
- path: {t['path']}
- purpose: {t['purpose']}

Constraints:
- Use pytest.
- No placeholders.
- Return ONLY the file content.
"""
        text = model.generate_content(test_prompt)
        if not text:
            raise RuntimeError(f"Test generation produced no text for {rel}")
        path.write_text(text, encoding="utf-8")
        log(f"WROTE {path}")


def step_finalize(model: ProxyGemini) -> None:
    final_prompt = f"""
You are the FINALIZER.

Finalizer guidance:
```
{read_text(PROMPTS['final1'])}
{read_text(PROMPTS['final2'])}
```

Given the repository at ./output/code, produce:
1) A top-level README.md (installation, usage, test instructions).
2) A requirements.txt with exact pinned versions if possible (avoid exotica).

Return a JSON object:
{{"readme": "...markdown...", "requirements": "...lines..."}}
"""
    text = model.generate_content(final_prompt)
    if not text:
        raise RuntimeError("Finalizer step returned no text.")
    data = try_parse_json(text)
    if not isinstance(data, dict):
        raise RuntimeError("Finalizer did not return valid JSON object.")

    (CODE_DIR / "README.md").write_text(data.get("readme", ""), encoding="utf-8")
    (CODE_DIR / "requirements.txt").write_text(data.get("requirements", ""), encoding="utf-8")
    log("Finalized README.md and requirements.txt")


def run_pytest() -> int:
    req = CODE_DIR / "requirements.txt"
    if req.exists():
        log("Installing requirements (best-effort)...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(req)],
                check=False,
            )
        except Exception as e:
            log(f"pip install failed (continuing): {e}")

    log("Running pytest...")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=str(CODE_DIR))
    return result.returncode


def step_debug_loop(model: ProxyGemini, max_iters: int = MAX_DEBUG_ITERS) -> bool:
    for i in range(1, max_iters + 1):
        rc = run_pytest()
        if rc == 0:
            log("✅ Tests passed.")
            return True

        # Pull last 400 log lines for context
        try:
            tail = LOG_FILE.read_text(encoding="utf-8").splitlines()[-400:]
        except Exception:
            tail = []

        fail_prompt = f"""
You are a DEBUGGER.

The tests failed. Here is recent log tail (pytest output and events).
Propose concrete patches as JSON list:
[
  {{
    "path": "relative/file.py",
    "before": "exact previous full file content",
    "after": "new full file content",
    "explanation": "short reason"
  }}
]

Include ALL files that must change. Keep patches minimal but correct.
Return ONLY JSON.
"""
        text = model.generate_content(fail_prompt + "\n\nRECENT LOG TAIL:\n" + "\n".join(tail))
        patches = try_parse_json(text)
        if not isinstance(patches, list):
            log("Debugger returned non-JSON or not a list; stopping.")
            return False

        applied_any = False
        for p in patches:
            try:
                rel = p["path"].strip().lstrip("/\\")
                target = CODE_DIR / rel
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                before = p.get("before", "")
                after = p.get("after", "")

                current = ""
                try:
                    current = target.read_text(encoding="utf-8")
                except FileNotFoundError:
                    current = ""

                if before and normalize_ws(before) != normalize_ws(current):
                    log(
                        f"Patch warning for {rel}: 'before' does not match current; applying 'after' anyway."
                    )

                target.write_text(after, encoding="utf-8")
                log(f"Patched {rel}: {p.get('explanation', '')}")
                applied_any = True
            except Exception as e:
                log(f"Failed to apply patch entry: {e}")

        if not applied_any:
            log("No patches applied; stopping.")
            return False

    return False


# ------------------ HELPERS ------------------------
def normalize_ws(s: str) -> str:
    return "\n".join(line.rstrip() for line in s.replace("\r\n", "\n").split("\n")).strip()


def try_parse_json(text: str) -> Optional[Any]:
    """
    Robust-ish JSON extraction:
    - Try direct json.loads
    - If that fails, find the first {...} or [...] block and parse that
    """
    text = text.strip()

    # Direct parse
    try:
        return json.loads(text)
    except Exception:
        pass

    # Extract first JSON block
    start_idx = None
    end_idx = None
    stack: List[str] = []
    for i, ch in enumerate(text):
        if ch in "{[":
            if start_idx is None:
                start_idx = i
            stack.append(ch)
        elif ch in "]}":
            if not stack:
                continue
            opener = stack.pop()
            if (opener == "{" and ch == "}") or (opener == "[" and ch == "]"):
                if not stack:
                    end_idx = i + 1
                    break

    if start_idx is not None and end_idx is not None and end_idx > start_idx:
        candidate = text[start_idx:end_idx]
        try:
            return json.loads(candidate)
        except Exception:
            return None

    return None


# ---------------------- MAIN -----------------------
def main() -> int:
    ensure_dirs()
    LOG_FILE.write_text("", encoding="utf-8")

    log("Starting CodePori (Proxy/Gemini) pipeline...")
    log(f"Proxy base: {PROXY_BASE}")
    log(f"Primary model: {PRIMARY_MODEL} | Fallback: {FALLBACK_MODEL}")

    proxy_healthcheck()

    model = ProxyGemini(PROXY_BASE, PRIMARY_MODEL, FALLBACK_MODEL)

    try:
        plan_json = step_plan(model)
        log("Generated plan.")

        step_generate_files(model, plan_json)
        log("Generated code and tests.")

        step_finalize(model)
        log("Finalized repo.")

        ok = step_debug_loop(model, MAX_DEBUG_ITERS)
        if ok:
            log("DONE: output in ./output/code")
            return 0
        else:
            log("FAILED: see ./output/run.log")
            return 1

    except Exception as e:
        log(f"FATAL: {e}\n{traceback.format_exc()}")
        return 2


if __name__ == "__main__":
    sys.exit(main())