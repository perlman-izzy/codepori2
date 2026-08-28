#!/usr/bin/env python3
"""V2 compatibility/hardening wrapper for the preserved v1 autowork driver.

V1 iteration 1 exposed a harness path-contract ambiguity: the executor emitted
`validated_agent_cloud/workspace/core.py` even though v1 expected paths relative
to workspace, so v1 prepended workspace again. This wrapper preserves all v1
code/evidence, records the correction durably, normalizes that harmless prefix,
and prevents the infrastructure-caused rejection from counting as an epistemic
rejection of the executor's causal hypothesis.
"""

from __future__ import annotations

import json
from pathlib import Path

from validated_agent_cloud import autowork as base

CORRECTION_ID = "infra-correction-v2-path-contract-iter1"
PREFIXES = (
    "validated_agent_cloud/workspace/",
    "workspace/",
)


def normalized_safe_target(rel: str) -> Path:
    if not isinstance(rel, str):
        raise ValueError("path must be a string")
    normalized = rel.replace("\\", "/").lstrip("./")
    for prefix in PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    if not normalized:
        raise ValueError(f"unsafe path after normalization: {rel!r}")
    return ORIGINAL_SAFE_TARGET(normalized)


def clarified_build_prompt(state: dict, test_output: str) -> str:
    prompt = ORIGINAL_BUILD_PROMPT(state, test_output)
    return prompt + """

STRICT PATH CONTRACT (harness v2):
Every action `path` is RELATIVE TO `validated_agent_cloud/workspace/`.
Examples: `core.py`, `__init__.py`, `pkg/module.py`.
Do NOT prefix paths with `validated_agent_cloud/workspace/` or `workspace/`.
The harness defensively normalizes either legacy prefix, but new responses must
use relative paths only.
"""


def record_v1_infrastructure_correction() -> None:
    state = base.load_state()
    corrections = state.setdefault("infrastructure_corrections", [])
    if any(c.get("correction_id") == CORRECTION_ID for c in corrections):
        return

    latest = state.get("latest_observation") or {}
    if state.get("iteration") != 1 or latest.get("evidence_id") != "iter-1-protected-tests":
        return

    previous_counts = dict(state.get("rejection_counts") or {})
    correction = {
        "correction_id": CORRECTION_ID,
        "classification": "HARNESS_INTERFACE_FAILURE",
        "preserved_primary_evidence": "iter-1-protected-tests",
        "finding": (
            "Executor identified the missing core module and supplied a plausible implementation, "
            "but v1's ambiguous relative-path contract caused a duplicated workspace prefix."
        ),
        "action": (
            "Preserve iteration-1 REJECT and immutable evidence; remove only its derived causal-"
            "hypothesis retry count and clarify/normalize the path contract in v2."
        ),
        "previous_rejection_counts": previous_counts,
    }
    corrections.append(correction)
    # The historical REJECT remains in state/latest_observation and evidence. Only the
    # derived hypothesis retry accumulator is corrected because the rejection was caused
    # by the harness interface rather than evidence against the stated causal hypothesis.
    state["rejection_counts"] = {}
    state["force_epistemic_reset"] = False
    base.save_state(state)

    evidence_path = base.EVIDENCE / "infrastructure_correction_v2.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(correction, indent=2, sort_keys=True) + "\n")


ORIGINAL_SAFE_TARGET = base.safe_target
ORIGINAL_BUILD_PROMPT = base.build_prompt
base.safe_target = normalized_safe_target
base.build_prompt = clarified_build_prompt


if __name__ == "__main__":
    record_v1_infrastructure_correction()
    raise SystemExit(base.main())
