"""JSON schema for the agent's final investigation output, and a validator
with readable error messages (used both to check the model's output and to
drive the bounded "fix your output" retry loop in orchestrator.py).
"""
import jsonschema

# Phase 8: the eval report showed the model inventing labels straight from
# the alert name or a variable name it saw in evidence (e.g. "crash_loop_backoff",
# "high_latency", "force_crash_on_boot") instead of picking a real root cause -
# every one of those was schema-valid (just a string) so nothing caught it. A
# closed enum, matching evals/scenarios.yaml's ground truth, means a wrong
# label fails validation and goes through the bounded "fix your output" retry
# instead of silently scoring wrong. Extend this list if new fault types are
# added to evals/scenarios.yaml.
ROOT_CAUSE_LABELS = [
    "oom_kill",
    "bad_deploy",
    "cpu_throttling",
    "kafka_consumer_lag",
    "crash_loop_bad_config",
    "dependency_down",
    "high_error_rate",
    "high_latency",
]

# The schema the model's final JSON answer must satisfy. Enforced via
# jsonschema (not just "ask nicely in the prompt") so a malformed or
# out-of-taxonomy answer is mechanically caught and sent back through the
# orchestrator's bounded retry loop instead of silently shipping bad data.
OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["hypothesis", "root_cause_label", "component", "confidence", "evidence", "status"],
    "properties": {
        "hypothesis": {"type": "string", "minLength": 1},
        # Must be one of the closed ROOT_CAUSE_LABELS, or null for an
        # inconclusive result - never an arbitrary string (see the Phase 8
        # comment above for why this is an enum and not just "string").
        "root_cause_label": {"enum": ROOT_CAUSE_LABELS + [None]},
        # The Deployment name (e.g. "cart"), not a pod name - pod names are
        # unstable (include a random suffix) and useless for a human
        # reading the hypothesis later. Enforced via prompt instruction,
        # not schema, since a Deployment name is just an arbitrary string.
        "component": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        # Every claim in the hypothesis should be traceable to a specific
        # tool call result - this array is what the replay UI renders as
        # the clickable evidence trail.
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["tool", "query", "summary"],
                "properties": {
                    "tool": {"type": "string"},
                    "query": {},
                    "summary": {"type": "string"},
                },
            },
        },
        # Optional: present only when the model has a concrete, allowlisted
        # remediation to suggest. Null proposed_action means "diagnosed but
        # no proposed fix" - distinct from status="inconclusive", which
        # means "couldn't diagnose at all". This is also the only field
        # evals/runner.py and executor/store.py look at to decide whether an
        # investigation becomes a pending approval proposal.
        "proposed_action": {
            "type": ["object", "null"],
            "properties": {
                "action": {"type": "string"},
                "target": {"type": "string"},
                "params": {"type": "object"},
            },
        },
        "status": {"type": "string", "enum": ["diagnosed", "inconclusive"]},
    },
    # Rejects any key the model invents beyond this schema - keeps the
    # output shape stable for downstream parsers (replay UI, eval scorer).
    "additionalProperties": False,
}

_validator = jsonschema.Draft7Validator(OUTPUT_SCHEMA)


def validate_output(obj: dict) -> tuple[bool, list[str]]:
    """Validate a candidate final-answer dict against OUTPUT_SCHEMA.

    Returns (True, []) if valid, or (False, [readable error messages]) -
    the orchestrator feeds those messages straight back to the model as the
    reason its last answer was rejected, so it has a concrete instruction
    for what to fix on retry.
    """
    errors = sorted(_validator.iter_errors(obj), key=lambda e: e.path)
    if not errors:
        return True, []
    messages = [f"{'.'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]
    return False, messages


def inconclusive_result(reason: str, evidence: list[dict]) -> dict:
    """Builds a schema-valid "gave up" result. Used whenever the
    orchestrator hits a budget limit, a tool/LLM failure it can't recover
    from, or exhausts its format-retry attempts - the system is designed to
    always produce a valid, honest "I don't know" rather than ever fabricate
    a hypothesis to avoid admitting failure.
    """
    return {
        "hypothesis": f"Investigation inconclusive: {reason}",
        "root_cause_label": None,
        "component": None,
        "confidence": 0.0,
        "evidence": evidence,
        "proposed_action": None,
        "status": "inconclusive",
    }
