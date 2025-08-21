#!/usr/bin/env python3
# coding: utf-8
"""
CodePori (Proxy/Gemini) — STRICT JSON, NO PATCH LOOP

What this rewrite changes (baked in):
- Forces the model to return **pure JSON** for every step (plan, files, tests, finalizer)
- Parses JSON robustly (direct, fenced, or embedded); never writes raw chatter to .py files
- Removes the auto-patching/debugger loop – no more LLM patch edits to your tree
- Syntax gate only detects and stops on errors (no auto-fixes)
- Keeps proxy flow (/:8000) and CommonMark parser for JSON fallback
- Disables pytest plugin autoload to keep your env quiet

If any step fails JSON validation or code extraction, the run fails fast with a clear log.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import textwrap
import time
import traceback
from typing import Any, Dict, List, Optional, Union

import requests

# ---------------------- OPTIONAL: CommonMark parser ----------------------
try:
    from markdown_it import MarkdownIt
    _MD = MarkdownIt("commonmark")
    MARKDOWN_OK = True
except Exception:
    _MD = None
    MARKDOWN_OK = False

# ---------------------- PATHS / CONFIG ----------------------
PROJECT_DIR = pathlib.Path(__file__).resolve().parent
OUT_DIR = PROJECT_DIR / "output"
CODE_DIR = OUT_DIR / "code"
LOG_FILE = OUT_DIR / "run.log"

PROMPTS = {
    "project": PROJECT_DIR / "project_description.txt",
    "manager": PROJECT_DIR / "manager_bot.txt",
    "dev1": PROJECT_DIR / "dev_1.txt",
    "dev2": PROJECT_DIR / "dev_2.txt",
    "final1": PROJECT_DIR / "finalizer_bot_1.txt",
    "final2": PROJECT_DIR / "finalizer_bot_2.txt",
    # repo typo kept for compatibility
    "verify": PROJECT_DIR / "verfication_bot.txt",
}

PROXY_BASE = os.getenv("GEMINI_PROXY_BASE", "http://localhost:8000").rstrip("/")
PRIMARY_MODEL = "models/gemini-2.5-pro"
FALLBACK_MODEL = "models/gemini-2.5-flash"
TIMEOUT_SECONDS = 180

# ---------------------- LOGGING ----------------------

def log(msg: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")

# ---------------------- FILE UTIL ----------------------

def ensure_dirs() -> None:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    CODE_DIR.mkdir(parents=True, exist_ok=True)

def read_text(p: pathlib.Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""

# ---------------------- HTTP CLIENT (Proxy) ----------------------

class ProxyGemini:
    def __init__(self, base: str, primary: str, fallback: str):
        self.base = base
        self.primary = primary
        self.fallback = fallback
        self.session = requests.Session()

    def _endpoint(self, model: str) -> str:
        return f"{self.base}/v1beta/models/{model}:generateContent"

    def _payload(self, prompt: str) -> Dict[str, Any]:
        return {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }

    def _join_parts(self, data: Dict[str, Any]) -> str:
        try:
            parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            out: List[str] = []
            for p in parts:
                t = p.get("text")
                if isinstance(t, str):
                    out.append(t)
            return "\n".join(out).strip()
        except Exception:
            return ""

    def ask(self, prompt: str) -> Optional[str]:
        for model in (self.primary, self.fallback):
            try:
                r = self.session.post(self._endpoint(model), json=self._payload(prompt), timeout=TIMEOUT_SECONDS)
                if r.ok:
                    text = self._join_parts(r.json())
                    if text:
                        if model == self.fallback:
                            log("Used fallback model successfully.")
                        return text
                    else:
                        log("Empty response text; trying fallback model." if model == self.primary else "Fallback returned empty text.")
                else:
                    log(f"Model {model} failed: HTTP {r.status_code} {(r.text or '')[:200]}")
            except Exception as e:
                log(f"Model {model} exception: {e}")
        return None

# ---------------------- JSON PARSING (robust) ----------------------

def _extract_fenced_json(s: str) -> Optional[str]:
    if not isinstance(s, str):
        return None
    if MARKDOWN_OK and _MD is not None:
        try:
            tokens = _MD.parse(s)
            for t in tokens:
                if t.type == "fence":
                    lang = (t.info or "").split()[0].lower() if t.info else ""
                    if lang in ("json", ""):
                        content = t.content.strip()
                        if content:
                            return content
        except Exception:
            pass
    # Lightweight regex fallback
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", s, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else None


def parse_json_strict(text: str, want: Union[type, tuple]) -> Any:
    """Try to parse JSON of a given top-level type. Accepts raw, fenced, or embedded.
    Raises ValueError on failure."""
    if not isinstance(text, str):
        raise ValueError("non-string response")

    candidates: List[str] = []
    candidates.append(text.strip())
    fj = _extract_fenced_json(text)
    if fj:
        candidates.append(fj)

    # try to slice first balanced {...} or [...] block
    def first_balanced(blob: str) -> Optional[str]:
        stack: List[str] = []
        start: Optional[int] = None
        for i, ch in enumerate(blob):
            if ch in "[{":
                if not stack:
                    start = i
                stack.append(ch)
            elif ch in "]}":
                if stack:
                    opener = stack.pop()
                    if ((opener == "{" and ch == "}") or (opener == "[" and ch == "]")) and not stack and start is not None:
                        return blob[start : i + 1]
        return None

    emb = first_balanced(text)
    if emb:
        candidates.append(emb)

    last_error: Optional[str] = None
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, want):
                return obj
        except Exception as e:
            last_error = str(e)
    raise ValueError(f"failed to parse {getattr(want, '__name__', want)} JSON ({last_error or 'no parse'})")

# ---------------------- CODE WRITING ----------------------

def write_python_file(path: pathlib.Path, code: str) -> None:
    """Write code and quickly syntax-check it before saving."""
    cleaned = textwrap.dedent(code).rstrip() + "\n"
    try:
        compile(cleaned, str(path), "exec")
    except SyntaxError as e:
        preview = cleaned.splitlines()[max((e.lineno or 1) - 1, 0)] if cleaned else ""
        raise SyntaxError(f"SyntaxError before write {path}: {e.msg} at {e.lineno}:{e.offset} -> {preview}") from e
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cleaned, encoding="utf-8")

# ---------------------- STEPS ----------------------

def proxy_healthcheck() -> None:
    try:
        r = requests.get(f"{PROXY_BASE}/health", timeout=10)
        if r.ok:
            data = r.json()
            log(
                "Proxy health: "
                f"status={data.get('status')} "
                f"valid_keys={data.get('valid_keys') or data.get('total_keys')} "
                f"cooldown={data.get('cooldown') or data.get('cooldown_keys')} "
                f"exhausted={data.get('exhausted') or data.get('exhausted_keys_per_model')}"
            )
        else:
            log(f"Warning: /health HTTP {r.status_code}")
    except Exception as e:
        log(f"Warning: could not reach proxy /health: {e}")


def step_plan(llm: ProxyGemini) -> Dict[str, Any]:
    desc = read_text(PROMPTS["project"]) or "(no project_description.txt)"
    mgr = read_text(PROMPTS["manager"]) or ""

    required_shape = {
        "architecture": ["..."],
        "files": [{"path": "main.py", "purpose": "..."}],
        "tests": [{"path": "tests/test_something.py", "purpose": "..."}],
        "notes": "...",
    }

    base_prompt = "\n".join([
        "You are the MANAGER orchestrator.",
        "\nProject description:",
        desc,
        "\nManager directives:",
        mgr,
        "\nReturn ONLY a JSON object with keys: architecture (list), files (list), tests (list), notes (string).",
        "Do not include markdown fences or commentary.",
        json.dumps(required_shape, indent=2),
    ])

    text = llm.ask(base_prompt)
    if not text:
        raise RuntimeError("plan: empty response")
    log("=== RAW PLAN RESPONSE ===")
    log(text[:1200] + ("\n... (truncated)" if len(text) > 1200 else ""))
    log("=== END RAW PLAN ===")

    # up to 3 attempts total
    for attempt in range(3):
        try:
            plan = parse_json_strict(text, dict)
            # light validation
            if not isinstance(plan.get("files", []), list):
                raise ValueError("files not a list")
            if not isinstance(plan.get("tests", []), list):
                raise ValueError("tests not a list")
            if any(not f.get("path") for f in plan.get("files", [])):
                raise ValueError("file missing path")
            log("=== PARSED PLAN ===")
            log(json.dumps(plan, indent=2)[:1200])
            log("=== END PARSED PLAN ===")
            return plan
        except Exception as e:
            log(f"Plan parse error: {e}; requesting strict reissue ({attempt+1}/2)")
            strict = (
                "Return ONLY valid JSON with keys architecture (list), files (list of {path,purpose}), "
                "tests (list of {path,purpose}), notes (string). No fences, no prose."
            )
            text = llm.ask(strict) or ""
    raise RuntimeError("plan: could not parse JSON after retries")


def _dev_prompt_for_file(file_item: Dict[str, Any], plan: Dict[str, Any]) -> str:
    dev1 = read_text(PROMPTS["dev1"]) or ""
    dev2 = read_text(PROMPTS["dev2"]) or ""
    plan_paths = [str(f.get("path", "")) for f in plan.get("files", [])]

    schema = {"language": "python", "code": "<FULL FILE CONTENT HERE>"}
    return "\n".join([
        "You are a SENIOR DEVELOPER. Produce the COMPLETE file as JSON only.",
        dev1,
        dev2,
        f"Target path: {file_item['path']}",
        f"Purpose: {file_item.get('purpose','')}\n",
        "Rules:",
        "- Return ONLY a JSON object with keys {\"language\", \"code\"}.",
        "- No markdown, no fences, no prose.",
        "- The file must be self-contained and production-ready.",
        "- Respect the repository layout; import paths must match target path.",
        f"- Other files present: {plan_paths}",
        json.dumps(schema, indent=2),
    ])


def _test_prompt_for_file(test_item: Dict[str, Any], plan: Dict[str, Any]) -> str:
    verify = read_text(PROMPTS["verify"]) or ""
    schema = {"language": "python", "code": "<FULL PYTEST FILE CONTENT HERE>"}
    return "\n".join([
        "You are a TEST ENGINEER. Produce a COMPLETE pytest file as JSON only.",
        verify,
        f"Target path: {test_item['path']}",
        f"Purpose: {test_item.get('purpose','') }\n",
        "Rules:",
        "- Return ONLY a JSON object with keys {\"language\", \"code\"}.",
        "- No markdown, no fences, no prose.",
        "- Import modules using the exact paths listed in the plan's files.",
        "- Use fixtures correctly; no top-level 'yield' statements.",
        json.dumps(schema, indent=2),
    ])


def _normalize_package_layout(root: pathlib.Path) -> None:
    """Ensure every python-containing directory is a package by adding __init__.py."""
    for p in root.rglob("*.py"):
        pkg_dir = p.parent
        init = pkg_dir / "__init__.py"
        if not init.exists():
            init.write_text("\n", encoding="utf-8")


def step_generate(plan: Dict[str, Any], llm: ProxyGemini) -> None:
    files = plan.get("files", [])
    tests = plan.get("tests", [])

    log(f"Generating {len(files)} code files and {len(tests)} test files")

    # ---- code files ----
    for i, fitem in enumerate(files, 1):
        rel = fitem["path"].strip().lstrip("/\\")
        target = CODE_DIR / rel
        log(f"Generating code file {i}/{len(files)}: {rel}")
        prompt = _dev_prompt_for_file(fitem, plan)
        txt = llm.ask(prompt)
        if not txt:
            raise RuntimeError(f"no response for {rel}")
        try:
            payload = parse_json_strict(txt, dict)
            code = str(payload.get("code", ""))
            if not code.strip():
                raise ValueError("empty code")
            write_python_file(target, code)
            log(f"WROTE {target}")
        except Exception as e:
            raise RuntimeError(f"file {rel}: {e}")

    # ensure packages for imports like 'src.*'
    _normalize_package_layout(CODE_DIR)

    # ---- test files ----
    for i, titem in enumerate(tests, 1):
        rel = titem["path"].strip().lstrip("/\\")
        target = CODE_DIR / rel
        log(f"Generating test file {i}/{len(tests)}: {rel}")
        prompt = _test_prompt_for_file(titem, plan)
        txt = llm.ask(prompt)
        if not txt:
            raise RuntimeError(f"no response for {rel}")
        try:
            payload = parse_json_strict(txt, dict)
            code = str(payload.get("code", ""))
            if not code.strip():
                raise ValueError("empty code")
            write_python_file(target, code)
            log(f"WROTE {target}")
        except Exception as e:
            raise RuntimeError(f"test {rel}: {e}")

# ---------------------- SYNTAX GATE (detect-only) ----------------------

def _collect_python_files(root: pathlib.Path) -> List[pathlib.Path]:
    return [p for p in root.rglob("*.py") if p.is_file()]


def syntax_gate(root: pathlib.Path) -> None:
    errors: List[str] = []
    for py in _collect_python_files(root):
        try:
            src = py.read_text(encoding="utf-8")
            compile(src, str(py), "exec")
        except SyntaxError as e:
            bad_line = (e.text or "").rstrip("\n")
            rel = py.relative_to(root)
            errors.append(f"{rel}: SyntaxError {e.msg} at {e.lineno}:{e.offset} -> {bad_line}")
        except Exception as e:
            rel = py.relative_to(root)
            errors.append(f"{rel}: read/compile error: {e}")
    if errors:
        log("Syntax gate failed:")
        for e in errors:
            log("  - " + e)
        raise SystemExit(2)
    log("Syntax gate: clean.")

# ---------------------- FINALIZER ----------------------

def step_finalize(llm: ProxyGemini) -> None:
    final1 = read_text(PROMPTS["final1"]) or ""
    final2 = read_text(PROMPTS["final2"]) or ""
    schema = {"readme": "# Project...", "requirements": "pytest\nrequests\nmarkdown-it-py\n"}
    prompt = "\n".join([
        "You are the FINALIZER.",
        final1,
        final2,
        "Return ONLY a JSON object with keys {\"readme\", \"requirements\"}. No fences, no prose.",
        json.dumps(schema, indent=2),
    ])
    txt = llm.ask(prompt) or ""
    try:
        obj = parse_json_strict(txt, dict)
    except Exception:
        obj = {}
    readme = str(obj.get("readme", "# Project\n\n(README was not returned as JSON.)\n"))
    reqs = str(obj.get("requirements", "pytest\nrequests\nmarkdown-it-py\n"))
    (CODE_DIR / "README.md").write_text(readme, encoding="utf-8")
    if not reqs.endswith("\n"):
        reqs = reqs + "\n"
    (CODE_DIR / "requirements.txt").write_text(reqs, encoding="utf-8")
    log("Finalized README.md and requirements.txt.")

# ---------------------- TEST RUN (no patch/debug loop) ----------------------

def run_pytest_once() -> int:
    req = CODE_DIR / "requirements.txt"
    if req.exists():
        log("Installing requirements (best-effort)...")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)], check=False)
        except Exception as e:
            log(f"pip install failed (continuing): {e}")
    log("Running pytest...")
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    res = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=str(CODE_DIR), env=env)
    return res.returncode

# ---------------------- MAIN ----------------------

def main() -> int:
    try:
        ensure_dirs()
        LOG_FILE.write_text("", encoding="utf-8")
        log("Starting CodePori (Proxy/Gemini) pipeline...")
        log(f"Proxy base: {PROXY_BASE}")
        log(f"Primary model: {PRIMARY_MODEL} | Fallback: {FALLBACK_MODEL}")
        proxy_healthcheck()

        llm = ProxyGemini(PROXY_BASE, PRIMARY_MODEL, FALLBACK_MODEL)

        plan = step_plan(llm)
        log("Generated plan.")

        step_generate(plan, llm)
        log("Generated files.")

        syntax_gate(CODE_DIR)

        step_finalize(llm)
        log("Finalized repo.")

        rc = run_pytest_once()
        if rc == 0:
            log("✅ Tests passed.")
            log("DONE: output in ./output/code")
            return 0
        else:
            log("❌ Tests failed. See pytest output above and ./output/run.log")
            return 1

    except SystemExit as e:
        return int(e.code or 0)
    except Exception as e:
        log(f"FATAL: {e}\n{traceback.format_exc()}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
