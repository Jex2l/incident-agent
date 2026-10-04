"""Tests for evals/scorer.py: exact-match scoring and failure-mode
classification. No live cluster or LLM needed."""
from evals.scorer import aggregate, classify_failure_mode, score_run

SCENARIO = {"name": "oom_kill", "root_cause_label": "oom_kill", "component": "cart"}


def test_exact_match_when_both_fields_correct():
    result = {"root_cause_label": "oom_kill", "component": "cart", "status": "diagnosed", "hypothesis": "ok"}
    scored = score_run(SCENARIO, result, tool_call_count=3, prompt_tokens=100, completion_tokens=50, elapsed_s=12.0)
    assert scored["exact_match"] is True
    assert scored["failure_mode"] == "correct"


def test_not_exact_match_when_root_cause_wrong():
    result = {"root_cause_label": "bad_deploy", "component": "cart", "status": "diagnosed", "hypothesis": "ok"}
    scored = score_run(SCENARIO, result, tool_call_count=3, prompt_tokens=100, completion_tokens=50, elapsed_s=12.0)
    assert scored["exact_match"] is False
    assert scored["correct_component"] is True
    assert scored["correct_root_cause_label"] is False
    assert scored["failure_mode"] == "wrong_root_cause_or_component"


def test_not_exact_match_when_component_wrong():
    result = {"root_cause_label": "oom_kill", "component": "frontend", "status": "diagnosed", "hypothesis": "ok"}
    scored = score_run(SCENARIO, result, tool_call_count=3, prompt_tokens=100, completion_tokens=50, elapsed_s=12.0)
    assert scored["exact_match"] is False
    assert scored["correct_root_cause_label"] is True
    assert scored["correct_component"] is False


def test_near_miss_label_does_not_count_as_match():
    """A compound/related label like 'dependency_down_or_bad_deploy' is NOT
    an exact match, even if semantically close - scoring must stay strict."""
    result = {"root_cause_label": "dependency_down_or_bad_deploy", "component": "cart", "status": "diagnosed", "hypothesis": "ok"}
    scored = score_run(SCENARIO, result, tool_call_count=3, prompt_tokens=100, completion_tokens=50, elapsed_s=12.0)
    assert scored["exact_match"] is False


def test_classify_failure_mode_timeout():
    result = {"status": "inconclusive", "hypothesis": "Investigation inconclusive: wallclock_timeout_s (480.0) reached"}
    assert classify_failure_mode(result, exact_match=False) == "wallclock_timeout"


def test_classify_failure_mode_schema_validation():
    result = {"status": "inconclusive", "hypothesis": "Investigation inconclusive: model's output failed schema validation: [...]"}
    assert classify_failure_mode(result, exact_match=False) == "schema_validation_failed"


def test_classify_failure_mode_invalid_json():
    result = {"status": "inconclusive", "hypothesis": "Investigation inconclusive: model could not produce valid JSON output after retries"}
    assert classify_failure_mode(result, exact_match=False) == "invalid_json_output"


def test_classify_failure_mode_llm_error():
    result = {"status": "inconclusive", "hypothesis": "Investigation inconclusive: LLM provider error after retries: TimeoutError: ..."}
    assert classify_failure_mode(result, exact_match=False) == "llm_provider_error"


def test_classify_failure_mode_crash():
    result = {"status": "inconclusive", "hypothesis": "Investigation inconclusive: investigation crashed: ValueError: boom"}
    assert classify_failure_mode(result, exact_match=False) == "investigation_crashed"


def test_classify_failure_mode_unrecognized_inconclusive_reason():
    result = {"status": "inconclusive", "hypothesis": "Investigation inconclusive: something we've never seen before"}
    assert classify_failure_mode(result, exact_match=False) == "inconclusive_other"


def test_classify_failure_mode_diagnosed_but_wrong():
    result = {"status": "diagnosed", "hypothesis": "confidently wrong"}
    assert classify_failure_mode(result, exact_match=False) == "wrong_root_cause_or_component"


def test_aggregate_empty_list():
    agg = aggregate([])
    assert agg["run_count"] == 0


def test_aggregate_computes_accuracy_and_rates():
    runs = [
        score_run(SCENARIO, {"root_cause_label": "oom_kill", "component": "cart", "status": "diagnosed", "hypothesis": "x"},
                  tool_call_count=2, prompt_tokens=100, completion_tokens=20, elapsed_s=10.0),
        score_run(SCENARIO, {"root_cause_label": "bad_deploy", "component": "cart", "status": "diagnosed", "hypothesis": "x"},
                  tool_call_count=4, prompt_tokens=200, completion_tokens=40, elapsed_s=20.0),
        score_run(SCENARIO, {"root_cause_label": None, "component": None, "status": "inconclusive", "hypothesis": "Investigation inconclusive: wallclock_timeout_s (480.0) reached"},
                  tool_call_count=6, prompt_tokens=300, completion_tokens=0, elapsed_s=480.0),
    ]
    agg = aggregate(runs)
    assert agg["run_count"] == 3
    assert agg["accuracy"] == round(1 / 3, 3)
    assert agg["inconclusive_rate"] == round(1 / 3, 3)
    assert agg["avg_tool_call_count"] == 4.0
    assert agg["failure_mode_tally"]["correct"] == 1
    assert agg["failure_mode_tally"]["wrong_root_cause_or_component"] == 1
    assert agg["failure_mode_tally"]["wallclock_timeout"] == 1


def test_aggregate_accuracy_by_scenario():
    other_scenario = {"name": "kafka_lag", "root_cause_label": "kafka_consumer_lag", "component": "kafka-consumer"}
    runs = [
        score_run(SCENARIO, {"root_cause_label": "oom_kill", "component": "cart", "status": "diagnosed", "hypothesis": "x"},
                  tool_call_count=2, prompt_tokens=100, completion_tokens=20, elapsed_s=10.0),
        score_run(other_scenario, {"root_cause_label": "x", "component": "y", "status": "diagnosed", "hypothesis": "x"},
                  tool_call_count=2, prompt_tokens=100, completion_tokens=20, elapsed_s=10.0),
    ]
    agg = aggregate(runs)
    assert agg["accuracy_by_scenario"]["oom_kill"]["accuracy"] == 1.0
    assert agg["accuracy_by_scenario"]["kafka_lag"]["accuracy"] == 0.0
