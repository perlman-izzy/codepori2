# Isolated Cloud Bootstrap Objective

This branch is a disposable prospective prototype. Never modify, merge into, or depend on existing project code outside `validated_agent_cloud/`.

Build a small, testable core for a validated long-horizon coding agent with these semantics:

1. Durable epistemic state distinguishes `VERIFIED_FACT`, `HYPOTHESIS`, `FALSIFIED_HYPOTHESIS`, and `UNKNOWN`.
2. Executor output is evidence, never authority.
3. Independent verification returns exactly `ACCEPT`, `REJECT`, or `INCONCLUSIVE`.
4. Only `ACCEPT` may advance authoritative completion/checkpoint state.
5. Repeated rejection is classified. After two rejected attempts relying on the same causal hypothesis, ordinary patch retry is forbidden: mark that hypothesis falsified/unsupported, produce an epistemic-reset payload, and continue from verified facts + falsified hypotheses rather than prior narrative.
6. Reset payloads must exclude executor narrative/reasoning summaries from authoritative context.
7. Immutable evidence records bind IDs, content hashes, and provenance sufficiently to detect artifact substitution.
8. Historical rejected/inconclusive results remain preserved.

The protected tests under `validated_agent_cloud/frozen_tests/` are prospective acceptance tests and may not be edited by the autonomous executor. The executor may write only under `validated_agent_cloud/workspace/`.

Work until all protected tests pass. Do not weaken tests or alter success criteria.
