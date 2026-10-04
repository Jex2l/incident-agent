"""JSON schema for the agent's final investigation output, and a validator
with readable error messages (used both to check the model's output and to
drive the bounded "fix your output" retry loop in orchestrator.py).
"""
import jsonschema

OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["hypothesis", "root_cause_label", "component", "confidence", "evidence", "status"],
    "properties": {
        "hypothesis": {"type": "string", "minLength": 1},
        "root_cause_label": {"type": ["string", "null"]},
        "component": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
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
    "additionalProperties": False,
}

_validator = jsonschema.Draft7Validator(OUTPUT_SCHEMA)


def validate_output(obj: dict) -> tuple[bool, list[str]]:
    errors = sorted(_validator.iter_errors(obj), key=lambda e: e.path)
    if not errors:
        return True, []
    messages = [f"{'.'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]
    return False, messages


def inconclusive_result(reason: str, evidence: list[dict]) -> dict:
    return {
        "hypothesis": f"Investigation inconclusive: {reason}",
        "root_cause_label": None,
        "component": None,
        "confidence": 0.0,
        "evidence": evidence,
        "proposed_action": None,
        "status": "inconclusive",
    }
