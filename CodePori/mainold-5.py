#!/usr/bin/env python3
# coding: utf-8
"""
CodePori (Proxy/Gemini) — STRICT JSON + FULL RAW LLM LOGGING + FORENSICS + LINTER + TARGETED REPROMPTS

What this build does:
- Logs **full proxy JSON** bodies for every LLM call (plan/files/tests/finalizer) to stdout and to /output/llm_raw/.
- Logs the **extracted response text** we actually parse/use.
- Enforces strict JSON contracts:
  * plan: dict with keys architecture/files/tests/notes
  * files/tests: dict with {"language":"python","code":"..."} and **non-empty code**
- Retries/reprompts on empty/invalid JSON (configurable).
- Compiles each file **before writing**; on SyntaxError prints 5-line context and dumps the exact payload.
- Runs a universal import/symbol linter and does targeted reprompts/auto-fixes (no network in generated files).
- Saves a repo snapshot and a diff report of added/removed/modified files.

Env knobs:
  CP_ENABLE_LINTER=1
  CP_LINT_REPROMPTS=2
  CP_JSON_RETRIES=2
  CP_AUTOFIX_RELATIVE=1
  GEMINI_PROXY_BASE (default http://localhost:8000)
"""

from __future__ import annotations

import ast
import importlib
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
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

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
FORENSICS_DIR = OUT_DIR / "llm_raw"
PREV_SNAPSHOT = PROJECT_DIR / ".codepori_prev"

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
TIMEOUT_SECONDS = int(os.getenv("CP_HTTP_TIMEOUT", "180"))

ENABLE_LINTER = os.getenv("CP_ENABLE_LINTER", "1") != "0"
LINT_REPROMPTS = max(0, int(os.getenv("CP_LINT_REPROMPTS", "2")))
JSON_RETRIES = max(0, int(os.getenv("CP_JSON_RETRIES", "2")))
AUTOFIX_RELATIVE = os.getenv("CP_AUTOFIX_RELATIVE", "1") != "0"

# ---------------------- LOGGING ----------------------

def log(msg: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------- SNAPSHOT/DIFF ----------------------

def snapshot_previous() -> None:
    try:
        if PREV_SNAPSHOT.exists():
            shutil.rmtree(PREV_SNAPSHOT)
        if CODE_DIR.exists():
            shutil.copytree(CODE_DIR, PREV_SNAPSHOT)
        log(f"Snapshot saved: {PREV_SNAPSHOT}")
    except Exception as e:
        log(f"Snapshot warning: {e}")


def _list_rel(root: pathlib.Path) -> Set[str]:
    out: Set[str] = set()
    if not root.exists():
        return out
    for p in root.rglob("*"):
        if p.is_file():
            out.add(str(p.relative_to(root)))
    return out


def write_diff_report() -> None:
    try:
        report = OUT_DIR / "diff-report.txt"
        old = PREV_SNAPSHOT
        new = CODE_DIR
        added: List[str] = []
        removed: List[str] = []
        modified: List[str] = []

        old_set = _list_rel(old)
        new_set = _list_rel(new)
        for rel in sorted(new_set - old_set):
            added.append(rel)
        for rel in sorted(old_set - new_set):
            removed.append(rel)
        for rel in sorted(new_set & old_set):
            try:
                if (old / rel).read_bytes() != (new / rel).read_bytes():
                    modified.append(rel)
            except Exception:
                modified.append(rel)
        lines = [
            "# CodePori Diff Report",
            "",
            "## Added:",
            *((f"+ {a}" for a in added) if added else ["(none)"]),
            "",
            "## Removed:",
            *((f"- {r}" for r in removed) if removed else ["(none)"]),
            "",
            "## Modified:",
            *((f"* {m}" for m in modified) if modified else ["(none)"]),
            "",
        ]
        report.write_text("\n".join(lines), encoding="utf-8")
        log("Diff report written to ./output/diff-report.txt")
    except Exception as e:
        log(f"Diff report warning: {e}")


# ---------------------- FILE UTIL ----------------------

def ensure_dirs() -> None:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    CODE_DIR.mkdir(parents=True, exist_ok=True)
    FORENSICS_DIR.mkdir(parents=True, exist_ok=True)


def read_text(p: pathlib.Path, default: str = "") -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return default


def safe_relpath(p: pathlib.Path) -> str:
    try:
        return str(p.relative_to(CODE_DIR))
    except Exception:
        return str(p)


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

    def _dump_forensics(self, tag: str, raw_json: Dict[str, Any], text: str) -> None:
        try:
            tag_s = re.sub(r"[^A-Za-z0-9_.-]", "_", tag)[:128] or "unnamed"
            (FORENSICS_DIR / f"{tag_s}.json").write_text(json.dumps(raw_json, indent=2, ensure_ascii=False), encoding="utf-8")
            (FORENSICS_DIR / f"{tag_s}.txt").write_text(text, encoding="utf-8")
        except Exception:
            pass

    def ask(self, prompt: str, *, tag: str) -> Optional[str]:
        """Call primary then fallback; print FULL JSON + extracted text; save forensics."""
        for model in (self.primary, self.fallback):
            try:
                r = self.session.post(self._endpoint(model), json=self._payload(prompt), timeout=TIMEOUT_SECONDS)
                body_bytes = len(r.content or b"")
                ctype = r.headers.get("content-type", "")
                if r.ok:
                    try:
                        j = r.json()
                    except Exception:
                        j = {"_raw": (r.text or "")}
                    # Print proxy-level JSON (verbatim)
                    cand_count = len(j.get("candidates", []) or [])
                    log(f"[PROXY] status={r.status_code} bytes={body_bytes} ctype={ctype} candidates={cand_count}")
                    print("[PROXY] FULL JSON BEGIN ==================================", flush=True)
                    try:
                        print(json.dumps(j, indent=2, ensure_ascii=False), flush=True)
                    except Exception:
                        print(r.text or "", flush=True)
                    print("[PROXY] FULL JSON END ====================================", flush=True)

                    text = self._join_parts(j)
                    # Print extracted text
                    log(f"[LLM] RESPONSE TEXT len={len(text)}")
                    print("[LLM] RESPONSE TEXT BEGIN ===============================", flush=True)
                    print(text if text else "<EMPTY>", flush=True)
                    print("[LLM] RESPONSE TEXT END =================================", flush=True)

                    # Save forensics
                    self._dump_forensics(f"{tag}_{'fallback' if model==self.fallback else 'primary'}", j, text)

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


# ---------------------- JSON PARSING (robust, multi-candidate) ----------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)

@dataclass
class JsonCandidate:
    raw: str
    obj: dict
    code: str
    score: int
    reason: str


def _balanced_json_slices(blob: str) -> List[str]:
    """Return all top-level balanced JSON objects/arrays found in the blob."""
    out: List[str] = []
    stack: List[str] = []
    start: Optional[int] = None
    for i, ch in enumerate(blob or ""):
        if ch in "{[":
            if not stack:
                start = i
            stack.append(ch)
        elif ch in "}]":
            if stack:
                opener = stack.pop()
                if (opener, ch) not in (("{", "}"), ("[", "]")):
                    stack.clear()
                    start = None
                    continue
                if not stack and start is not None:
                    out.append(blob[start : i + 1])
                    start = None
    return out


def _all_json_blobs(text: str) -> List[str]:
    blobs: List[str] = []
    # 1) fenced
    for m in _JSON_FENCE_RE.finditer(text or ""):
        block = m.group(1).strip()
        if block:
            blobs.append(block)
    # 2) balanced
    blobs.extend(_balanced_json_slices(text or ""))
    # 3) whole text
    t = (text or "").strip()
    if t.startswith("{") or t.startswith("["):
        blobs.append(t)
    # dedupe
    seen = set()
    uniq: List[str] = []
    for b in blobs:
        k = b.strip()
        if k and k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


def _score_code_sanity(code: str) -> int:
    if not code or not code.strip():
        return 0
    s = code.strip()
    score = 0
    score += min(len(s), 100000) // 50
    if "\n" in s: score += 50
    if "import " in s: score += 50
    if "def " in s: score += 50
    if "class " in s: score += 30
    if "<FULL" in s.upper(): score -= 200
    return score


def parse_json_best(
    text: str,
    *,
    want: type = dict,
    require_code: bool = False,
    dump_dir: Optional[pathlib.Path] = None,
    tag: str = "generic",
) -> dict:
    if not isinstance(text, str):
        raise ValueError("non-string response")

    blobs = _all_json_blobs(text)
    if dump_dir:
        dump_dir.mkdir(parents=True, exist_ok=True)
        (dump_dir / f"{tag}.raw.txt").write_text(text, encoding="utf-8")
        (dump_dir / f"{tag}.blobs.count").write_text(str(len(blobs)), encoding="utf-8")

    candidates: List[JsonCandidate] = []
    last_err = None
    for idx, blob in enumerate(blobs):
        try:
            obj = json.loads(blob)
            if not isinstance(obj, want):
                continue
            code = str(obj.get("code", "")) if isinstance(obj, dict) else ""
            score = _score_code_sanity(code)
            reason = "ok"
            if require_code and not code.strip():
                reason = "empty_code"
            candidates.append(JsonCandidate(raw=blob, obj=obj, code=code, score=score, reason=reason))
        except Exception as e:
            last_err = str(e)

    candidates.sort(key=lambda c: (c.score, len(c.code)), reverse=True)

    if dump_dir:
        lines = []
        for i, c in enumerate(candidates):
            preview = c.code[:80].replace(os.linesep, " ") if isinstance(c.code, str) else ""
            lines.append(f"{i:02d} score={c.score} reason={c.reason} code_len={len(c.code)} preview={preview}")
        (dump_dir / f"{tag}.candidates.txt").write_text("\n".join(lines), encoding="utf-8")

    if not candidates:
        raise ValueError(f"failed to parse {want.__name__} JSON ({last_err or 'no candidates'})")

    best = candidates[0]
    if require_code and not best.code.strip():
        raise ValueError("parsed JSON but best candidate has empty code (all candidates empty)")
    return best.obj


# ---------------------- CODE WRITING ----------------------

def _context_lines(src: str, lineno: int, radius: int = 5) -> str:
    lines = src.splitlines()
    start = max(1, lineno - radius)
    end = min(len(lines), lineno + radius)
    buf = []
    for i in range(start, end + 1):
        mark = ">>" if i == lineno else "  "
        buf.append(f"{mark} {i:5d}: {lines[i-1]}")
    return "\n".join(buf)

def write_python_file(path: pathlib.Path, code: str, *, tag: str = "") -> None:
    cleaned = textwrap.dedent(code).rstrip() + "\n"
    try:
        compile(cleaned, str(path), "exec")
    except SyntaxError as e:
        preview_ctx = _context_lines(cleaned, e.lineno or 1)
        log(f"SyntaxError BEFORE WRITE {safe_relpath(path)}: {e.msg} at {e.lineno}:{e.offset}")
        print("=== Offending source context ===", flush=True)
        print(preview_ctx, flush=True)
        # dump payload forensics
        try:
            tag_s = re.sub(r"[^A-Za-z0-9_.-]", "_", tag)[:128] or "file"
            (FORENSICS_DIR / f"{tag_s}.py").write_text(cleaned, encoding="utf-8")
        except Exception:
            pass
        raise
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cleaned, encoding="utf-8")


# ---------------------- PLAN / PROMPTS ----------------------

def step_plan(llm: ProxyGemini) -> Dict[str, Any]:
    desc = read_text(PROMPTS["project"]) or "(no project_description.txt)"
    mgr = read_text(PROMPTS["manager"]) or ""
    # Pin framework to Tkinter to avoid framework drift in generated modules/tests
    mgr_addendum = (
        "\nHARD REQUIREMENT: Use Tkinter (not PyQt/Qt) for any UI. "
        "Do not switch frameworks or introduce GUI libs other than Tkinter. "
        "Tests must target Tkinter-based modules."
    )

    required_shape = {
        "architecture": ["..."],
        "files": [{"path": "src/module.py", "purpose": "..."}],
        "tests": [{"path": "tests/test_something.py", "purpose": "..."}],
        "notes": "...",
    }

    base_prompt = "\n".join(
        [
            "You are the MANAGER orchestrator.",
            "\nProject description:",
            desc,
            "\nManager directives:",
            mgr + mgr_addendum,
            "\nReturn ONLY a JSON object with keys: architecture (list), files (list), tests (list), notes (string).",
            "Do not include markdown fences or commentary.",
            json.dumps(required_shape, indent=2),
        ]
    )

    text = llm.ask(base_prompt, tag="plan")
    if not text:
        raise RuntimeError("plan: empty response")

    log("=== RAW PLAN RESPONSE ===")
    log(text[:1200] + ("\n... (truncated)" if len(text) > 1200 else ""))
    log("=== END RAW PLAN ===")

    for attempt in range(3):
        try:
            plan = parse_json_best(text, want=dict, require_code=False, dump_dir=FORENSICS_DIR / "plan", tag="plan")
            if not isinstance(plan.get("files", []), list):
                raise ValueError("files not a list")
            if not isinstance(plan.get("tests", []), list):
                raise ValueError("tests not a list")
            log("=== PARSED PLAN ===")
            log(json.dumps(plan, indent=2)[:1200])
            log("=== END PARSED PLAN ===")
            return plan
        except Exception as e:
            log(f"Plan parse error: {e}; requesting strict reissue ({attempt+1}/2)")
            strict = (
                "Return ONLY valid JSON with keys architecture, files, tests, notes. No fences, no prose. "
                "Remember: Tkinter only; do not switch frameworks."
            )
            text = llm.ask(strict, tag=f"plan_retry_{attempt+1}")
            if not text:
                break
    raise RuntimeError("plan: could not parse JSON after retries")


def _dev_prompt_for_file(file_item: Dict[str, Any], plan_paths: Sequence[str]) -> str:
    dev1 = read_text(PROMPTS["dev1"]) or ""
    dev2 = read_text(PROMPTS["dev2"]) or ""
    schema = {"language": "python", "code": "<FULL FILE CONTENT HERE>"}
    return "\n".join(
        [
            "You are a SENIOR DEVELOPER. Produce the COMPLETE file as JSON only.",
            dev1,
            dev2,
            "HARD REQUIREMENTS:",
            "- The UI framework must be Tkinter. Do NOT use PyQt/Qt/others.",
            "- Import paths MUST be absolute from the repo root package (e.g., 'src.module'); never relative imports.",
            f"Target path: {file_item['path']}",
            f"Purpose: {file_item.get('purpose','')}\n",
            "Rules:",
            "- Return ONLY a JSON object with keys {\"language\", \"code\"}.",
            "- The 'code' field MUST contain the complete, production-ready source code for the file.",
            "- The 'code' field MUST NOT be empty or contain only whitespace.",
            "- No markdown, no fences, no prose outside the 'code' field.",
            f"- Other planned files: {plan_paths}",
            json.dumps(schema, indent=2),
        ]
    )


def _test_prompt_for_file(test_item: Dict[str, Any], plan_paths: Sequence[str]) -> str:
    verify = read_text(PROMPTS["verify"]) or ""
    schema = {"language": "python", "code": "<FULL PYTEST FILE CONTENT HERE>"}
    return "\n".join(
        [
            "You are a TEST ENGINEER. Produce a COMPLETE pytest file as JSON only.",
            verify,
            "HARD REQUIREMENTS:",
            "- Tests must target Tkinter-based modules (no PyQt).",
            "- Import modules using absolute paths from the repo root (e.g., 'src.module').",
            f"Target path: {test_item['path']}",
            f"Purpose: {test_item.get('purpose','')}\n",
            "Rules:",
            "- Return ONLY a JSON object with keys {\"language\", \"code\"}.",
            "- The 'code' field MUST contain the complete, production-ready source code for the test file.",
            "- The 'code' field MUST NOT be empty or contain only whitespace.",
            "- No markdown, no fences, no prose outside the 'code' field.",
            json.dumps(schema, indent=2),
        ]
    )


# ---------------------- GENERATION ----------------------

def _json_retry(llm: ProxyGemini, prompt: str, *, is_code_payload: bool = False, dump_dir: Optional[pathlib.Path] = None, tag: str = "item") -> Dict[str, Any]:
    last_err: Optional[str] = None
    retries = JSON_RETRIES + (2 if is_code_payload else 0)

    for i in range(retries + 1):
        txt = llm.ask(prompt, tag=f"{tag}_try{i+1}")
        if not txt:
            last_err = "empty response from LLM"
            log(f"  -> LLM returned empty response; re-prompt ({i+1}/{retries})")
            continue
        try:
            payload = parse_json_best(txt, want=dict, require_code=is_code_payload, dump_dir=dump_dir, tag=f"{tag}_try{i+1}")
            # Contract enforcement for code/test payloads
            lang = str(payload.get("language", "")).strip().lower()
            if is_code_payload and lang not in ("python", "py"):
                last_err = f"language must be 'python', got '{payload.get('language')}'"
                log(f"  -> Invalid language; re-prompt ({i+1}/{retries})")
                continue
            code = str(payload.get("code", ""))
            if is_code_payload and not code.strip():
                last_err = "code field is empty or whitespace"
                log(f"  -> Code payload empty; re-prompt ({i+1}/{retries})")
                continue
            return payload
        except Exception as e:
            last_err = str(e)
            log(f"  -> JSON:MISS or invalid ({last_err}); re-prompt ({i+1}/{retries})")
    raise ValueError(last_err or "no parse")


def step_generate(plan: Dict[str, Any], llm: ProxyGemini) -> None:
    files = plan.get("files", [])
    tests = plan.get("tests", [])

    log(f"Generating {len(files)} code files and {len(tests)} test files")
    plan_paths = [f.get("path", "") for f in files]

    # code files
    for i, fitem in enumerate(files, 1):
        rel = fitem["path"].strip().lstrip("/\\")
        target = CODE_DIR / rel
        log(f"Generating code file {i}/{len(files)}: {rel}")
        payload = _json_retry(
            llm,
            _dev_prompt_for_file(fitem, plan_paths),
            is_code_payload=True,
            dump_dir=FORENSICS_DIR / "files",
            tag=rel.replace("/", "_"),
        )
        code = str(payload.get("code", ""))
        log(f"[GEN] {rel}: language={payload.get('language')} code_len={len(code)}")
        write_python_file(target, code, tag=f"file_{rel.replace('/', '_')}")
        log(f"WROTE {target}")

    # test files
    for i, titem in enumerate(tests, 1):
        rel = titem["path"].strip().lstrip("/\\")
        target = CODE_DIR / rel
        log(f"Generating test file {i}/{len(tests)}: {rel}")
        payload = _json_retry(
            llm,
            _test_prompt_for_file(titem, plan_paths),
            is_code_payload=True,
            dump_dir=FORENSICS_DIR / "tests",
            tag=rel.replace("/", "_"),
        )
        code = str(payload.get("code", ""))
        log(f"[GEN-TEST] {rel}: language={payload.get('language')} code_len={len(code)}")
        write_python_file(target, code, tag=f"test_{rel.replace('/', '_')}")
        log(f"WROTE {target}")


# ---------------------- SYNTAX GATE ----------------------

def _collect_python_files(root: pathlib.Path) -> List[pathlib.Path]:
    return [p for p in root.rglob("*.py") if p.is_file()]


def syntax_gate(root: pathlib.Path) -> None:
    errors: List[str] = []
    for py in _collect_python_files(root):
        try:
            src = py.read_text(encoding="utf-8")
            compile(src, str(py), "exec")
        except SyntaxError as e:
            bad = (e.text or "").rstrip("\n")
            ctx = _context_lines(src, e.lineno or 1)
            errors.append(f"{py.relative_to(root)}: SyntaxError {e.msg} at {e.lineno}:{e.offset} -> {bad}\n{ctx}")
        except Exception as e:
            errors.append(f"{py.relative_to(root)}: read/compile error: {e}")
    if errors:
        log("Syntax gate failed:")
        for e in errors:
            print("  - " + e, flush=True)
            log("  - " + e.splitlines()[0])
        raise SystemExit(2)
    log("Syntax gate: clean.")


# ---------------------- PACKAGE NORMALIZATION ----------------------

def normalize_packages(root_pkg: str = "src") -> None:
    root = CODE_DIR / root_pkg
    if not root.exists():
        return
    for py in root.rglob("*.py"):
        d = py.parent
        while True:
            init = d / "__init__.py"
            if not init.exists():
                init.write_text("# auto-added by CodePori\n", encoding="utf-8")
            if d == root:
                break
            d = d.parent
    log(f"Normalized packages under {root_pkg}/ (added __init__.py where missing).")


# ---------------------- UNIVERSAL IMPORT/API LINTER (v2) ----------------------
ImportSpec = Tuple[str, Tuple[str, ...]]  # (module, (symbols,))


def _module_to_relpath(mod: str) -> pathlib.Path:
    parts = mod.split(".")
    return pathlib.Path(*parts).with_suffix(".py")


def _discover_test_imports(test_path: pathlib.Path) -> List[ImportSpec]:
    text = read_text(test_path, "")
    if not text:
        return []
    try:
        tree = ast.parse(text)
    except Exception:
        return []
    specs: List[ImportSpec] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                names = tuple(n.name for n in node.names if isinstance(n, ast.alias))
                specs.append((node.module, names))
        elif isinstance(node, ast.Import):
            for n in node.names:
                specs.append((n.name, ()))
    return specs


def _gather_all_test_imports(code_root: pathlib.Path) -> List[Tuple[pathlib.Path, ImportSpec]]:
    out: List[Tuple[pathlib.Path, ImportSpec]] = []
    tests_dir = code_root / "tests"
    if not tests_dir.exists():
        return out
    for test in tests_dir.rglob("test_*.py"):
        for spec in _discover_test_imports(test):
            out.append((test, spec))
    return out


def _try_import(mod: str) -> Tuple[bool, Optional[str], Optional[str]]:
    try:
        sys.path.insert(0, str(CODE_DIR))
        importlib.invalidate_caches()
        importlib.import_module(mod)
        return True, None, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", traceback.format_exc()
    finally:
        if sys.path and sys.path[0] == str(CODE_DIR):
            sys.path.pop(0)


def _module_symbols(mod: str) -> Set[str]:
    try:
        sys.path.insert(0, str(CODE_DIR))
        importlib.invalidate_caches()
        m = importlib.import_module(mod)
        return set(dir(m))
    except Exception:
        return set()
    finally:
        if sys.path and sys.path[0] == str(CODE_DIR):
            sys.path.pop(0)


def _autofix_relative_imports(mod: str, root_pkg: str = "src") -> bool:
    if not AUTOFIX_RELATIVE:
        return False
    target = CODE_DIR / _module_to_relpath(mod)
    if not target.exists():
        return False
    code = read_text(target, "")
    if not code:
        return False
    rel_parts = _module_to_relpath(mod).with_suffix("").parts
    changed = False

    def replacer(match: re.Match[str]) -> str:
        nonlocal changed
        dots = match.group(1)
        rest = match.group(2)
        import_tail = match.group(3)
        level = len(dots)
        up = level - 1
        base = list(rel_parts)
        while up > 0 and len(base) > 1:
            base.pop()
            up -= 1
        if len(base) == 1:
            base = [root_pkg]
        abs_mod = ".".join(base + ([p for p in rest.split(".") if p] if rest else []))
        changed = True
        return f"from {abs_mod} import {import_tail}"

    new_code = re.sub(r"^\s*from\s+(\.+)([A-Za-z0-9_\.]*?)\s+import\s+(.+)$", replacer, code, flags=re.MULTILINE)
    if changed and new_code != code:
        write_python_file(target, new_code, tag=f"autofix_{mod.replace('.', '_')}")
        log(f"Auto-fixed relative imports in {safe_relpath(target)} -> absolute")
        return True
    return False


def lint_imports_and_symbols(code_root: pathlib.Path) -> Tuple[Set[str], Dict[str, Set[str]], Dict[str, str]]:
    missing_modules: Set[str] = set()
    missing_symbols: Dict[str, Set[str]] = {}
    broken_modules: Dict[str, str] = {}

    pairs = _gather_all_test_imports(code_root)
    for _test_file, (mod, names) in pairs:
        ok, err, _tb = _try_import(mod)
        if not ok:
            target = code_root / _module_to_relpath(mod)
            if target.exists():
                broken_modules[mod] = err or "ImportError"
                continue
            missing_modules.add(mod)
            continue
        if names:
            present = _module_symbols(mod)
            needed = set(names) - present
            if needed:
                missing_symbols.setdefault(mod, set()).update(needed)
    return missing_modules, missing_symbols, broken_modules


# ---------------------- TARGETED REPROMPT ENGINE ----------------------

def _prompt_make_module(mod: str, sym_list: Sequence[str], plan_paths: Sequence[str]) -> str:
    schema = {"language": "python", "code": "<FULL FILE HERE>"}
    target_rel = str(_module_to_relpath(mod)).replace("\\", "/")
    project_desc = read_text(PROMPTS["project"]) or "(no project_description.txt)"
    dev1 = read_text(PROMPTS["dev1"]) or ""
    dev2 = read_text(PROMPTS["dev2"]) or ""
    root_pkg = target_rel.split("/", 1)[0] if "/" in target_rel else "src"
    return "\n".join(
        [
            "You are a SENIOR DEVELOPER. Create a NEW Python module as JSON only.",
            dev1,
            dev2,
            "HARD REQUIREMENTS: Tkinter only for UI; absolute imports from root.",
            f"Target path (relative to repo root): {target_rel}",
            "Context: This module is referenced by tests and must exist.",
            f"It MUST define these top-level names: {list(sym_list)}",
            f"All imports MUST be absolute from the root package '{root_pkg}.' — never use relative imports.",
            "Keep it minimal but production-ready. Prefer stdlib; do not access network.",
            "Return ONLY a JSON object with keys {\"language\",\"code\"}. No fences, no prose.",
            json.dumps(schema, indent=2),
            "\nProject description for context:\n" + project_desc,
            f"\nOther planned files for context: {plan_paths}",
        ]
    )


def _prompt_edit_module_to_add_symbols(mod: str, missing: Sequence[str], existing_code: str, plan_paths: Sequence[str]) -> str:
    schema = {"language": "python", "code": "<FULL FILE HERE>"}
    target_rel = str(_module_to_relpath(mod)).replace("\\", "/")
    verify = read_text(PROMPTS["verify"]) or ""
    return "\n".join(
        [
            "You are a SENIOR DEVELOPER. Update the EXISTING file to add missing exports.",
            verify,
            "HARD REQUIREMENTS: Tkinter only for UI; absolute imports from root.",
            f"Target path: {target_rel}",
            f"Missing symbols to add: {list(missing)}",
            "Rules:",
            "- Return ONLY JSON with {\"language\",\"code\"}.",
            "- No markdown/prose.",
            "- Preserve existing behavior; add minimal, correct definitions for the missing names.",
            "- **All imports must be absolute from the repo root package; never use relative imports.**",
            json.dumps(schema, indent=2),
            "\n--- BEGIN EXISTING FILE ---\n" + existing_code + "\n--- END EXISTING FILE ---\n",
            f"Other files for context: {plan_paths}",
        ]
    )


def targeted_reprompts(plan: Dict[str, Any], llm: ProxyGemini) -> None:
    if not ENABLE_LINTER or LINT_REPROMPTS <= 0:
        log("Universal linter/reprompts disabled or 0 retries configured.")
        return

    plan_paths = [f.get("path", "") for f in plan.get("files", [])]
    attempts = 0
    while attempts < LINT_REPROMPTS:
        attempts += 1
        log(f"Universal linter pass {attempts}/{LINT_REPROMPTS}...")
        missing_mods, missing_syms, broken_mods = lint_imports_and_symbols(CODE_DIR)

        # Broken modules -> try auto-fix or regenerate
        for mod, err in sorted(broken_mods.items()):
            log(f"Broken module detected: {mod} -> {err}")
            if AUTOFIX_RELATIVE and "relative import" in (err or ""):
                if _autofix_relative_imports(mod):
                    ok, err2, _tb2 = _try_import(mod)
                    if ok:
                        log(f"Auto-fix successful for {mod}")
                        continue
                    else:
                        log(f"Auto-fix attempted for {mod} but still failing: {err2}")
            expected: Set[str] = set()
            for _t, (m, names) in _gather_all_test_imports(CODE_DIR):
                if m == mod and names:
                    expected.update(names)
            prompt = _prompt_make_module(mod, sorted(expected), plan_paths)
            try:
                payload = _json_retry(llm, prompt, is_code_payload=True, dump_dir=FORENSICS_DIR / "reprompts", tag=f"regen_{mod.replace('.', '_')}")
                code = str(payload.get("code", ""))
                if code.strip():
                    target = CODE_DIR / _module_to_relpath(mod)
                    write_python_file(target, code, tag=f"regen_{mod.replace('.', '_')}")
                    log(f"WROTE (broken module regen) {safe_relpath(target)}")
                else:
                    log(f"Targeted reprompt (broken module) got empty code for {mod}")
            except Exception as e:
                log(f"Targeted reprompt (broken module {mod}) failed: {e}")

        # Create missing modules
        for mod in sorted(missing_mods):
            expected: Set[str] = set()
            for _t, (m, names) in _gather_all_test_imports(CODE_DIR):
                if m == mod and names:
                    expected.update(names)
            prompt = _prompt_make_module(mod, sorted(expected), plan_paths)
            try:
                payload = _json_retry(llm, prompt, is_code_payload=True, dump_dir=FORENSICS_DIR / "reprompts", tag=f"missing_{mod.replace('.', '_')}")
                code = str(payload.get("code", ""))
                if not code.strip():
                    raise ValueError("empty code")
                target = CODE_DIR / _module_to_relpath(mod)
                write_python_file(target, code, tag=f"missing_{mod.replace('.', '_')}")
                log(f"WROTE (missing module) {safe_relpath(target)}")
            except Exception as e:
                log(f"Targeted reprompt (module {mod}) failed: {e}")

        # Add missing symbols
        for mod, symset in sorted(missing_syms.items()):
            target = CODE_DIR / _module_to_relpath(mod)
            existing = read_text(target, "")
            if not existing:
                continue
            prompt = _prompt_edit_module_to_add_symbols(mod, sorted(symset), existing, plan_paths)
            try:
                payload = _json_retry(llm, prompt, is_code_payload=True, dump_dir=FORENSICS_DIR / "reprompts", tag=f"addsyms_{mod.replace('.', '_')}")
                code = str(payload.get("code", ""))
                if not code.strip():
                    raise ValueError("empty code")
                write_python_file(target, code, tag=f"addsyms_{mod.replace('.', '_')}")
                log(f"WROTE (added symbols) {safe_relpath(target)}")
            except Exception as e:
                log(f"Targeted reprompt (symbols {mod}) failed: {e}")

        # Re-normalize packages after changes
        normalize_packages("src")

        # Recheck after this round
        missing_mods2, missing_syms2, broken_mods2 = lint_imports_and_symbols(CODE_DIR)
        if not missing_mods2 and not missing_syms2 and not broken_mods2:
            log("Universal linter: all modules/symbols satisfied.")
            return
        else:
            log(
                "Universal linter still sees gaps: "
                f"missing_modules={sorted(missing_mods2)} "
                f"missing_symbols={{ {', '.join(f'{k}: {sorted(v)}' for k,v in missing_syms2.items())} }} "
                f"broken_modules={{ {', '.join(f'{k}: {v}' for k,v in broken_mods2.items())} }}"
            )

    log("Universal linter: reached retry limit; continuing to test run.")


# ---------------------- FINALIZER ----------------------

def step_finalize(llm: ProxyGemini) -> None:
    final1 = read_text(PROMPTS["final1"]) or ""
    final2 = read_text(PROMPTS["final2"]) or ""
    schema = {"readme": "# Project...", "requirements": "pytest\nrequests\nmarkdown-it-py\n"}
    prompt = "\n".join(
        [
            "You are the FINALIZER.",
            final1,
            final2,
            "Return ONLY a JSON object with keys {\"readme\", \"requirements\"}. No fences, no prose.",
            json.dumps(schema, indent=2),
        ]
    )
    txt = llm.ask(prompt, tag="finalizer") or ""
    try:
        obj = parse_json_best(txt, want=dict, require_code=False, dump_dir=FORENSICS_DIR / "finalizer", tag="final")
    except Exception:
        obj = {}
    readme = str(obj.get("readme", "# Project\n\n(README was not returned as JSON.)\n"))
    reqs = str(obj.get("requirements", "pytest\nrequests\nmarkdown-it-py\n"))
    (CODE_DIR / "README.md").write_text(readme, encoding="utf-8")
    (CODE_DIR / "requirements.txt").write_text(reqs if reqs.endswith("\n") else reqs + "\n", encoding="utf-8")
    log("Finalized README.md and requirements.txt.")


# ---------------------- PYTEST RUN + ERROR PARSING ----------------------

def run_pytest_once_capture() -> Tuple[int, str]:
    req = CODE_DIR / "requirements.txt"
    if req.exists():
        log("Installing requirements (best-effort)...")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)], check=False)
        except Exception as e:
            log(f"pip install failed (continuing): {e}")
    log("Running pytest...")
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=str(CODE_DIR), env=env, capture_output=True, text=True)
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    for line in out.splitlines()[:300]:
        log(line)
    LOG_FILE.write_text(read_text(LOG_FILE) + "\n=== PYTEST FULL OUTPUT ===\n" + out, encoding="utf-8")
    return proc.returncode, out


_MNF_RE = re.compile(r"ModuleNotFoundError:\s+No module named '([^']+)'")
_CANNOT_IMPORT_RE = re.compile(r"cannot import name '([^']+)' from '([^']+)'")


def parse_pytest_import_errors(pytest_output: str) -> Tuple[Set[str], Dict[str, Set[str]]]:
    missing_modules: Set[str] = set()
    missing_symbols: Dict[str, Set[str]] = {}
    for m in _MNF_RE.finditer(pytest_output):
        missing_modules.add(m.group(1))
    for m in _CANNOT_IMPORT_RE.finditer(pytest_output):
        sym = m.group(1)
        mod = m.group(2)
        missing_symbols.setdefault(mod, set()).add(sym)
    return missing_modules, missing_symbols


# ---------------------- HEALTHCHECK ----------------------

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


# ---------------------- MAIN ----------------------

def main() -> int:
    try:
        snapshot_previous()
        ensure_dirs()
        LOG_FILE.write_text("", encoding="utf-8")
        log("Starting CodePori (Proxy/Gemini) pipeline...")
        log(f"Proxy base: {PROXY_BASE}")
        log(f"Primary model: {PRIMARY_MODEL} | Fallback: {FALLBACK_MODEL}")
        proxy_healthcheck()

        llm = ProxyGemini(PROXY_BASE, PRIMARY_MODEL, FALLBACK_MODEL)

        # 1) Plan
        plan = step_plan(llm)
        log("Generated plan.")

        # 2) Generate files & tests
        step_generate(plan, llm)
        log("Generated files.")

        # 3) Syntax gate
        syntax_gate(CODE_DIR)

        # 4) Normalize packages
        normalize_packages("src")

        # 5) Universal linter + targeted reprompts (pre-test)
        targeted_reprompts(plan, llm)

        # 6) Finalizer
        step_finalize(llm)
        log("Finalized repo.")

        # 7) Diff report
        write_diff_report()

        # 8) Pytest
        rc, pout = run_pytest_once_capture()
        if rc == 0:
            log("✅ Tests passed.")
            log("DONE: output in ./output/code")
            return 0

        # 9) If pytest shows import/name problems, do one last targeted round
        mm, ms = parse_pytest_import_errors(pout)
        if (mm or ms) and LINT_REPROMPTS > 0:
            log("Detected pytest import/name errors. Attempting one targeted repair round...")
            plan_paths = [f.get("path", "") for f in plan.get("files", [])]

            # Modules that pytest says are missing
            for mod in sorted(mm):
                target = CODE_DIR / _module_to_relpath(mod)
                if target.exists():
                    if _autofix_relative_imports(mod):
                        ok, err2, _ = _try_import(mod)
                        if ok:
                            log(f"Auto-fix successful for {mod}")
                            continue
                        else:
                            log(f"Auto-fix attempted for {mod} but still failing: {err2}")
                expected: Set[str] = set()
                for _t, (m, names) in _gather_all_test_imports(CODE_DIR):
                    if m == mod and names:
                        expected.update(names)
                prompt = _prompt_make_module(mod, sorted(expected), plan_paths)
                try:
                    payload = _json_retry(llm, prompt, is_code_payload=True, dump_dir=FORENSICS_DIR / "reprompts", tag=f"pytest_module_{mod.replace('.', '_')}")
                    code = str(payload.get("code", ""))
                    if not code.strip():
                        raise ValueError("empty code")
                    write_python_file(target, code, tag=f"pytest_module_{mod.replace('.', '_')}")
                    log(f"WROTE (pytest-module) {safe_relpath(target)}")
                except Exception as e:
                    log(f"pytest-module repair failed for {mod}: {e}")

            # cannot import name ...
            for mod, syms in sorted(ms.items()):
                target = CODE_DIR / _module_to_relpath(mod)
                existing = read_text(target, "")
                if not existing:
                    continue
                prompt = _prompt_edit_module_to_add_symbols(mod, sorted(syms), existing, plan_paths)
                try:
                    payload = _json_retry(llm, prompt, is_code_payload=True, dump_dir=FORENSICS_DIR / "reprompts", tag=f"pytest_symbols_{mod.replace('.', '_')}")
                    code = str(payload.get("code", ""))
                    if not code.strip():
                        raise ValueError("empty code")
                    write_python_file(target, code, tag=f"pytest_symbols_{mod.replace('.', '_')}")
                    log(f"WROTE (pytest-symbols) {safe_relpath(target)}")
                except Exception as e:
                    log(f"pytest-symbols repair failed for {mod}: {e}")

            # Re-normalize packages and re-run pytest once
            normalize_packages("src")
            syntax_gate(CODE_DIR)
            rc2, pout2 = run_pytest_once_capture()
            if rc2 == 0:
                log("✅ Tests passed after targeted repair.")
                log("DONE: output in ./output/code")
                return 0
            else:
                log("❌ Tests still failing after targeted repair. See output above and run.log")
                return 1

        log("❌ Tests failed. See pytest output above and ./output/run.log")
        return 1

    except SystemExit as e:
        return int(e.code)
    except Exception as e:
        log(f"FATAL: {e}\n{traceback.format_exc()}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
