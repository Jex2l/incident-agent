# Failure modes

Honest list of how this agent fails, grounded in real eval runs (`evals/report.md`,
`evals/results.json`) and real fixes applied in response - not a hypothetical list.

## Phase 7 eval report: the starting point

The first full eval run (`evals/report.md`, N=1 per scenario, `qwen2.5:7b`) scored
**0% exact-match accuracy** (strict: `root_cause_label` AND `component` both correct).
That number is real and uncomfortable, but it's also diagnosable - the failure tally
pointed at two concrete, fixable causes, not a vague "the model isn't smart enough":

| Failure mode | Count | What was actually happening |
|---|---|---|
| `wrong_root_cause_or_component` | 4 | Model invented its own label instead of picking a real root cause |
| `schema_validation_failed` | 2 | Malformed output exhausted the bounded retry budget |

## Root cause of the wrong-label failures

Looking at the actual predictions (not just the pass/fail count):

| Scenario | Predicted `root_cause_label` | Predicted `component` | Expected |
|---|---|---|---|
| `oom_kill` | `crash_loop_backoff` | `cart-8646979656-mslrv` (a pod name) | `oom_kill` / `cart` |
| `cpu_throttle` | `high_latency` | `null` | `cpu_throttling` / `cart` |
| `crash_loop` | `force_crash_on_boot` | `cart` | `crash_loop_bad_config` / `cart` |
| `dependency_down` | `bad_deploy` | `cart` | `dependency_down` / `cache` |

Two distinct bugs, both in `agent/prompts.py` and `agent/schema.py`, not in the model's
reasoning:

1. **The prompt only gave examples, not a closed set, for `root_cause_label`.** The
   model was reading signals correctly (e.g. `crash_loop` really was a
   `CrashLoopBackOff` situation) but then labeling it from whatever string was most
   salient in what it just saw - the alert name (`high_latency`, `crash_loop_backoff`)
   or a literal environment variable name it found in evidence
   (`force_crash_on_boot`) - instead of the canonical taxonomy. Nothing in the schema
   stopped this, because any string passes `{"type": "string"}`.
2. **The prompt said "component/deployment name" without saying which one when
   they differ.** For `oom_kill` the model returned the full pod name (with its
   random suffix) instead of the Deployment name. For `dependency_down` - where the
   whole point of the scenario is that the *symptom* shows up on `cart` but the
   *cause* is `cache` - the model stayed anchored on the symptomatic component.

`dependency_down` is also a genuinely harder case on its merits: the alert's own
`root_cause_hint` annotation literally says `"bad_deploy_or_dependency_down"` (Prometheus
can't distinguish these from the alert condition alone - `DeploymentUnavailable` fires
either way). That ambiguity is honest and not something to paper over in the alert
rule; it's exactly what the agent's investigation step is supposed to resolve by
checking whether the *dependency itself* is down, not just restating the alert.

## Fixes applied (Phase 8)

1. **`agent/prompts.py`**: replaced the example-only `root_cause_label` line with an
   explicit closed list of 8 labels (6 specific root causes + 2 symptom-level
   fallbacks for genuinely ambiguous cases), each with a one-line definition, plus an
   explicit instruction not to invent a new label from the alert name or a variable
   name. Added an explicit rule for `component`: always the Deployment name, never a
   pod name, and - for dependency_down specifically - the name of the broken
   dependency, not the component merely showing symptoms.
2. **`agent/schema.py`**: added a matching `enum` constraint (`ROOT_CAUSE_LABELS`) on
   `root_cause_label` in `OUTPUT_SCHEMA`. This isn't just prompt wording now - a label
   outside the list fails `validate_output()` and goes through the orchestrator's
   existing bounded "fix your output" retry with the exact validation error message,
   the same mechanism already used for malformed JSON. Verified directly:
   `root_cause_label: 'crash_loop_backoff'` now produces
   `"'crash_loop_backoff' is not one of ['oom_kill', 'bad_deploy', ...]"` instead of
   silently passing validation.

## Before / after (real re-runs, not projected)

Re-ran the three scenarios most directly affected by the label/component confusion,
through the exact same `evals.runner.run_one()` path the real eval harness uses:

| Scenario | Before (Phase 7 report) | After (Phase 8, same model, same budget) |
|---|---|---|
| `oom_kill` | `crash_loop_backoff` / pod name - **wrong** | `oom_kill` / `cart` - **correct** (confidence 0.85) |
| `cpu_throttle` | `high_latency` / `null` - **wrong** | `cpu_throttling` / `cart` - **correct** (confidence 0.8) |
| `crash_loop` | `force_crash_on_boot` / `cart` - **wrong** | `crash_loop_bad_config` / `cart` - **correct** (confidence 0.8) |

3/3 exact-match on scenarios that were 0/3 before, with no other code path changed -
same model (`qwen2.5:7b`), same tools, same investigation-method prompt structure,
same budgets. `dependency_down` and `bad_deploy` were not re-verified in this pass
(the harness's own multi-hour cost per full run, documented in `docs/DECISIONS.md`
Phase 7, makes re-running all 6 impractical in one sitting) - re-running the full
6-scenario eval with this fix is the natural next data point, left for a future
`make eval` run rather than asserted here without having actually run it.

## Known failure modes still present

- **`schema_validation_failed` / `invalid_json_output`**: the bounded retry (2
  attempts) sometimes still isn't enough - a small 7B model occasionally adds an
  unexpected field (e.g. `likely_causes`) or gets an enum value wrong twice in a row.
  The harness correctly falls back to `inconclusive` rather than scoring a malformed
  guess as correct, which is the right failure mode to have, but it does cost
  accuracy. Not fixed in this pass - a candidate for Phase 9+ iteration (e.g. a
  JSON-mode/structured-output constraint if the provider supports one, or a stricter
  single-field reminder on the specific field that failed).
- **`wallclock_timeout` under CPU contention**: documented extensively in
  `docs/DECISIONS.md` across Phases 4, 6, and 7 - this is a hardware constraint (this
  machine cannot run the full kind cluster and local 7B inference at full speed
  simultaneously for extended periods), not a reasoning failure. The harness handles
  it gracefully (saves a real `inconclusive` result with partial evidence, never
  crashes), but it's a real cost to measured accuracy and time-to-diagnosis on this
  specific hardware that would not apply on a machine with more headroom or a smaller
  model.
- **Compound-cause scenarios** (`dependency_down` vs `bad_deploy`, both triggering
  `DeploymentUnavailable`): the alert signal alone is genuinely ambiguous between
  these two root causes by design. The fix for this class of failure is better
  investigation (checking the dependency's own health before concluding), not a
  prompt/schema tweak - worth targeted testing in a future iteration pass.
