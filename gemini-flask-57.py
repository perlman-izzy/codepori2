#!/usr/bin/env python3
"""
gemini-flask-final.py – Intelligent proxy with adaptive, per-model rate limiting.

• Per-Model Rate Limiting: Applies separate RPM limits for each model to maximize throughput.
• Intelligent 429 Error Handling: Differentiates between temporary rate limits (cooldown) 
  and hard quota exhaustion (disqualifies key for that model).
• Key Pre-Validation: Concurrently validates all keys at startup for a high-speed, valid-only pool.
• Efficient Session Management: Uses round-robin for keys and Tor sessions.
• Aggressive Tor IP Renewal on 403 and temporary 429 errors.
"""

import os
import re
import sys
import subprocess
import json
import time
import shutil
import socket
import logging
import threading
import atexit
import random
from pathlib import Path
from collections import deque, defaultdict
from itertools import cycle
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, request, jsonify, Response
import requests
import psutil

try:
    from stem import Signal
    from stem.control import Controller
    STEM_ENABLED = True
except ImportError:
    STEM_ENABLED = False

# ---------------- CONFIG -----------------
KEY_FILES = [
    Path("/Users/williamwhite/myapikeys/old/apikeys"),
    Path("/Users/williamwhite/myapikeys/old/apikeys2"),
    Path("/Users/williamwhite/myapikeys/old/apikeys3"),
    Path("/Users/williamwhite/myapikeys/old/apikeys4"),
]
TOR_PORTS = [9050, 9052, 9054]
TOR_CONTROL_PORTS = [p + 1 for p in TOR_PORTS]

# --- PER-MODEL RATE LIMITING ---
MODEL_RPM_LIMITS = {
    "gemini-1.5-flash": 15,
    "gemini-1.5-pro": 2,
    "gemini-2.5-flash-lite": 5,
    "DEFAULT": 11
}
KEY_COOLDOWN_PERIOD = 60

CLI_CANDIDATES = [
    Path(os.getenv("GEMINI_CLI_DIR", "")),
    Path.home() / ".npm-global/lib/node_modules/@google/gemini-cli",
    Path.home() / ".config/yarn/global/node_modules/@google/gemini-cli",
    Path.home() / "gemini-cli-2-revised-copy2",
]

# ---------------- GLOBAL STATE -----------------
log = logging.getLogger(__name__)
VALID_API_KEYS = []
TOR_SESSIONS = []
key_cycler = None
session_cycler = None
key_cooldown = {}
exhausted_keys = defaultdict(set) # Model -> set of exhausted keys
key_lock = threading.Lock()

class RateLimiter:
    def __init__(self, max_requests, window_seconds):
        self.max_requests, self.window_seconds = max_requests, window_seconds
        self.request_timestamps = deque()
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            while self.request_timestamps and self.request_timestamps[0] <= now - self.window_seconds:
                self.request_timestamps.popleft()
            if len(self.request_timestamps) >= self.max_requests:
                wait_time = (self.request_timestamps[0] + self.window_seconds) - now
                if wait_time > 0:
                    log.warning(f"Rate limit hit ({self.max_requests}/min). Throttling for {wait_time:.2f}s.")
                    time.sleep(wait_time)
            self.request_timestamps.append(time.monotonic())

MODEL_RATE_LIMITERS = defaultdict(
    lambda: RateLimiter(MODEL_RPM_LIMITS["DEFAULT"], 60),
    {model: RateLimiter(rpm, 60) for model, rpm in MODEL_RPM_LIMITS.items()}
)

def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("stem").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

def renew_tor_circuits():
    if not STEM_ENABLED: return
    log.info("Requesting new Tor circuits...")
    for port in TOR_CONTROL_PORTS:
        try:
            with Controller.from_port(port=port) as controller:
                controller.authenticate()
                controller.signal(Signal.NEWNYM)
        except Exception as e:
            log.warning(f"Could not signal NEWNYM on port {port}: {e}")

def find_tor_executable() -> str:
    for p in ["/opt/homebrew/bin/tor", "/usr/local/bin/tor", "tor"]:
        if (path := shutil.which(p)): return path
    return None

def start_tor_instance(port: int):
    tor_exe = find_tor_executable()
    if not tor_exe: return None
    try:
        data_dir = f"/tmp/tor_data_{port}"
        command = [tor_exe, "--SocksPort", str(port), "--ControlPort", str(port + 1), "--DataDirectory", data_dir, "--Log", "notice stdout", "--CookieAuthentication", "0"]
        os.makedirs(data_dir, exist_ok=True)
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    log.info(f"Tor instance on port {port} started.")
                    s = requests.Session()
                    s.proxies = {'https': f'socks5h://127.0.0.1:{port}'}
                    return s
            except (socket.timeout, ConnectionRefusedError):
                time.sleep(1)
        log.warning(f"Tor on port {port} failed to start.")
    except Exception as e:
        log.warning(f"Failed to start Tor on port {port}: {e}")
    return None

def find_cli_root() -> Path:
    for p in CLI_CANDIDATES:
        if p and p.exists() and (p / "package.json").exists():
            log.info(f"Detected gemini-cli at {p}")
            return p
    return None

def patch_cli_if_needed(root: Path):
    if not root: return
    js_files = [js for js in root.rglob("*(provider|model).js") if 'fallbackModel' in js.read_text(encoding="utf-8")]
    if js_files:
        log.info(f"Found {len(js_files)} files to patch.")
        for js_path in js_files:
            text = js_path.read_text(encoding="utf-8")
            text = re.sub(r"if\s*\([^)]*\)\s*\{\s*this\.model\s*=\s*fallbackModel;\s*\}", "", text, flags=re.S)
            text = re.sub(r"const\s+fallbackModel\s*=\s*['\"][^'\"]+['\"]\s*;", "const fallbackModel = null;", text)
            js_path.write_text(text, encoding="utf-8")
        try:
            log.info("Rebuilding CLI bundle...")
            subprocess.run(["npm", "run", "build"], cwd=root, check=True, capture_output=True, text=True)
        except Exception as e:
            log.error(f"CLI build failed: {getattr(e, 'stderr', e)}")
    else:
        log.info("CLI already patched. ✔︎")

def validate_key(key: str, session: requests.Session):
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
        response = session.get(url, timeout=10)
        if response.status_code == 200:
            return key
    except requests.exceptions.RequestException:
        pass
    return None

def load_and_validate_keys():
    global VALID_API_KEYS, key_cycler
    raw_keys = set()
    for key_file in KEY_FILES:
        if key_file.exists():
            try:
                raw_keys.update(re.findall(r"(AIza[0-9A-Za-z_\\-]{35})", key_file.read_text()))
            except Exception as e:
                log.error(f"Could not read key file {key_file}: {e}")

    if "GEMINI_API_KEY" in os.environ:
        raw_keys.add(os.environ["GEMINI_API_KEY"])
    if not raw_keys:
        log.critical("FATAL: No API keys found. Exiting.")
        sys.exit(1)

    log.info(f"Found {len(raw_keys)} unique candidate keys. Now validating...")
    valid_keys_list = []
    
    with requests.Session() as validation_session:
        validation_session.proxies = random.choice(TOR_SESSIONS).proxies if TOR_SESSIONS else {}
        with ThreadPoolExecutor(max_workers=50) as executor:
            futures = [executor.submit(validate_key, key, validation_session) for key in raw_keys]
            for future in as_completed(futures):
                if result := future.result():
                    valid_keys_list.append(result)

    VALID_API_KEYS = valid_keys_list
    if not VALID_API_KEYS:
        log.critical("FATAL: No valid API keys found. Exiting.")
        sys.exit(1)
        
    log.info(f"VALIDATION COMPLETE: {len(VALID_API_KEYS)} / {len(raw_keys)} keys are valid.")
    key_cycler = cycle(VALID_API_KEYS)

app = Flask(__name__)

def get_next_key(model_name: str):
    with key_lock:
        now = time.monotonic()
        for k, expiry in list(key_cooldown.items()):
            if now > expiry:
                del key_cooldown[k]
        
        # Check if there are any usable keys for this model
        available_keys = [k for k in VALID_API_KEYS if k not in exhausted_keys[model_name]]
        if not available_keys:
            log.error(f"All {len(VALID_API_KEYS)} keys have exhausted their quota for model {model_name}.")
            return None

        for _ in range(len(VALID_API_KEYS) * 2):
            key = next(key_cycler)
            if key not in key_cooldown and key not in exhausted_keys[model_name]:
                return key
        
        log.warning(f"All non-exhausted keys for {model_name} are on cooldown. Waiting...")
        time.sleep(1)
        return get_next_key(model_name)

def _execute_request(model: str, payload: dict, stream: bool = False):
    model_name_only = model.split('/')[-1]
    rate_limiter = MODEL_RATE_LIMITERS[model_name_only]
    
    for _ in range(len(VALID_API_KEYS)):
        rate_limiter.wait()
        key = get_next_key(model_name_only)
        if not key:
            continue
        
        session = next(session_cycler)
        action, params = ("streamGenerateContent", "alt=sse") if stream else ("generateContent", "")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name_only}:{action}?key={key}&{params}"
        
        try:
            response = session.post(url, json=payload, timeout=180, stream=stream)
            response.raise_for_status()
            log.info(f"Request SUCCEEDED for {model_name_only} with key ...{key[-4:]}")
            return response
        except requests.exceptions.HTTPError as e:
            status, text = e.response.status_code, e.response.text
            log.warning(f"Key ...{key[-4:]} failed: HTTP {status} for {model_name_only}.")
            
            if status == 429:
                if "you exceeded your current quota" in text.lower():
                    log.error(f"HARD QUOTA LIMIT. Disqualifying key ...{key[-4:]} for model {model_name_only}.")
                    with key_lock:
                        exhausted_keys[model_name_only].add(key)
                else:
                    log.warning(f"RATE LIMITED. Cooldown for key ...{key[-4:]} ({KEY_COOLDOWN_PERIOD}s).")
                    with key_lock:
                        key_cooldown[key] = time.monotonic() + KEY_COOLDOWN_PERIOD
                    renew_tor_circuits()
            elif status == 403:
                renew_tor_circuits()
        except requests.exceptions.RequestException as e:
            log.error(f"Key ...{key[-4:]} failed: Network error: {e}")
            if isinstance(e, requests.exceptions.SSLError):
                renew_tor_circuits()
            
    log.error(f"Request failed for {model_name_only} after trying all available keys.")
    return None

@app.route("/v1beta/models/<path:model>:generateContent", methods=["POST"])
def v1_generate(model):
    resp = _execute_request(model, request.get_json(force=True), stream=False)
    if resp is None:
        return jsonify({"error": "Request failed; all keys may be on cooldown or exhausted."}), 502
    return jsonify(resp.json()), resp.status_code

@app.route("/v1beta/models/<path:model>:streamGenerateContent", methods=["POST"])
def v1_stream(model):
    resp = _execute_request(model, request.get_json(force=True), stream=True)
    if resp is None:
        return jsonify({"error": "Request failed; all keys may be on cooldown or exhausted."}), 502

    def gen():
        try:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        finally:
            try:
                resp.close()
            except Exception:
                pass

    proxy_resp = Response(gen(), status=resp.status_code, mimetype="text/event-stream", direct_passthrough=True)
    proxy_resp.headers["Cache-Control"] = "no-cache"
    proxy_resp.headers["Connection"] = "keep-alive"
    proxy_resp.headers["X-Accel-Buffering"] = "no"
    return proxy_resp

@app.route("/health", methods=["GET"])
def health_check():
    with key_lock:
        cooldown_count = len(key_cooldown)
        exhausted_count = {model: len(keys) for model, keys in exhausted_keys.items()}
    return jsonify({
        "status": "healthy",
        "valid_keys": len(VALID_API_KEYS),
        "cooldown_keys": cooldown_count,
        "exhausted_keys_per_model": exhausted_count
    })

def cleanup_tor_processes():
    for proc in psutil.process_iter(['pid', 'name']):
        if 'tor' in proc.info.get('name', '').lower():
            try:
                psutil.Process(proc.info['pid']).terminate()
            except Exception:
                pass

def main():
    setup_logging()
    atexit.register(cleanup_tor_processes)
    cleanup_tor_processes()
    time.sleep(1)
    
    if not STEM_ENABLED:
        log.warning("Consider `pip install stem` for Tor IP renewal.")
    patch_cli_if_needed(find_cli_root())
    
    global TOR_SESSIONS, session_cycler
    for port in TOR_PORTS:
        if (session := start_tor_instance(port)):
            TOR_SESSIONS.append(session)
    if not TOR_SESSIONS:
        log.warning("No Tor sessions started. Using direct connections.")
        TOR_SESSIONS.append(requests.Session())
    session_cycler = cycle(TOR_SESSIONS)

    load_and_validate_keys()
    
    port = 8000
    log.info(f"Starting Flask proxy on http://0.0.0.0:{port} with {len(VALID_API_KEYS)} valid keys.")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

if __name__ == "__main__":
    main()
