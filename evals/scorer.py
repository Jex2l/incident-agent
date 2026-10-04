"""Scores a single investigation run against ground truth: exact match on
root_cause_label and component, plus classifying *why* a run failed when
it did (timeout, bad JSON, wrong answer, etc.) for the failure-mode tally.
"""
import re

# Matched in order against the hypothesis text of "inconclusive" results.
# These strings are not arbitrary - they're substrings that
# agent/schema.py's inconclusive_result() and the orchestrator's own error
# paths always embed in the hypothesis when they give up for a specific
# reason, so this regex list is effectively parsing a semi-structured
# reason code back out of free text rather than guessing at prose.
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
    """Categorize *why* a run did or didn't match ground truth, for the
    failure-mode tally in the eval report. This is a secondary, finer-grained
    view on top of score_run()'s binary exact_match: exact_match answers
    "was it right", classify_failure_mode() answers "if not, what kind of
    wrong" (confidently wrong answer vs. timed out vs. bad JSON vs. gave up
    as inconclusive for an unrecognized reason).

    Args:
        result: the pipeline's output dict (status, hypothesis, etc.).
        exact_match: the exact_match verdict already computed by
            score_run() for this same result, passed in rather than
            recomputed here.

    Returns:
        one of: "correct", "wrong_root_cause_or_component", one of the
        labels in _FAILURE_PATTERNS (e.g. "wallclock_timeout",
        "schema_validation_failed"), or "inconclusive_other" as a catch-all.
    """
    status = result.get("status")
    hypothesis = result.get("hypothesis") or ""

    if status == "diagnosed" and exact_match:
        return "correct"
    if status == "diagnosed" and not exact_match:
        # The model committed to an answer (status "diagnosed") but got the
        # label and/or component wrong - distinct from the patterns below,
        # which only apply to "inconclusive" results where the pipeline
        # itself gave up rather than guessing wrong.
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
    """Score one investigation's output against one scenario's ground
    truth. This is the strict metric used in the real eval report: exact
    string match on both root_cause_label AND component, no partial
    credit for getting one of the two right or for a hypothesis that's
    "close" in prose. Also bundles in the run's cost metrics (tool calls,
    tokens, wall-clock time) so a single record carries everything
    aggregate() needs later.

    Args:
        scenario: the ground-truth entry from scenarios.yaml (must have
            "name", "root_cause_label", "component").
        result: the pipeline's output dict (root_cause_label, component,
            status, hypothesis, ...).
        tool_call_count: number of tool calls the pipeline made for this run.
        prompt_tokens: total prompt tokens consumed across the run's LLM call(s).
        completion_tokens: total completion tokens consumed.
        elapsed_s: wall-clock seconds for the run.

    Returns:
        a flat dict combining the exact-match verdict, its failure-mode
        classification, the predicted labels, and the cost metrics passed
        in - this is the per-run record both build_report() and
        aggregate() consume.
    """
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
