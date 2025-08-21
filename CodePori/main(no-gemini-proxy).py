def step_finalize(model: ProxyGemini, plan_json: str) -> str:
    """Finalize the project by generating comprehensive documentation and verification."""
    final_prompt = f"""
    You are the FINALIZER.
    
    Project plan:
    {json.dumps(json.loads(plan_json), indent=2)}
    
    Produce a comprehensive JSON with:
    - readme: Complete README.md content
    - requirements: requirements.txt content
    - verification: pytest verification steps
    
    Return ONLY JSON.
    """
    
    text = model.generate_content(final_prompt)
    log(f"Raw model response for finalizer:\n---\n{text}\n---")
    
    parse_result = parse_llm_fix(text, required_keys=("readme", "requirements"))
    
    if not parse_result.success:
        log(f"FATAL: Finalizer step failed to parse LLM response: {parse_result.error.brief}")
        log(f"Parse attempts: {parse_result.error.attempts}")
        raise RuntimeError(f"Finalizer step did not return valid JSON object: {parse_result.error.brief}")
    
    return json.dumps(parse_result.data, ensure_ascii=False)

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

TIMEOUT_SECONDS = 600
MAX_DEBUG_ITERS = 5

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
            log(f"Proxy health check successful: {r.json()}")
        else:
            log(f"Warning: /health HTTP {r.status_code} - {r.text}")
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

    def generate_content(self, prompt: str) -> Optional[str]:
        """
        Always uses the streaming endpoint and assembles the response.
        """
        payload = self._payload(prompt)

        def _process_stream(response: requests.Response) -> str:
            full_text = []
            for line in response.iter_lines(decode_unicode=True):
                if line.startswith("data:"):
                    line = line[len("data:"):]
                line = line.strip()
                if not line:
                    continue
                
                try:
                    data = json.loads(line)
                    text = self._extract_text(data)
                    if text:
                        full_text.append(text)
                except json.JSONDecodeError:
                    log(f"Warning: Could not decode JSON line from stream: {line}")
                except Exception as e:
                    log(f"Warning: Error processing stream line: {e}")
            return "".join(full_text)

        # Try primary
        try:
            url = self._endpoint(self.primary_model, stream=True)
            r = self.session.post(url, json=payload, timeout=TIMEOUT_SECONDS, stream=True)
            if r.ok:
                text = _process_stream(r)
                if text:
                    return text
                log("Empty response text from primary; trying fallback model.")
            else:
                log(f"Primary model failed: HTTP {r.status_code} - {r.text[:200]}")
        except Exception as e:
            log(f"Primary model exception: {e}")

        # Fallback
        try:
            url = self._endpoint(self.fallback_model, stream=True)
            r = self.session.post(url, json=payload, timeout=TIMEOUT_SECONDS, stream=True)
            if r.ok:
                text = _process_stream(r)
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
    log(f"Raw manager response:\n---\n{text}\n---")
    
    parse_result = parse_llm_fix(text, required_keys=("architecture", "files", "tests", "notes"))
    
    if not parse_result.success:
        log(f"FATAL: Manager/plan step failed to parse LLM response: {parse_result.error.brief}")
        log(f"Parse attempts: {parse_result.error.attempts}")
        raise RuntimeError(f"Manager/plan step did not return valid JSON: {parse_result.error.brief}")
    
    plan = parse_result.data
    return json.dumps(plan, ensure_ascii=False)


def step_generate_files(model: ProxyGemini, plan_json: str) -> None:
    plan = json.loads(plan_json)

    # Code files
    for f in plan.get("files", []):
        rel = f["path"].strip().lstrip("/\\")
        path = CODE_DIR / rel
        log(f"Attempting to generate file: {path}") # Log the full path
        path.parent.mkdir(parents=True, exist_ok=True) # Ensure parent directories exist

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
        log(f"Sending request to model for {rel}...")
        text = model.generate_content(dev_prompt)
        log(f"Received response from model for {rel}.")
        log(f"Raw model response for {rel}:\n---\n{text}\n---")
        if not text:
            log(f"Model produced no text for {rel}. Skipping file generation.")
            continue # Skip to next file instead of raising error
        
        code = CodeExtractor.extract_code(text)
        if not code:
            log(f"CodeExtractor could not extract valid code for {rel}. Writing raw text (including markdown fences).")
            code = text # Fallback to writing raw text if extraction fails
        
        log(f"Writing file: {path}")
        path.write_text(code, encoding="utf-8")
        log(f"WROTE {path}")

    # Test files
    for t in plan.get("tests", []):
        rel = t["path"].strip().lstrip("/\\")
        path = CODE_DIR / rel
        log(f"Attempting to generate test file: {path}") # Log the full path
        path.parent.mkdir(parents=True, exist_ok=True) # Ensure parent directories exist

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
        log(f"Sending request to model for test file {rel}...")
        text = model.generate_content(test_prompt)
        log(f"Received response from model for test file {rel}.")
        log(f"Raw model response for test file {rel}:\n---\n{text}\n---")
        if not text:
            log(f"Model produced no text for test file {rel}. Skipping test file generation.")
            continue # Skip to next test file
        
        code = CodeExtractor.extract_code(text)
        if not code:
            log(f"CodeExtractor could not extract valid code for test file {rel}. Writing raw text (including markdown fences).")
            code = text # Fallback to writing raw text if extraction fails
        
        log(f"Writing test file: {path}")
        path.write_text(code, encoding="utf-8")
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
    log(f"Raw model response for finalizer:\n---\n{text}\n---")
    
    parse_result = parse_llm_fix(text, required_keys=("readme", "requirements"))
    
    if not parse_result.success:
        log(f"FATAL: Finalizer step failed to parse LLM response: {parse_result.error.brief}")
        log(f"Parse attempts: {parse_result.error.attempts}")
        raise RuntimeError(f"Finalizer step did not return valid JSON object: {parse_result.error.brief}")
    
    data = parse_result.data

    readme_content = data.get("readme", "")
    reqs_content = data.get("requirements", "")

    (CODE_DIR / "README.md").write_text(readme_content, encoding="utf-8")
    (CODE_DIR / "requirements.txt").write_text(reqs_content, encoding="utf-8")
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
        log(f"Raw model response for debugger:\n---\n{text}\n---")
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
class CodeExtractor:
    @staticmethod
    def _prepare_and_validate(code_block: str) -> Optional[str]:
        if not code_block:
            log("Val skip: empty block.")
            return None
        try:
            dedented_code = textwrap.dedent(code_block)
            cleaned_code = dedented_code.strip()
        except Exception as e:
            log(f"Dedent/strip err: {e}")
            return None

        if not cleaned_code or cleaned_code.isspace():
            log("Val fail: empty/ws clean.")
            return None

        try:
            tree = ast.parse(cleaned_code)
            if not tree.body:
                log("Val trivial: Empty AST.")
                return None
            is_trivial = all(
                isinstance(n, (ast.Pass, ast.Expr))
                and (
                    isinstance(n, ast.Pass)
                    or isinstance(getattr(n, "value", None), ast.Constant)
                )
                for n in tree.body
            )
            if is_trivial and not (
                len(tree.body) == 1 and isinstance(tree.body[0], ast.Pass)
            ):
                log("Val trivial: Only Pass/Const.")
                return None
            log("Val pass: syntax valid & non-trivial.")
            return cleaned_code
        except SyntaxError as e:
            try:
                lines = cleaned_code.splitlines()
                lc = (
                    lines[e.lineno - 1]
                    if e.lineno and 0 < e.lineno <= len(lines)
                    else "N/A"
                )
            except IndexError:
                lc = "N/A(Idx Err)"
            log(
                f"Val fail WITHIN block: SyntaxError {e.msg} L{e.lineno} Ctx:'{lc}'. Returning None."
            )
            return None  # Return None if internal SyntaxError
        except Exception as e:
            log(
                f"Val fail: Unexpected AST err: {e}"
            )
            return None

    @staticmethod
    def extract_code(response: str) -> Optional[str]:
        log("Attempting code extraction...")
        if not response:
            log(
                "Cannot extract code from empty response."
            )
            return None

        bt = chr(96)
        nl = chr(10)
        end_tag_str = f"{bt*3}"
        python_block_start_tag = f"{end_tag_str}python{nl}"
        fallback_block_start_tag = f"{end_tag_str}{nl}"

        # Strategy 1: Specific ```python block
        try:
            start = response.find(python_block_start_tag)
            if start != -1:
                log(
                    f"Found '{python_block_start_tag.strip()}'."
                )
                end = response.find(end_tag_str, start + len(python_block_start_tag))
                if end != -1:
                    code_raw = response[
                        start + len(python_block_start_tag) : end
                    ]
                    processed = CodeExtractor._prepare_and_validate(code_raw)
                    if processed is not None:
                        log(
                            f"Processed via '```python' ({len(processed)} chars)."
                        )
                        return processed
        except Exception as e:
            log(
                f"Specific python block err: {e}"
            )

        # Strategy 2: Fallback generic ``` block
        try:
            start = response.find(fallback_block_start_tag)
            specific_start = response.find(python_block_start_tag)
            if start != -1 and (start != specific_start or specific_start == -1):
                log(
                    f"Found distinct '{fallback_block_start_tag.strip()}'."
                )
                end = response.find(end_tag_str, start + len(fallback_block_start_tag))
                if end != -1:
                    code_raw = response[
                        start + len(fallback_block_start_tag) : end
                    ]
                    processed = CodeExtractor._prepare_and_validate(code_raw)
                    if processed is not None:
                        log(
                            f"Processed via fallback '```' ({len(processed)} chars)."
                        )
                        return processed
        except Exception as e:
            log(f"Fallback block err: {e}")

        # Strategy 3: Regex (Cleaned formatting)
        processed_blocks = []
        try:
            generic_block_pattern = r"```(?:[a-zA-Z0-9_]*)?\s*?\n(.*?)\n?```"
            generic_block_regex = re.compile(
                generic_block_pattern, re.DOTALL | re.IGNORECASE
            )
            log("Compiled regex pattern.")
            for match in generic_block_regex.finditer(response):
                block = match.group(1)
                processed = CodeExtractor._prepare_and_validate(block)
                if processed is not None:
                    log("Regex block processed.")
                    processed_blocks.append(processed)
                    break  # Use first valid match
        except Exception as e:
            log(f"Regex err: {e}")

        if processed_blocks:
            log(
                f"Processed via regex (FIRST valid, {len(processed_blocks[0])} chars)."
            )
            return processed_blocks[0]

        log(
            "Failed to extract/process code via all methods."
        )
        return None


def normalize_ws(s: str) -> str:

    return "\n".join(line.rstrip() for line in s.replace("\r\n", "\n").split("\n")).strip()


# ---------- Data structures from robust_llm_parser.py ----------

@dataclass
class ParseAttempt:
    name: str
    success: bool
    error: Optional[str] = None
    note: Optional[str] = None

@dataclass
class ParseErrorDetail:
    brief: str
    raw_excerpt: str
    sanitized_excerpt: str
    attempts: List[ParseAttempt] = field(default_factory=list)
    re_prompt: str = ""
    context_used: Dict[str, str] = field(default_factory=dict)

@dataclass
class ParseResult:
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[ParseErrorDetail] = None


# ---------- Helpers from robust_llm_parser.py ----------

def _trim(s: str, n: int = 1200) -> str:
    if s is None:
        return ""
    s = s.replace("\r", "")
    if len(s) <= n:
        return s
    head = s[: n // 2]
    tail = s[- n // 2 :]
    return head + "\n…[snip]…\n" + tail

SMART_QUOTES = {
    "\u2018": "'", "\u2019": "'", "\u201C": '"', "\u201D": '"',
    "\u2013": "-", "\u2014": "-", "\u00A0": " ", "\u200B": "",
}

def _normalize_unicode(s: str) -> str:
    for bad, good in SMART_QUades.items():
        s = s.replace(bad, good)
    return s

def _strip_code_fences(s: str) -> str:
    fence = re.compile(r"```(?:json|python)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
    m = fence.search(s)
    return m.group(1) if m else s

def _balanced_braces_extract(s: str) -> Optional[str]:
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None

def _fix_trailing_commas(s: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", s)

def _py_to_json_literals(s: str) -> str:
    out = []
    in_str = False
    quote = ""
    i = 0
    while i < len(s):
        ch = s[i]
        if not in_str and ch in ("'", '"'):
            in_str = True
            quote = ch
            out.append(ch)
            i += 1
            continue
        if in_str and ch == "\\":
            if i + 1 < len(s):
                out.extend([ch, s[i+1]])
                i += 2
                continue
        if in_str and ch == quote:
            in_str = False
            out.append(ch)
            i += 1
            continue
        if not in_str:
            if s.startswith("None", i):
                out.append("null"); i += 4; continue
            if s.startswith("True", i):
                out.append("true"); i += 4; continue
            if s.startswith("False", i):
                out.append("false"); i += 5; continue
        out.append(ch)
        i += 1
    return "".join(out)

def _escape_newlines_in_strings(s: str) -> str:
    out = []
    in_str = False
    quote = ""
    i = 0
    while i < len(s):
        ch = s[i]
        if not in_str and ch in ('"', "'"):
            in_str = True
            quote = ch
            out.append('"') if ch == "'" else out.append(ch)
            i += 1
            continue
        if in_str:
            if ch == "\\":
                if i + 1 < len(s):
                    out.extend([ch, s[i+1]])
                    i += 2
                    continue
            if ch == quote:
                in_str = False
                out.append('"')
                i += 1
                continue
            if ch == "\n":
                out.append("\\n")
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)

def _strip_double_wrapping_braces(s: str) -> str:
    s2 = s.strip()
    if s2.startswith("{{") and s2.endswith("}}"):
        return s2[1:-1]
    return s

def _ensure_string(val: Any) -> str:
    if isinstance(val, list):
        return "\n".join(str(x) for x in val)
    return str(val)

# ---------- Re-prompt builder ----------

RE_PROMPT_TEMPLATE = (
    "You are a code-fixing agent. Your PREVIOUS message could not be parsed as JSON.\n\n"
    "REQUIREMENTS (CRITICAL):\n"
    "- Return EXACTLY one JSON object with keys {required_keys}.\n"
    "- Use double quotes for all keys/strings.\n"
    "- Do NOT include markdown fences, explanations, or extra text.\n"
    "- The \"corrected_code\" value MUST be a single JSON string (escape newlines as \\n).\n\n"
    "Example (structure only):\n"
    "{{\n"
    "  \"target_name\": \"MyClass.my_method\",\n"
    "  \"corrected_code\": \"def my_method(self, x):\\n    return x + 1\\n\"\n"
    "}}\n\n"
    "Parse error summary: {error_brief}\n\n"
    "Your previous content (excerpt):\n"
    "{raw_excerpt}\n\n"
    "--- CONTEXT YOU MAY NEED ---\n"
    "{full_code_block}"
    "{error_output_block}"
    "{history_block}"
)

def _build_reprompt(required_keys, detail, context):
    full_code_block = ""
    error_output_block = ""
    history_block = ""
    if context:
        if context.get("full_code"):
            full_code_block = "FULL CODE:\n" + _trim(context["full_code"], 3500) + "\n"
        if context.get("error_output"):
            error_output_block = "ERROR OUTPUT:\n" + _trim(context["error_output"], 2000) + "\n"
        if context.get("history_summary"):
            history_block = "DEBUG HISTORY:\n" + _trim(context["history_summary"], 2000) + "\n"
    return RE_PROMPT_TEMPLATE.format(
        required_keys=list(required_keys),
        error_brief=detail.brief,
        raw_excerpt=_trim(detail.raw_excerpt, 800),
        full_code_block=full_code_block,
        error_output_block=error_output_block,
        history_block=history_block,
    )

# ---------- Normalization ----------

ALIAS_KEYS = {
    "target": "target_name",
    "name": "target_name",
    "function": "target_name",
    "block": "target_name",
    "code": "corrected_code",
    "fixed_code": "corrected_code",
    "patch": "corrected_code",
}

def _normalize_payload(obj: Any, required_keys) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValueError("payload is not an object")
    obj = dict(obj)
    for k, v in list(obj.items()):
        kk = ALIAS_KEYS.get(k, k)
        if kk != k:
            obj.setdefault(kk, v)

    for need in required_keys:
        if need not in obj:
            raise KeyError(f"missing required key: {need}")

    cc = obj["corrected_code"]
    cc = _ensure_string(cc).replace("\r\n", "\n").replace("\r", "\n")
    if not cc.endswith("\n"):
        cc += "\\n"
    obj["corrected_code"] = cc
    obj["target_name"] = str(obj["target_name"])
    return {k: obj[k] for k in required_keys}

# ---------- Main parser ----------

def parse_llm_fix(raw_text: str,
                  required_keys: tuple = ("target_name", "corrected_code"),
                  context: Optional[Dict[str, str]] = None) -> ParseResult:
    attempts: List[ParseAttempt] = []
    original = raw_text or ""
    work = _normalize_unicode(original)
    work = work.replace("\r", "")
    work_no_fence = _strip_code_fences(work)

    # Strategy 1: direct json
    try:
        data = json.loads(work_no_fence)
        attempts.append(ParseAttempt("json.loads (direct)", True))
        out = _normalize_payload(data, required_keys)
        return ParseResult(True, out, None)
    except Exception as e:
        attempts.append(ParseAttempt("json.loads (direct)", False, str(e)))

    # Strategy 2: extract balanced braces then json
    try:
        candidate = _balanced_braces_extract(work_no_fence)
        if candidate:
            data = json.loads(candidate)
            attempts.append(ParseAttempt("balanced-braces -> json", True, note="used first balanced {...}"))
            out = _normalize_payload(data, required_keys)
            return ParseResult(True, out, None)
        attempts.append(ParseAttempt("balanced-braces -> json", False, "no balanced object found"))
    except Exception as e:
        attempts.append(ParseAttempt("balanced-braces -> json", False, str(e)))

    # Strategy 3: python-literal (dict) then coerce
    try:
        pyobj = ast.literal_eval(work_no_fence)
        if isinstance(pyobj, dict):
            attempts.append(ParseAttempt("ast.literal_eval (dict)", True))
            out = _normalize_payload(pyobj, required_keys)
            return ParseResult(True, out, None)
        attempts.append(ParseAttempt("ast.literal_eval (dict)", False, "result not a dict"))
    except Exception as e:
        attempts.append(ParseAttempt("ast.literal_eval (dict)", False, str(e)))

    # Strategy 4: repair common issues and json again
    try:
        repaired = _strip_double_wrapping_braces(work_no_fence)
        repaired = _py_to_json_literals(repaired)
        repaired = _escape_newlines_in_strings(repaired)
        repaired = _fix_trailing_commas(repaired)
        data = json.loads(repaired)
        attempts.append(ParseAttempt("repaired->json", True,
                                     note="py-literals->json, escaped newlines, removed trailing commas"))
        out = _normalize_payload(data, required_keys)
        return ParseResult(True, out, None)
    except Exception as e:
        attempts.append(ParseAttempt("repaired->json", False, str(e)))

    # Strategy 5: regex pull for keys
    try:
        tgt = re.search(r'"?target_name"?\s*:\s*"([^"]+)"', work_no_fence)
        code = re.search(r'"?(corrected_code|code|fixed_code)"?\s*:\s*"((?:\\.|[^"\\])*)"', work_no_fence, re.DOTALL)
        if tgt and code:
            data = {"target_name": tgt.group(1), "corrected_code": code.group(2)}
            attempts.append(ParseAttempt("regex (key extraction)", True))
            out = _normalize_payload(data, required_keys)
            return ParseResult(True, out, None)
        attempts.append(ParseAttempt("regex (key extraction)", False, "required keys not both found"))
    except Exception as e:
        attempts.append(ParseAttempt("regex (key extraction)", False, str(e)))

    detail = ParseErrorDetail(
        brief="Unable to parse LLM output into required JSON object.",
        raw_excerpt=_trim(original, 2000),
        sanitized_excerpt=_trim(work_no_fence, 2000),
        attempts=attempts,
    )
    detail.re_prompt = _build_reprompt(required_keys, detail, context)
    if context:
        for k in ("full_code", "error_output", "history_summary"):
            if k in context and context[k]:
                detail.context_used[k] = _trim(context[k], 2000)

    return ParseResult(False, None, detail)


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
