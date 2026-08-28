import hashlib
from typing import Dict, Any, List, Optional

# Verifier decision constants
ACCEPT = "ACCEPT"
REJECT = "REJECT"
INCONCLUSIVE = "INCONCLUSIVE"

# Status constants
VERIFIED_FACT = "VERIFIED_FACT"
HYPOTHESIS_STATUS = "HYPOTHESIS"
UNKNOWN_STATUS = "UNKNOWN"
FALSIFIED_HYPOTHESIS = "FALSIFIED_HYPOTHESIS"


def new_state(job_id: str, objective: str) -> Dict[str, Any]:
    """Create a new epistemic state for a job."""
    return {
        "job_id": job_id,
        "objective": objective,
        "verified_facts": {},
        "hypotheses": {},
        "unknowns": {},
        "falsified_hypotheses": {},
        "rejection_history": [],
        "verifier_history": [],
        "current_checkpoint": None,
    }


def record_fact(state: Dict[str, Any], fact_id: str, content: str, sha256: str) -> None:
    """Record a verified fact in the state."""
    if not fact_id:
        raise ValueError("fact_id cannot be empty")
    state["verified_facts"][fact_id] = {
        "status": VERIFIED_FACT,
        "content": content,
        "sha256": sha256,
    }


def record_hypothesis(state: Dict[str, Any], hypothesis_id: str, content: str) -> None:
    """Record a hypothesis in the state."""
    if not hypothesis_id:
        raise ValueError("hypothesis_id cannot be empty")
    state["hypotheses"][hypothesis_id] = {
        "status": HYPOTHESIS_STATUS,
        "content": content,
        "rejection_count": 0,
    }


def record_unknown(state: Dict[str, Any], unknown_id: str, content: str) -> None:
    """Record an unknown in the state."""
    if not unknown_id:
        raise ValueError("unknown_id cannot be empty")
    state["unknowns"][unknown_id] = {
        "status": UNKNOWN_STATUS,
        "content": content,
    }


def record_rejection(state: Dict[str, Any], hypothesis_id: str, evidence_id: str) -> Dict[str, Any]:
    """Record rejection of a hypothesis. Returns whether epistemic reset is required."""
    if hypothesis_id not in state["hypotheses"]:
        raise ValueError(f"Hypothesis {hypothesis_id} not found in state")
    
    # Record the rejection in history
    rejection_record = {
        "hypothesis_id": hypothesis_id,
        "evidence_id": evidence_id,
    }
    state["rejection_history"].append(rejection_record)
    
    # Update rejection count
    state["hypotheses"][hypothesis_id]["rejection_count"] += 1
    
    # Check if we need epistemic reset (two rejections of same hypothesis)
    requires_reset = state["hypotheses"][hypothesis_id]["rejection_count"] >= 2
    
    if requires_reset:
        # Move hypothesis to falsified_hypotheses
        hypothesis = state["hypotheses"].pop(hypothesis_id)
        falsified_evidence_ids = []
        for rejection in state["rejection_history"]:
            if rejection["hypothesis_id"] == hypothesis_id:
                falsified_evidence_ids.append(rejection["evidence_id"])
        
        state["falsified_hypotheses"][hypothesis_id] = {
            "status": FALSIFIED_HYPOTHESIS,
            "content": hypothesis["content"],
            "falsified_by": falsified_evidence_ids,
        }
    
    return {"requires_epistemic_reset": requires_reset}


def build_reset_payload(state: Dict[str, Any]) -> Dict[str, Any]:
    """Build a reset payload containing only verified facts and falsified hypotheses."""
    return {
        "verified_facts": state["verified_facts"].copy(),
        "falsified_hypotheses": state["falsified_hypotheses"].copy(),
    }


def apply_verifier_result(state: Dict[str, Any], decision: str, checkpoint_id: str, evidence_id: str) -> None:
    """Apply a verifier result to the state."""
    if decision not in (ACCEPT, REJECT, INCONCLUSIVE):
        raise ValueError(f"Invalid decision: {decision}. Must be ACCEPT, REJECT, or INCONCLUSIVE")
    
    # Record the verifier decision
    state["verifier_history"].append({
        "decision": decision,
        "checkpoint_id": checkpoint_id,
        "evidence_id": evidence_id,
    })
    
    # Only ACCEPT advances checkpoint
    if decision == ACCEPT:
        state["current_checkpoint"] = checkpoint_id


def make_evidence_record(evidence_id: str, content: bytes, provenance: Dict[str, str]) -> Dict[str, Any]:
    """Create an immutable evidence record with content hash and provenance."""
    if not evidence_id:
        raise ValueError("evidence_id cannot be empty")
    if not provenance:
        raise ValueError("provenance cannot be empty")
    
    sha256_hash = hashlib.sha256(content).hexdigest()
    
    return {
        "evidence_id": evidence_id,
        "sha256": sha256_hash,
        "provenance": provenance.copy(),
    }


def verify_evidence_record(record: Dict[str, Any], content: bytes) -> bool:
    """Verify that an evidence record matches the given content."""
    expected_hash = hashlib.sha256(content).hexdigest()
    return record["sha256"] == expected_hash
