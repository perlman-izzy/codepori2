import hashlib
import unittest

from validated_agent_cloud.workspace.core import (
    ACCEPT,
    INCONCLUSIVE,
    REJECT,
    apply_verifier_result,
    build_reset_payload,
    make_evidence_record,
    new_state,
    record_fact,
    record_hypothesis,
    record_rejection,
    record_unknown,
    verify_evidence_record,
)


class DurableEpistemicStateTests(unittest.TestCase):
    def test_state_types_are_separate(self):
        s = new_state("job-1", "fix the repository")
        record_fact(s, "F1", "API returns schema Z", "sha-f1")
        record_hypothesis(s, "H1", "parser causes auth failure")
        record_unknown(s, "U1", "whether cache participates")
        self.assertEqual(s["verified_facts"]["F1"]["status"], "VERIFIED_FACT")
        self.assertEqual(s["hypotheses"]["H1"]["status"], "HYPOTHESIS")
        self.assertEqual(s["unknowns"]["U1"]["status"], "UNKNOWN")
        self.assertEqual(s["falsified_hypotheses"], {})

    def test_two_rejections_force_epistemic_reset_and_preserve_evidence(self):
        s = new_state("job-2", "repair behavior")
        record_hypothesis(s, "H1", "token parser is the causal fault")
        r1 = record_rejection(s, "H1", "E-reject-1")
        self.assertFalse(r1["requires_epistemic_reset"])
        self.assertIn("H1", s["hypotheses"])
        r2 = record_rejection(s, "H1", "E-reject-2")
        self.assertTrue(r2["requires_epistemic_reset"])
        self.assertNotIn("H1", s["hypotheses"])
        f = s["falsified_hypotheses"]["H1"]
        self.assertEqual(f["status"], "FALSIFIED_HYPOTHESIS")
        self.assertEqual(f["falsified_by"], ["E-reject-1", "E-reject-2"])
        self.assertEqual(len(s["rejection_history"]), 2)

    def test_reset_payload_contains_verified_truth_not_executor_narrative(self):
        s = new_state("job-3", "objective")
        s["executor_narrative"] = "previous agent says parser is definitely broken"
        s["reasoning_summary"] = "anchored diagnosis"
        record_fact(s, "F1", "test X fails", "sha")
        record_hypothesis(s, "H1", "wrong cause")
        record_rejection(s, "H1", "E1")
        record_rejection(s, "H1", "E2")
        payload = build_reset_payload(s)
        self.assertEqual(payload["verified_facts"], s["verified_facts"])
        self.assertEqual(payload["falsified_hypotheses"], s["falsified_hypotheses"])
        lowered = " ".join(payload.keys()).lower()
        self.assertNotIn("narrative", lowered)
        self.assertNotIn("reasoning", lowered)
        self.assertNotIn("hypotheses", payload.keys() - {"falsified_hypotheses"})


class VerifierAuthorityTests(unittest.TestCase):
    def test_only_accept_advances_checkpoint(self):
        s = new_state("job-4", "objective")
        apply_verifier_result(s, REJECT, "cp-rejected", "E1")
        self.assertIsNone(s["current_checkpoint"])
        apply_verifier_result(s, INCONCLUSIVE, "cp-unknown", "E2")
        self.assertIsNone(s["current_checkpoint"])
        apply_verifier_result(s, ACCEPT, "cp-accepted", "E3")
        self.assertEqual(s["current_checkpoint"], "cp-accepted")
        self.assertEqual([x["decision"] for x in s["verifier_history"]], [REJECT, INCONCLUSIVE, ACCEPT])

    def test_verifier_decision_vocabulary_is_closed(self):
        s = new_state("job-5", "objective")
        with self.assertRaises(ValueError):
            apply_verifier_result(s, "PASS", "cp", "E")


class ImmutableEvidenceTests(unittest.TestCase):
    def test_content_hash_detects_substitution(self):
        content = b"original immutable evidence"
        r = make_evidence_record("E1", content, {"executor": "dummy", "run_id": "R1"})
        self.assertEqual(r["sha256"], hashlib.sha256(content).hexdigest())
        self.assertTrue(verify_evidence_record(r, content))
        self.assertFalse(verify_evidence_record(r, b"modified immutable evidence"))
        self.assertEqual(r["provenance"]["run_id"], "R1")

    def test_evidence_record_requires_identity_and_provenance(self):
        with self.assertRaises(ValueError):
            make_evidence_record("", b"x", {"run_id": "R1"})
        with self.assertRaises(ValueError):
            make_evidence_record("E1", b"x", {})


if __name__ == "__main__":
    unittest.main()
