"""Scores a single investigation run against ground truth: exact match on
root_cause_label and component, plus classifying *why* a run failed when
it did (timeout, bad JSON, wrong answer, etc.) for the failure-mode tally.
"""
import re

_FAILURE_PATTERNS = [
    (re.compile(r"wallclock_timeout_s"), "wallclock_timeout"),
    (re.compile(r"max_steps"), "max_steps_exceeded"),
    (re.compile(r"max_tokens"), "max_tokens_exceeded"),
    (re.compile(r"schema validation"), "schema_validation_failed"),
    (re.compile(r"could not produce valid JSON|did not produce valid JSON"), "invalid_json_output"),
    (re.compile(r"LLM provider error"), "llm_provider_error"),
    (re.compile(r"investigation crashed"), "investigation_crashed"),
    (re.compile(r"no active record|no such"), "harness_error"),
]


def classify_failure_mode(result: dict, exact_match: bool) -> str:
    status = result.get("status")
    hypothesis = result.get("hypothesis") or ""

    if status == "diagnosed" and exact_match:
        return "correct"
    if status == "diagnosed" and not exact_match:
        return "wrong_root_cause_or_component"

    # status == "inconclusive": classify by why, parsed from the hypothesis
    # text that inconclusive_result()/orchestrator always populate with the
    # specific reason (see agent/schema.py's inconclusive_result).
    for pattern, label in _FAILURE_PATTERNS:
        if pattern.search(hypothesis):
            return label
    return "inconclusive_other"


def score_run(
    scenario: dict,
    result: dict,
    tool_call_count: int,
    prompt_tokens: int,
    completion_tokens: int,
    elapsed_s: float,
) -> dict:
    correct_root_cause = result.get("root_cause_label") == scenario["root_cause_label"]
    correct_component = result.get("component") == scenario["component"]
    exact_match = correct_root_cause and correct_component

    return {
        "scenario": scenario["name"],
        "exact_match": exact_match,
        "correct_root_cause_label": correct_root_cause,
        "correct_component": correct_component,
        "predicted_root_cause_label": result.get("root_cause_label"),
        "predicted_component": result.get("component"),
        "status": result.get("status"),
        "failure_mode": classify_failure_mode(result, exact_match),
        "tool_call_count": tool_call_count,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "elapsed_s": round(elapsed_s, 2),
    }


def aggregate(runs: list[dict]) -> dict:
    """Aggregate a list of score_run() dicts (all from the same
    pipeline - agent or baseline) into summary stats."""
    if not runs:
        return {"run_count": 0}

    n = len(runs)
    exact_matches = sum(r["exact_match"] for r in runs)
    inconclusive = sum(r["status"] == "inconclusive" for r in runs)

    failure_tally: dict[str, int] = {}
    for r in runs:
        failure_tally[r["failure_mode"]] = failure_tally.get(r["failure_mode"], 0) + 1

    by_scenario: dict[str, dict] = {}
    for r in runs:
        s = by_scenario.setdefault(r["scenario"], {"run_count": 0, "exact_matches": 0})
        s["run_count"] += 1
        s["exact_matches"] += int(r["exact_match"])
    for s in by_scenario.values():
        s["accuracy"] = round(s["exact_matches"] / s["run_count"], 3)

    return {
        "run_count": n,
        "accuracy": round(exact_matches / n, 3),
        "inconclusive_rate": round(inconclusive / n, 3),
        "avg_tool_call_count": round(sum(r["tool_call_count"] for r in runs) / n, 2),
        "avg_prompt_tokens": round(sum(r["prompt_tokens"] for r in runs) / n, 1),
        "avg_completion_tokens": round(sum(r["completion_tokens"] for r in runs) / n, 1),
        "avg_total_tokens": round(sum(r["total_tokens"] for r in runs) / n, 1),
        "avg_elapsed_s": round(sum(r["elapsed_s"] for r in runs) / n, 2),
        "failure_mode_tally": failure_tally,
        "accuracy_by_scenario": by_scenario,
    }
