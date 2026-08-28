#!/usr/bin/env python3
"""Bounded branch-local autonomous executor loop.

The LLM is stateless between iterations. Durable truth lives in state.json and
protected deterministic tests. The LLM may write only workspace/.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
WORKSPACE = ROOT / "workspace"
FROZEN_TESTS = ROOT / "frozen_tests"
EVIDENCE = ROOT / "evidence"
STATE_PATH = ROOT / "state.json"
HEARTBEAT = ROOT / "HEARTBEAT.md"
OBJECTIVE = ROOT / "OBJECTIVE.md"
BRANCH = "validated-agent-cloud-20260827"
MODEL_URL = "https://opencode.ai/zen/v1/chat/completions"
MODEL_CANDIDATES = [
    "mimo-v2.5-free",
    "hy3-free",
    "nemotron-3.5-lightning-free",
    "nemotron-3-ultra-free",
    "big-pickle",
    "north-mini-code-free",
]
MAX_ITERATIONS = int(os.environ.get("AUTOWORK_MAX_ITERATIONS", "50"))
SLEEP_SECONDS = int(os.environ.get("AUTOWORK_SLEEP_SECONDS", "300"))


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args), cwd=REPO, text=True, capture_output=True, check=check
    )


def load_state() -> dict:
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def normalize_hypothesis(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return " ".join(words[:24]) or "unspecified"


def file_snapshot(root: Path, limit: int = 70000) -> str:
    if not root.exists():
        return "<workspace empty>"
    pieces: list[str] = []
    used = 0
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        rel = p.relative_to(root)
        try:
            text = p.read_text(errors="replace")
        except Exception:
            continue
        chunk = f"\n--- {rel} ---\n{text}\n"
        if used + len(chunk) > limit:
            pieces.append("\n<snapshot truncated>\n")
            break
        pieces.append(chunk)
        used += len(chunk)
    return "".join(pieces) or "<workspace empty>"


def frozen_spec_snapshot(limit: int = 50000) -> str:
    pieces = [OBJECTIVE.read_text()]
    used = len(pieces[0])
    for p in sorted(FROZEN_TESTS.glob("test_*.py")):
        text = p.read_text(errors="replace")
        chunk = f"\n--- PROTECTED {p.name} ---\n{text}\n"
        if used + len(chunk) > limit:
            break
        pieces.append(chunk)
        used += len(chunk)
    return "".join(pieces)


def extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise ValueError("executor did not return a JSON object")


def call_model(prompt: str) -> tuple[str, str]:
    errors: list[str] = []
    for model in MODEL_CANDIDATES:
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a bounded coding executor. Return only one JSON object. "
                        "Do not claim tests passed unless the supplied observation says so."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 7000,
            "stream": False,
        }
        req = urllib.request.Request(
            MODEL_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "validated-agent-autowork/1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
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
    raise RuntimeError("all free models failed: " + " | ".join(errors))


def build_prompt(state: dict, test_output: str) -> str:
    reset = bool(state.get("force_epistemic_reset"))
    reset_rule = ""
    if reset:
        reset_rule = """
FORCED EPISTEMIC RESET:
The prior causal hypothesis has been rejected repeatedly. Do not continue it.
Return an `alternatives` array with at least two genuinely different causal/implementation hypotheses,
then choose a new `hypothesis` and `hypothesis_id`. Use the test evidence to discriminate before editing.
Do not rely on prior executor narrative; none is authoritative.
"""
    return f"""
You are implementing the isolated prototype described below.
You may modify ONLY files relative to validated_agent_cloud/workspace/.
Protected tests/objective are immutable. Do not propose changing them.
You have no shell tool. The harness will run the protected unittest suite after your edits.

Return exactly this JSON shape:
{{
  "hypothesis_id": "short-id",
  "hypothesis": "concise causal/implementation hypothesis",
  "predicted_observation": "what the protected tests should show if this is right",
  "alternatives": ["optional alternative 1", "optional alternative 2"],
  "summary": "brief description of the bounded change",
  "actions": [
    {{"type": "write_file", "path": "relative/path.py", "content": "complete file contents"}}
  ]
}}

Do not emit patches. For every changed file, emit its COMPLETE intended contents.
Keep the action set small. You can inspect the current workspace and protected tests below.
{reset_rule}

DURABLE VERIFIED FACTS:
{json.dumps(state.get('verified_facts', []), indent=2)}

DURABLE FALSIFIED HYPOTHESES:
{json.dumps(state.get('falsified_hypotheses', []), indent=2)}

LATEST NON-AUTHORITATIVE TEST OBSERVATION:
{test_output[-12000:] if test_output else '<none yet>'}

CURRENT WORKSPACE:
{file_snapshot(WORKSPACE)}

PROTECTED OBJECTIVE AND TESTS:
{frozen_spec_snapshot()}
"""


def safe_target(rel: str) -> Path:
    if not rel or Path(rel).is_absolute():
        raise ValueError(f"unsafe path: {rel!r}")
    target = (WORKSPACE / rel).resolve(strict=False)
    root = WORKSPACE.resolve(strict=False)
    if target != root and root not in target.parents:
        raise ValueError(f"path escapes workspace: {rel!r}")
    return target


def apply_actions(obj: dict) -> list[str]:
    actions = obj.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("executor returned no actions")
    if len(actions) > 8:
        raise ValueError("too many actions")
    written: list[str] = []
    for action in actions:
        if not isinstance(action, dict) or action.get("type") != "write_file":
            raise ValueError("only write_file actions are allowed")
        rel = action.get("path")
        content = action.get("content")
        if not isinstance(rel, str) or not isinstance(content, str):
            raise ValueError("write_file requires string path/content")
        if len(content) > 50000:
            raise ValueError(f"file too large: {rel}")
        target = safe_target(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        written.append(str(target.relative_to(REPO)))
    return written


def run_tests() -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(FROZEN_TESTS), "-v"],
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=150,
    )
    out = (p.stdout + "\n" + p.stderr).strip()
    return p.returncode, out


def write_evidence(iteration: int, model: str, raw: str, test_output: str, decision: str) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    raw_path = EVIDENCE / f"iteration_{iteration:03d}_executor.txt"
    test_path = EVIDENCE / f"iteration_{iteration:03d}_tests.txt"
    meta_path = EVIDENCE / f"iteration_{iteration:03d}_meta.json"
    raw_path.write_text(raw)
    test_path.write_text(test_output + "\n")
    meta = {
        "iteration": iteration,
        "model": model,
        "decision": decision,
        "executor_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "tests_sha256": hashlib.sha256(test_output.encode()).hexdigest(),
    }
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")


def update_heartbeat(state: dict, model: str, decision: str) -> None:
    obs = state.get("latest_observation") or {}
    HEARTBEAT.write_text(
        "# Cloud Autowork Heartbeat\n\n"
        f"- iteration: {state['iteration']}\n"
        f"- status: {state['status']}\n"
        f"- last model: {model}\n"
        f"- last decision: {decision}\n"
        f"- force epistemic reset: {state.get('force_epistemic_reset', False)}\n"
        f"- latest evidence: {obs.get('evidence_id', 'none')}\n"
    )


def persist(iteration: int, decision: str) -> None:
    sh("git", "config", "user.name", "validated-agent-cloud")
    sh("git", "config", "user.email", "validated-agent-cloud@users.noreply.github.com")
    sh("git", "add", "validated_agent_cloud")
    status = sh("git", "status", "--porcelain").stdout.strip()
    if not status:
        return
    sh("git", "commit", "-m", f"cloud autowork iteration {iteration}: {decision}")
    # Never force. Any unexpected concurrent branch edit becomes a visible failure.
    sh("git", "push", "origin", f"HEAD:{BRANCH}")


def sync_and_check_stop() -> bool:
    sh("git", "fetch", "origin", BRANCH)
    # Safe only when remote is an exact fast-forward of our committed local state.
    merge = sh("git", "merge", "--ff-only", f"origin/{BRANCH}", check=False)
    if merge.returncode != 0:
        raise RuntimeError("remote branch diverged; refusing to overwrite concurrent work")
    return (ROOT / "STOP").exists()


def baseline_observation() -> str:
    _, out = run_tests()
    return out


def main() -> int:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    state = load_state()
    observation = baseline_observation()

    for _ in range(MAX_ITERATIONS):
        if sync_and_check_stop():
            state["status"] = "STOP_REQUESTED"
            save_state(state)
            update_heartbeat(state, "none", "STOP")
            persist(state["iteration"], "STOP")
            return 0

        state = load_state()
        state["iteration"] = int(state.get("iteration", 0)) + 1
        iteration = state["iteration"]
        prompt = build_prompt(state, observation)

        try:
            model, raw = call_model(prompt)
            obj = extract_json(raw)
            hypothesis = str(obj.get("hypothesis", "")).strip()
            hypothesis_id = str(obj.get("hypothesis_id", "")).strip() or f"H-{iteration}"
            alternatives = obj.get("alternatives") or []
            if state.get("force_epistemic_reset") and (not isinstance(alternatives, list) or len(alternatives) < 2):
                raise ValueError("epistemic reset requires at least two alternatives")
            if not hypothesis:
                raise ValueError("missing hypothesis")
            apply_actions(obj)
        except Exception as exc:
            model = locals().get("model", "none")
            raw = locals().get("raw", f"EXECUTOR_FAILURE: {type(exc).__name__}: {exc}")
            decision = "REJECT"
            observation = f"Executor protocol failure: {type(exc).__name__}: {exc}"
            evidence_id = f"iter-{iteration}-executor-protocol"
            state["status"] = "REJECTED"
            state["latest_observation"] = {"evidence_id": evidence_id, "kind": "executor_protocol_failure", "text": observation[-6000:]}
            write_evidence(iteration, model, raw, observation, decision)
            save_state(state)
            update_heartbeat(state, model, decision)
            persist(iteration, decision)
            time.sleep(SLEEP_SECONDS)
            continue

        rc, test_output = run_tests()
        observation = test_output
        evidence_id = f"iter-{iteration}-protected-tests"
        if rc == 0:
            decision = "ACCEPT"
            state["status"] = "ACCEPTED"
            state["force_epistemic_reset"] = False
            state["accepted_checkpoint"] = evidence_id
            state.setdefault("verified_facts", []).append(
                {
                    "fact_id": f"F-tests-{iteration}",
                    "claim": "All frozen prospective acceptance tests pass",
                    "evidence_id": evidence_id,
                }
            )
        else:
            decision = "REJECT"
            state["status"] = "REJECTED"
            key = normalize_hypothesis(hypothesis)
            counts = state.setdefault("rejection_counts", {})
            counts[key] = int(counts.get(key, 0)) + 1
            if counts[key] >= 2:
                prior = [x for x in state.setdefault("falsified_hypotheses", []) if x.get("key") == key]
                if not prior:
                    state["falsified_hypotheses"].append(
                        {
                            "hypothesis_id": hypothesis_id,
                            "hypothesis": hypothesis,
                            "key": key,
                            "status": "FALSIFIED_HYPOTHESIS",
                            "evidence_ids": [evidence_id],
                        }
                    )
                else:
                    prior[0].setdefault("evidence_ids", []).append(evidence_id)
                state["force_epistemic_reset"] = True
            else:
                state["force_epistemic_reset"] = False

        state["latest_observation"] = {
            "evidence_id": evidence_id,
            "kind": "protected_test_result",
            "decision": decision,
            "text": test_output[-6000:],
        }
        write_evidence(iteration, model, raw, test_output, decision)
        save_state(state)
        update_heartbeat(state, model, decision)
        persist(iteration, decision)

        print(f"HEARTBEAT iteration={iteration} model={model} decision={decision}", flush=True)
        if decision == "ACCEPT":
            return 0
        time.sleep(SLEEP_SECONDS)

    state["status"] = "ITERATION_BUDGET_EXHAUSTED"
    save_state(state)
    update_heartbeat(state, "none", "INCONCLUSIVE")
    persist(state["iteration"], "INCONCLUSIVE")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
