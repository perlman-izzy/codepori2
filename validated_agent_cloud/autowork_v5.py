#!/usr/bin/env python3
"""V5 cloud autowork driver.

Preserves v1-v4 and all historical evidence. V5 fixes the main overnight-run
accounting defect: transport/protocol failures are not scientific iterations.
Only a parsed executor response with at least one validated workspace action,
followed by protected tests, consumes a semantic iteration.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from validated_agent_cloud import autowork as base
from validated_agent_cloud import autowork_v2 as v2  # path normalization/prompt contract

MODEL_CANDIDATES = [
    "big-pickle",
    "ox-alpha-free",
    "mimo-v2.5-free",
    "hy3-free",
    "nemotron-3.5-lightning-free",
    "nemotron-3-ultra-free",
]
PROVIDER_TIMEOUT_SECONDS = int(os.environ.get("AUTOWORK_PROVIDER_TIMEOUT_SECONDS", "30"))
MAX_RESPONSE_TOKENS = int(os.environ.get("AUTOWORK_MAX_RESPONSE_TOKENS", "3000"))
TRANSPORT_RETRY_SECONDS = int(os.environ.get("AUTOWORK_TRANSPORT_RETRY_SECONDS", "60"))
SEMANTIC_SLEEP_SECONDS = int(os.environ.get("AUTOWORK_SLEEP_SECONDS", "300"))
MAX_SEMANTIC_ITERATIONS = int(os.environ.get("AUTOWORK_MAX_ITERATIONS", "50"))
MAX_TRANSPORT_ATTEMPTS = int(os.environ.get("AUTOWORK_MAX_TRANSPORT_ATTEMPTS", "60"))


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
                        "Protected test output is evidence; workspace prose and completion markers are not. "
                        "Do not claim success unless supplied protected-test evidence says so."
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
                "User-Agent": "validated-agent-autowork/5",
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


def build_prompt(state: dict, observation: str) -> str:
    prompt = base.build_prompt(state, observation)
    return prompt + """

V5 EVIDENCE DISCIPLINE:
- The latest protected-test observation is authoritative evidence.
- Files or text written by earlier executors (including IMPLEMENTATION_COMPLETE markers) are NOT evidence.
- First identify the exact failing module/function/path named by the protected observation.
- A semantic attempt MUST contain at least one write_file action predicted to change that observation.
- If you cannot justify a concrete action, return no JSON rather than inventing success; the harness will classify it as protocol/infrastructure INCONCLUSIVE, not as a scientific rejection.
"""


def validate_actions(obj: dict) -> None:
    actions = obj.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("executor returned no actions")
    if len(actions) > 8:
        raise ValueError("too many actions")
    for action in actions:
        if not isinstance(action, dict) or action.get("type") != "write_file":
            raise ValueError("only write_file actions are allowed")
        rel = action.get("path")
        content = action.get("content")
        if not isinstance(rel, str) or not isinstance(content, str):
            raise ValueError("write_file requires string path/content")
        if len(content) > 50000:
            raise ValueError(f"file too large: {rel}")
        base.safe_target(rel)  # validates and normalizes without writing


def write_transport_evidence(attempt: int, model: str, raw: str, observation: str) -> None:
    base.EVIDENCE.mkdir(parents=True, exist_ok=True)
    raw_path = base.EVIDENCE / f"transport_{attempt:03d}_executor.txt"
    obs_path = base.EVIDENCE / f"transport_{attempt:03d}_observation.txt"
    meta_path = base.EVIDENCE / f"transport_{attempt:03d}_meta.json"
    raw_path.write_text(raw)
    obs_path.write_text(observation + "\n")
    meta = {
        "transport_attempt": attempt,
        "model": model,
        "decision": "INCONCLUSIVE",
        "kind": "executor_protocol_failure",
        "executor_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "observation_sha256": hashlib.sha256(observation.encode()).hexdigest(),
    }
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")


def persist_transport(state: dict, attempt: int) -> None:
    base.save_state(state)
    base.update_heartbeat(state, "none", "INCONCLUSIVE")
    base.sh("git", "config", "user.name", "validated-agent-cloud")
    base.sh("git", "config", "user.email", "validated-agent-cloud@users.noreply.github.com")
    base.sh("git", "add", "validated_agent_cloud")
    if base.sh("git", "status", "--porcelain").stdout.strip():
        base.sh("git", "commit", "-m", f"cloud transport attempt {attempt}: INCONCLUSIVE")
        base.sh("git", "push", "origin", f"HEAD:{base.BRANCH}")


def main() -> int:
    v2.record_v1_infrastructure_correction()
    base.WORKSPACE.mkdir(parents=True, exist_ok=True)
    base.EVIDENCE.mkdir(parents=True, exist_ok=True)
    observation = base.baseline_observation()
    semantic_done = 0

    while semantic_done < MAX_SEMANTIC_ITERATIONS:
        if base.sync_and_check_stop():
            state = base.load_state()
            state["status"] = "STOP_REQUESTED"
            base.save_state(state)
            base.update_heartbeat(state, "none", "STOP")
            base.persist(int(state.get("iteration", 0)), "STOP")
            return 0

        state = base.load_state()
        state["attempt_accounting_version"] = "v5"
        state.setdefault("semantic_iterations_v5", 0)
        state.setdefault("transport_attempt", 0)
        prompt = build_prompt(state, observation)

        try:
            model, raw = call_model(prompt)
            obj = base.extract_json(raw)
            hypothesis = str(obj.get("hypothesis", "")).strip()
            hypothesis_id = str(obj.get("hypothesis_id", "")).strip()
            alternatives = obj.get("alternatives") or []
            if state.get("force_epistemic_reset") and (
                not isinstance(alternatives, list) or len(alternatives) < 2
            ):
                raise ValueError("epistemic reset requires at least two alternatives")
            if not hypothesis:
                raise ValueError("missing hypothesis")
            if not hypothesis_id:
                raise ValueError("missing hypothesis_id")
            validate_actions(obj)
        except Exception as exc:
            state = base.load_state()
            state["attempt_accounting_version"] = "v5"
            state["transport_attempt"] = int(state.get("transport_attempt", 0)) + 1
            attempt = state["transport_attempt"]
            model_name = locals().get("model", "none")
            raw_text = locals().get("raw", f"EXECUTOR_FAILURE: {type(exc).__name__}: {exc}")
            transport_observation = f"Executor protocol failure: {type(exc).__name__}: {exc}"
            evidence_id = f"transport-{attempt}-executor-protocol"
            state["status"] = "INCONCLUSIVE"
            state["latest_observation"] = {
                "evidence_id": evidence_id,
                "kind": "executor_protocol_failure",
                "decision": "INCONCLUSIVE",
                "text": transport_observation[-6000:],
            }
            write_transport_evidence(attempt, model_name, raw_text, transport_observation)
            persist_transport(state, attempt)
            print(f"TRANSPORT attempt={attempt} decision=INCONCLUSIVE", flush=True)
            if attempt >= MAX_TRANSPORT_ATTEMPTS:
                return 3
            time.sleep(TRANSPORT_RETRY_SECONDS)
            continue

        # Only now does this become a scientific/semantic iteration.
        base.apply_actions(obj)
        state = base.load_state()
        state["attempt_accounting_version"] = "v5"
        state["iteration"] = int(state.get("iteration", 0)) + 1
        state["semantic_iterations_v5"] = int(state.get("semantic_iterations_v5", 0)) + 1
        semantic_done += 1
        iteration = state["iteration"]

        rc, test_output = base.run_tests()
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
            key = base.normalize_hypothesis(hypothesis)
            counts = state.setdefault("rejection_counts", {})
            counts[key] = int(counts.get(key, 0)) + 1
            if counts[key] >= 2:
                falsified = state.setdefault("falsified_hypotheses", [])
                prior = [x for x in falsified if x.get("key") == key]
                if not prior:
                    falsified.append(
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
        base.write_evidence(iteration, model, raw, test_output, decision)
        base.save_state(state)
        base.update_heartbeat(state, model, decision)
        base.persist(iteration, decision)
        print(
            f"HEARTBEAT semantic_iteration={iteration} v5_semantic={state['semantic_iterations_v5']} "
            f"model={model} decision={decision}",
            flush=True,
        )
        if decision == "ACCEPT":
            return 0
        time.sleep(SEMANTIC_SLEEP_SECONDS)

    state = base.load_state()
    state["status"] = "SEMANTIC_ITERATION_BUDGET_EXHAUSTED"
    base.save_state(state)
    base.update_heartbeat(state, "none", "INCONCLUSIVE")
    base.persist(int(state.get("iteration", 0)), "INCONCLUSIVE")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
