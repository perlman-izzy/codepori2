#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Self-healing test runner for the generated repo in ./output/code

Goals
-----
- Run pytest against ./output/code/tests with PYTHONPATH set to include ./output/code and ./output/code/src
- If tests fail, classify the failure and build a targeted *debug prompt* for the LLM
- ALWAYS apply LLM patch suggestions (no CLI flags)
- Log everything verbosely and save artifacts to ./output/code/ (raw pytest output, raw LLM reply, applied patches)
- Never "regenerate from scratch" — only patch the files that exist
- Preflight fixes before first run: write pytest.ini (pythonpath + testpaths), remove root __init__.py that confuses imports
- Robust code extraction from fenced blocks; print when code fences are used

Usage
-----
    python /Users/williamwhite/CodePori/testonly.py

Environment
-----------
- GEMINI_PROXY_BASE (optional): defaults to http://localhost:8000
- Uses Gemini-compatible proxy endpoints (generateContent) just like main.py
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

# ---------------------- Paths ----------------------
PROJECT_DIR = pathlib.Path(__file__).resolve().parent
OUT_DIR = PROJECT_DIR / "output"
CODE_DIR = OUT_DIR / "code"
TESTS_DIR = CODE_DIR / "tests"
LOG_FILE = CODE_DIR / "TFH_run.log"

# Proxy + Models (align with main)
PROXY_BASE = os.getenv("GEMINI_PROXY_BASE", "http://localhost:8000")
PRIMARY_MODEL = "models/gemini-2.5-pro"
FALLBACK_MODEL = "models/gemini-2.5-flash"
TIMEOUT_SECONDS = 180
MAX_DEBUG_ITERS = 3

# ---------------------- Logging --------------------
def _ensure_dirs() -> None:
    CODE_DIR.mkdir(parents=True, exist_ok=True)
    TESTS_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    _ensure_dirs()
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        prev = LOG_FILE.read_text(encoding="utf-8") if LOG_FILE.exists() else ""
        LOG_FILE.write_text(prev + line + "\n", encoding="utf-8")
    except Exception:
        pass


# ------------------- Proxy Client ------------------
class ProxyGemini:
    def __init__(self, base: str, primary_model: str, fallback_model: str):
        self.base = base.rstrip("/")
        self.primary_model = primary_model
        self.fallback_model = fallback_model
        self.session = requests.Session()

    def _endpoint(self, model: str, stream: bool = False) -> str:
        action = "streamGenerateContent" if stream else "generateContent"
        return f"{self.base}/v1beta/models/{model}:{action}"

    def _payload(self, prompt: str) -> Dict[str, Any]:
        return {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}

    def _extract_text(self, data: Dict[str, Any]) -> str:
        try:
            cands = data.get("candidates") or []
            parts = cands[0].get("content", {}).get("parts", []) if cands else []
            texts: List[str] = []
            for p in parts:
                t = p.get("text")
                if isinstance(t, str):
                    texts.append(t)
            return "\n".join(texts).strip()
        except Exception:
            return ""

    def generate_content(self, prompt: str) -> Optional[str]:
        payload = self._payload(prompt)
        # primary
        try:
            r = self.session.post(self._endpoint(self.primary_model), json=payload, timeout=TIMEOUT_SECONDS)
            if r.ok:
                txt = self._extract_text(r.json())
                if txt:
                    return txt
                log("Primary returned empty text; trying fallback")
            else:
                log(f"Primary HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log(f"Primary exception: {e}")
        # fallback
        try:
            r = self.session.post(self._endpoint(self.fallback_model), json=payload, timeout=TIMEOUT_SECONDS)
            if r.ok:
                txt = self._extract_text(r.json())
                if txt:
                    log("LLM fallback used")
                    return txt
                log("Fallback returned empty text")
            else:
                log(f"Fallback HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log(f"Fallback exception: {e}")
        return None


# ---------------- Code Extractor -------------------
class CodeExtractor:
    @staticmethod
    def extract_code(response: str, prefer_lang: List[str] = ["python", "py"]) -> Optional[str]:
        """Extract fenced code if present; otherwise return whole string. Prints how it extracted."""
        if not response:
            return None
        lines = response.split("\n")
        blocks: List[Tuple[str, str]] = []
        in_block = False
        lang = ""
        buf: List[str] = []
        for line in lines:
            s = line.strip()
            if s.startswith("```"):
                if not in_block:
                    in_block = True
                    lang = s[3:].strip().lower()
                    buf = []
                else:
                    in_block = False
                    code = "\n".join(buf).strip("\n\r")
                    if code:
                        blocks.append((lang, code))
                continue
            if in_block:
                buf.append(line)
        for l, code in blocks:
            if l in prefer_lang:
                log("CodeExtractor: used fenced code block (lang match).")
                return code + "\n"
        if blocks:
            log("CodeExtractor: used first fenced block (no lang match).")
            return blocks[0][1] + "\n"
        log("CodeExtractor: no code blocks; returning whole response")
        # Try to be nice: sometimes responses are JSON with embedded code blocks — caller will pass us the 'after' string only
        return response.strip() + "\n"


# ----------------- Helpers ------------------------
def try_parse_json(text: str) -> Optional[Any]:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        # Find first balanced JSON object/array substring
        start, end, stack = None, None, []
        for i, ch in enumerate(text):
            if ch in "[{":
                if start is None:
                    start = i
                stack.append(ch)
            elif ch in "]}":
                if not stack:
                    continue
                opener = stack.pop()
                if (opener == "[" and ch == "]") or (opener == "{" and ch == "}"):
                    if not stack:
                        end = i + 1
                        break
        if start is not None and end is not None and end > start:
            candidate = text[start:end]
            try:
                return json.loads(candidate)
            except Exception:
                return None
    return None


def normalize_ws(s: str) -> str:
    return "\n".join(line.rstrip() for line in s.replace("\r\n", "\n").split("\n")).strip()


def list_tree(root: pathlib.Path, limit: int = 600) -> str:
    rows: List[str] = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rows.append(f"{p.relative_to(root)} ({p.stat().st_size} bytes)")
        else:
            rows.append(f"{p.relative_to(root)}/")
        if len(rows) >= limit:
            break
    return "\n".join(rows)


# -------------- Pytest running --------------------

def _write_pytest_ini() -> None:
    """Ensure pytest.ini directs pytest to correct root & pythonpath."""
    ini = CODE_DIR / "pytest.ini"
    body = """[pytest]
pythonpath = src .
testpaths = tests
"""
    try:
        ini.write_text(body, encoding="utf-8")
        log("Wrote pytest.ini with pythonpath=src . and testpaths=tests")
    except Exception as e:
        log(f"Failed to write pytest.ini: {e}")


def preflight_structure() -> None:
    _ensure_dirs()
    _write_pytest_ini()
    # Remove root __init__.py if present to avoid interpreting 'code' as a package (collides with stdlib 'code')
    root_init = CODE_DIR / "__init__.py"
    if root_init.exists():
        try:
            root_init.unlink()
            log("Preflight: removed root __init__.py to avoid 'code.tests' import confusion")
        except Exception as e:
            log(f"Preflight: could not remove root __init__.py: {e}")
    # Make sure src/ and tests/ are packages (helps some tooling)
    for d in [CODE_DIR / "src", CODE_DIR / "tests"]:
        d.mkdir(parents=True, exist_ok=True)
        init = d / "__init__.py"
        if not init.exists():
            try:
                init.write_text("", encoding="utf-8")
            except Exception:
                pass
    # AST sanity for tests only (catch broken triple quotes early)
    for test in TESTS_DIR.rglob("test_*.py"):
        try:
            ast.parse(test.read_text(encoding="utf-8"))
        except SyntaxError as e:
            raise RuntimeError(f"SyntaxError {test}: {e}")
    log("Preflight: structure OK.")


def run_pytest() -> Tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{CODE_DIR}:{CODE_DIR / 'src'}:" + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "-m", "pytest", "-q"]
    proc = subprocess.run(cmd, cwd=str(CODE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    return proc.returncode, proc.stdout


# -------------- Classification --------------------
@dataclass
class Classified:
    kind: str
    details: str


def classify(out: str) -> Classified:
    if not out:
        return Classified("Unknown", "<empty>")
    m = re.search(r"ModuleNotFoundError: No module named '([^']+)'", out)
    if m:
        return Classified("ModuleNotFound", m.group(1))
    sm = re.search(r"(SyntaxError|IndentationError):\s*(.*)\n", out)
    if sm:
        return Classified("SyntaxError", sm.group(0))
    if "AssertionError" in out or re.search(r"^E +AssertionError", out, re.M):
        return Classified("AssertionError", "assert failed")
    if "NameError:" in out:
        nm = re.search(r"NameError: name '([^']+)' is not defined", out)
        return Classified("NameError", nm.group(1) if nm else "NameError")
    return Classified("Unknown", out[-240:])


# -------------- Debug prompt ----------------------

def build_debug_prompt(last_out: str) -> str:
    constraints = textwrap.dedent(
        """
        Constraints:
        - Return ONLY a JSON array of patches: [{"path": "relative/file.py", "before": "...optional...", "after": "...REQUIRED...", "explanation": "short reason"}]
        - Each patch is a FULL FILE REPLACEMENT of the given path relative to ./output/code
        - Keep changes minimal. Do NOT introduce new dependencies or rewrite the project structure
        - Do NOT modify requirements.txt
        - Fix imports and test compatibility rather than adding heavy GUI/vision deps
        - Never suggest regenerating files from scratch
        """
    ).strip()
    # Pick up to 6 file contents referenced in output
    referenced: List[pathlib.Path] = []
    for m in re.finditer(r"([A-Za-z0-9_./\\-]+\.py)", last_out):
        raw = m.group(1)
        cand = (CODE_DIR / raw) if not raw.startswith("/") else pathlib.Path(raw)
        if cand.exists() and cand not in referenced:
            referenced.append(cand)
        if len(referenced) >= 6:
            break
    blobs: List[str] = []
    for p in referenced:
        try:
            rel = p.relative_to(CODE_DIR)
        except Exception:
            rel = p
        try:
            blobs.append(f"--- FILE: {rel} ---\n" + p.read_text(encoding="utf-8")[:4000])
        except Exception:
            pass
    tree = list_tree(CODE_DIR)
    prompt = (
        "You are a DEBUGGER.\n\n" +
        constraints + "\n\n" +
        "RECENT PYTEST OUTPUT (tail):\n" + last_out[-4000:] + "\n\n" +
        "REPO TREE (./output/code):\n" + tree + "\n\n" +
        "SELECTED FILE CONTENTS:\n" + "\n\n".join(blobs) + "\n\n" +
        "Return ONLY the JSON array of patches."
    )
    return prompt


# -------------- Patch application -----------------

def apply_patches(patches: Any, iter_idx: int) -> bool:
    if not isinstance(patches, list):
        log("Debugger returned non-JSON or not a list; stopping.")
        return False
    any_applied = False
    for p in patches:
        try:
            rel = p["path"].strip().lstrip("/\\")
            target = CODE_DIR / rel
            before = p.get("before", "")
            after = p.get("after", "")
            # Extract code from fenced blocks if present
            if after:
                extracted = CodeExtractor.extract_code(after, prefer_lang=["python", "py"]) or after
                if extracted != after:
                    try:
                        ast.parse(extracted)
                    except Exception as e:
                        log(f"CodeExtractor: AST parse failed (will still return): {e}")
                after = extracted
            # Compare with current content
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            if before and normalize_ws(before) != normalize_ws(current):
                log(f"Patch warning for {rel}: 'before' does not match; applying anyway.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(after, encoding="utf-8")
            log(f"Patched {rel}: {p.get('explanation', '').strip()}")
            any_applied = True
        except Exception as e:
            log(f"Failed to apply patch entry: {e}")
    # Save patches JSON for debugging
    try:
        (CODE_DIR / f"TEST_FIX_SUGGESTIONS_iter{iter_idx}.json").write_text(json.dumps(patches, indent=2), encoding="utf-8")
    except Exception:
        pass
    return any_applied


# -------------- Main loop -------------------------

def main() -> int:
    _ensure_dirs()
    log("Running pytest...")
    preflight_structure()
    rc, out_text = run_pytest()
    # Always write the raw pytest output for visibility
    (CODE_DIR / "TFH_pytest_output_iter0.txt").write_text(out_text, encoding="utf-8")

    cls = classify(out_text)
    log(f"Classifier: kind={cls.kind} details={cls.details}")

    if rc == 0:
        log("✅ Tests passed on first run.")
        return 0

    # Deterministic preflight tries once more before calling LLM
    log("Preflight: structure OK.")
    log("Re-running pytest after preflight heals...")
    rc, out_text = run_pytest()
    (CODE_DIR / "TFH_pytest_output_iter0b.txt").write_text(out_text, encoding="utf-8")
    cls = classify(out_text)
    log(f"Classifier: kind={cls.kind} details={cls.details}")
    if rc == 0:
        log("✅ Tests passed after preflight.")
        return 0

    # LLM debug loop
    model = ProxyGemini(PROXY_BASE, PRIMARY_MODEL, FALLBACK_MODEL)
    last_out = out_text
    for i in range(1, MAX_DEBUG_ITERS + 1):
        prompt = build_debug_prompt(last_out)
        reply = model.generate_content(prompt) or "[]"
        # Print first 500 chars of raw reply
        head = reply[:500].replace("\n", "\\n")
        log(f"Debugger raw reply (first 500 chars): {head}")
        # Persist raw reply
        (CODE_DIR / f"TFH_raw_debugger_iter{i}.txt").write_text(reply, encoding="utf-8")
        patches = try_parse_json(reply) or []
        if not apply_patches(patches, i):
            log("No patches applied; stopping.")
            break
        preflight_structure()
        rc, last_out = run_pytest()
        (CODE_DIR / f"TFH_pytest_output_iter{i}.txt").write_text(last_out, encoding="utf-8")
        cls = classify(last_out)
        log(f"Classifier: kind={cls.kind} details={cls.details}")
        if rc == 0:
            log("✅ Tests passed after patches.")
            return 0

    log("❌ Tests still failing after iterations. See TFH logs and TEST_FIX_SUGGESTIONS if present.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
