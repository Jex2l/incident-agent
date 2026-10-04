# incident-agent eval report

Generated from **6 real runs** (1 per scenario x 6 scenarios). Model under test: `qwen2.5:7b`.

Started: 2026-10-04T05:53:04.406033+00:00  
Completed: 2026-10-04T10:50:39.755210+00:00

## Overall accuracy (exact match: root_cause_label AND component)

| Pipeline | Accuracy | Inconclusive rate | Avg tool calls | Avg tokens | Avg time to diagnosis |
|---|---|---|---|---|---|
| Full agent | 0% | 33% | 5.67 | 12815 | 240.1s |
| Baseline (fixed pipeline, 1 LLM call) | 0% | 83% | 5.0 | 2898 | 125.2s |

## Accuracy by scenario (full agent)

| Scenario | Runs | Accuracy |
|---|---|---|
| oom_kill | 1 | 0% |
| bad_deploy | 1 | 0% |
| cpu_throttle | 1 | 0% |
| kafka_lag | 1 | 0% |
| crash_loop | 1 | 0% |
| dependency_down | 1 | 0% |

## Failure mode tally (full agent)

| Failure mode | Count |
|---|---|
| wrong_root_cause_or_component | 4 |
| schema_validation_failed | 2 |

## Token usage and reference cost

**Actual cost: $0** - the model under test runs locally via Ollama; no API calls, no spend.

Total tokens across all 6 agent runs: 76,890 (72,162 prompt + 4,728 completion). Average per incident: 12815 tokens.

**Reference figure only, not actual spend**: at illustrative pricing of $0.0005/1K prompt tokens and $0.0015/1K completion tokens (roughly a small hosted-model tier), this token volume would cost **$0.0432** across all runs, or **$0.00720 per incident**.

## LLM-judge explanation quality

Not run for this report (`EVAL_LLM_JUDGE=false`) - see docs/DECISIONS.md for why.
