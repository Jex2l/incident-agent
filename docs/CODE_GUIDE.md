# Code guide

This is a code-level companion to [README.md](../README.md) (the pitch and quickstart),
[docs/customer-deployment.md](customer-deployment.md) (how to adapt this for a real
environment), and [docs/HANDOFF.md](HANDOFF.md) (full project history and decisions). This
document walks through **what's actually in the code**: every module, what it does, how
data flows between modules, and where to look for the non-obvious parts. Every source
file referenced here also has inline docstrings/comments — this doc is the map, the
comments in each file are the detail.

Read this top to bottom for a first orientation, or jump to the section for the
directory you're editing.

---

## 1. The big picture: one request's journey through the system

```
1. faults/<scenario>.py inject()      — breaks the sample app for real
2. target-app/*/app.py                — the broken service starts failing/erroring
3. Prometheus scrapes + evaluates     — infra/alert-rules/alert-rules.yaml fires an alert
4. Alertmanager                       — routes the firing alert to two webhook receivers
5. infra/webhook-receiver/app.py      — logs it (used by faults/lib.py to confirm the fire)
   agent/webhook.py                   — the one that actually matters: kicks off an investigation
6. agent/orchestrator.py investigate()
     <-> agent/llm.py (OllamaProvider.chat)
     <-> agent/mcp_client.py (ToolSet.call)
           -> tools/prometheus_query.py / log_search.py / kube_read.py / runbook_search.py
     -> writes evals/runs/<incident_id>.json   (the investigation record)
     -> writes traces/<incident_id>.json       (the full OTel-adjacent trace for replay)
     -> appends traces/spans.jsonl             (every LLM/tool call as an OTel span)
7. executor/store.py sync_from_incidents()
     — scans evals/runs/ for a non-null proposed_action, creates a pending Proposal
8. ui/app.py                          — human sees the proposal, clicks approve/reject
9. executor/app.py approve_proposal() -> executor/allowlist.py execute_action()
     — re-validated against the allowlist, THEN a real kubectl command runs
10. executor/store.py append_audit()  — one more append-only JSONL line, forever
```

Two threads run independently of all this and feed the eval story:

- `evals/runner.py` (`make eval`) drives the same inject -> alert -> investigate -> reset
  cycle per `evals/scenarios.yaml` entry, but also runs `evals/baseline.py`'s fixed
  5-tool-call comparison pipeline, scores both with `evals/scorer.py`, and writes
  `evals/report.md` / `evals/results.json`.
- `agent/tracing.py` instruments steps 6 above regardless of which path triggered them
  (live webhook or eval run) — `traces/viewer.html` and `replay/index.html` both read
  from the same trace format.

---

## 2. `agent/` — the investigation engine

This is the only package that talks to the LLM and to the MCP tool servers. It never
writes to the cluster.

| File | Role |
|---|---|
| `llm.py` | `LLMProvider` ABC + `OllamaProvider` implementation. Normalizes Ollama's `/api/chat` response into a provider-agnostic `ChatResponse`. Swap in a different free backend by adding a new `LLMProvider` subclass and a branch in `get_default_provider()` — nothing else changes. |
| `mcp_client.py` | `ToolSet`: spawns the 4 `tools/*.py` servers as subprocesses (`python -m tools.<name>`), speaks real MCP over stdio to each, and exposes one `call(tool_name, args)` that routes to the right subprocess. The agent never imports `tools/*.py` directly — this is deliberate, so the investigation exercises the same protocol path an external MCP client would. |
| `budgets.py` | `Budget` (config: max steps / max tokens / wallclock timeout, all env-var overridable) and `BudgetTracker` (the live counters). `BudgetTracker.exceeded()` is checked at the top of every orchestrator loop iteration. |
| `schema.py` | `OUTPUT_SCHEMA` (JSON Schema, Draft 7) the model's final answer must satisfy, `ROOT_CAUSE_LABELS` (the closed taxonomy — see §7's Phase 8 note), `validate_output()`, and `inconclusive_result()` (the single factory for every "gave up honestly" result). |
| `prompts.py` | `SYSTEM_PROMPT` — the entire investigation method lives here as one string: ordered steps (alert → blast radius → metrics → logs → events → runbooks → hypothesis → verify), the exact output JSON shape, and the closed `root_cause_label` list. There's no separate planner or decision tree in code; this prompt *is* the method. |
| `orchestrator.py` | `investigate()` — the actual loop. Alternates LLM turn → (tool calls, loop) or (final JSON, validate, maybe retry, return). Bounded retries for bad JSON/schema failures (`MAX_FORMAT_RETRIES=2`) and LLM provider errors (`MAX_LLM_ERROR_RETRIES=2`), independent of the overall `Budget`. Saves a full trace via `_save_trace()` no matter how the investigation ends. |
| `webhook.py` | FastAPI app (`make agent`, port 8010). `lifespan()` starts a Loki port-forward and the one shared `ToolSet` for the process's whole life. `/webhook` is what Alertmanager posts to — filters for `root_cause_hint` (our own annotation marking "this is one of our 6 alerts, not kube-prometheus-stack's bundled Watchdog/etc.") and fires `_run_investigation()` as a background `asyncio` task so Alertmanager's webhook call returns immediately. `/incidents` lists completed investigation records. |
| `tracing.py` | OpenTelemetry setup. `get_tracer()` manages its own module-level `TracerProvider` reference rather than using OTel's global `trace.set_tracer_provider()` (which is one-shot and silently refuses re-registration — this bit a test suite that needed a fresh provider per test). `JSONFileSpanExporter` appends one JSON line per finished span to `traces/spans.jsonl`; set `TRACE_EXPORTER=otlp` to point at a real collector instead, with zero instrumentation code changes. |

**Read order if you're new to this package**: `budgets.py` → `schema.py` → `llm.py` →
`mcp_client.py` → `prompts.py` → `orchestrator.py` → `webhook.py` → `tracing.py` (tracing
last since it's cross-cutting and easiest to understand once you've seen what it's
wrapping).

---

## 3. `tools/` — the 4 MCP tool servers

Each file is a standalone MCP server built with FastMCP (`python -m tools.<name>` runs
it). Every tool function returns plain JSON-serializable dicts — errors come back as
`{"error": "..."}` data, never as raised exceptions that would propagate oddly through
the MCP transport.

| File | Exposes | Key design point |
|---|---|---|
| `prometheus_query.py` | `prometheus_query_instant`, `prometheus_query_range_stats` | Checks HTTP status *before* calling `.json()`/`raise_for_status()` — Prometheus returns 4xx with a JSON error body that `raise_for_status()` would otherwise swallow as a generic exception. Range queries are downsampled to min/max/avg/last rather than returned as raw timeseries, to keep output small. |
| `log_search.py` | `search_pod_logs` | Talks to Loki's LogQL API — same status-before-parse fix (Loki's 4xx bodies are plain text, not JSON). `normalize_line()` collapses numbers/UUIDs/IPs in each line so near-duplicate log lines (e.g. differing only by a timestamp or request ID) group together instead of flooding the result with noise; UUID/IP patterns are matched *before* the generic digit pattern so they're not partially eaten by it first. |
| `kube_read.py` | `kube_list_pods`, `kube_get_pod_logs`, `kube_list_events`, `kube_list_deployments` | **Safety-critical.** `get_restricted_api_client()` mints a short-lived token for the fixed `incident-agent-reader` ServiceAccount via `kubectl create token`, always in the fixed `NAMESPACE` constant — never whatever namespace a tool call's arguments (ultimately LLM-controlled) specify. Decoupling token-minting namespace from query namespace was a real bug fix (the two were originally conflated). RBAC itself (no secrets, no write verbs — `infra/rbac/role.yaml`) is the actual enforcement; `tests/test_kube_read.py` proves it with real 403s from a live API server, not mocks. |
| `runbook_search.py` | `search_runbooks_tool` | Builds a SQLite index over `runbooks/*.md`. Primary path: embed query + runbook content via a local Ollama embedding model (`nomic-embed-text`) and rank by similarity. If Ollama's embedding endpoint is unreachable, falls back to BM25 (`rank_bm25`, pure Python, no network) — this is what makes runbook search work in a fully air-gapped environment that doesn't happen to have the embedding model pulled. |
| `common.py` | `cap_list()`, `cap_json()` | Shared truncation helpers used by every tool above so a single verbose result can't blow out a local model's context window. Two-tier: `cap_list` caps item *count*, `cap_json` is a character-count safety net on the serialized whole. |

---

## 4. `executor/` — the only write-capable component

This is a **separate FastAPI service** (own process, own port 8020 via `make executor`)
and the single most safety-relevant package in the repo. The agent can only *propose*;
this is the only code path that can issue a `kubectl` command that changes cluster state.

| File | Role |
|---|---|
| `allowlist.py` | `ALLOWED_ACTIONS = {"rollout_undo": ..., "scale": ...}` — a small, fixed dict. `ALLOWED_TARGETS` restricts which Deployments can be touched at all; `MIN_REPLICAS=0`/`MAX_REPLICAS=3` bound `scale`. `execute_action()` **re-validates action name, target, and replica bounds immediately before running `kubectl`** — not just trusting that whatever called it already checked. This re-check is the actual safety boundary: it was verified live that a fake, non-allowlisted action "approved" through the API still gets refused here, with the cluster untouched. |
| `store.py` | `sync_from_incidents()` is the **only** place a `Proposal` object is ever created — it scans `evals/runs/*.json` (the records `agent/webhook.py` writes) for investigations with a non-null `proposed_action` and turns each into a pending proposal. `append_audit()` writes one append-only JSONL line (`executor/audit.jsonl`) per approve/reject/execute decision — the file is never mutated or deleted by the running system, only appended to. |
| `app.py` | FastAPI routes: `GET /proposals` (list pending), `GET /proposals/{id}`, `POST /proposals/{id}/approve` (the only route that can reach `execute_action()`), `POST /proposals/{id}/reject` (never touches the cluster or the allowlist at all), `GET /audit`. |

**Credential model** (see `docs/customer-deployment.md` for the full production
guidance): the agent's `kube_read` tool and the executor's write path are completely
separate identities. In this reference deployment the executor reuses the same local
admin kubeconfig as everything else — the doc explicitly flags that as the one thing to
scope down before a real deployment (a dedicated ServiceAccount with `patch` on
`deployments` only, nothing else).

---

## 5. `faults/` — 6 realistic failure injectors

Each file implements the same three-function shape:

```python
def inject():  # break something for real (patches a Deployment, env var, etc.)
def reset():   # undo it
def run():     # inject() -> wait_for_alert() -> reset()
```

`lib.py` has the shared plumbing:
- `kubectl()` — thin subprocess wrapper, one place to change how every fault shells out.
- `record_fault_start()` / `record_fault_end()` — timestamps consumed later by
  `evals/scorer.py` to measure time-to-diagnosis.
- `wait_for_alert()` — polls `infra/webhook-receiver`'s `/alerts` endpoint for the
  expected alert to show up as firing.
- `wait_rollout()` — waits for a `kubectl rollout status` to finish.
- `patch_deployment()` — applies a JSON patch. **Reset uses `"$patch": "delete"`
  directives, not `kubectl apply` of the pristine manifest** — `apply`'s 3-way merge
  can't see or remove fields that `patch` added out-of-band, since they never show up in
  the Deployment's `last-applied-configuration` annotation. Re-applying the original
  manifest would silently leave the fault's changes in place. This was a real bug caught
  by a fault scenario that "reset" but didn't.

| Scenario | What it breaks | Alert it should trigger |
|---|---|---|
| `oom_kill.py` | Sets `MEMORY_BALLAST_MB` on `cart` so it allocates past its memory limit | `PodOOMKilled` |
| `bad_deploy.py` | Scales to 0, swaps in a broken image, scales back up (avoids a rolling update masking the failure) | `DeploymentUnavailable` |
| `cpu_throttle.py` | Lowers `cart`'s CPU limit and sets `LATENCY_MS` (which deliberately excludes `/healthz`/`/readyz` so the fault doesn't kill its own readiness probe — a real earlier bug) | `HighLatency` |
| `kafka_lag.py` | Sets `CONSUMER_STOPPED`/`CONSUMER_DELAY_MS` on `kafka-consumer` so it stays connected but stops/slows polling | `KafkaConsumerLagHigh` |
| `crash_loop.py` | Sets a bad config so the pod `sys.exit`s immediately on boot | `CrashLoopBackOff` |
| `dependency_down.py` | Takes down `cache`, but the alert fires on `cart` (which calls it) — this is the one scenario where the symptomatic component and the root-cause component differ, which is exactly what `dependency_down` as a root-cause label means | `HighErrorRate` |

`run_all.py` runs all 6 in sequence with per-scenario exception isolation (one fault
failing doesn't stop the rest from running) and reports pass/fail per scenario.

---

## 6. `target-app/` — the thing being broken

A deliberately simple 5-service FastAPI/Python app whose only job is to have realistic,
breakable failure modes:

- **`frontend/`** — serves a simple page, runs a background traffic-generator loop
  (`_traffic_loop`) that calls the other services continuously so Grafana/Prometheus
  always have baseline traffic/metrics, not just whatever a fault happens to produce.
- **`cart/`** — has three built-in fault hooks read from env vars: `FORCE_CRASH_ON_BOOT`,
  `MEMORY_BALLAST_MB`, `LATENCY_MS` (the latter explicitly skips `/healthz` and
  `/readyz`). `/healthz` and `/readyz` also differ deliberately: `/healthz` doesn't check
  Redis, `/readyz` does — so a cache outage pulls the pod out of Service endpoints
  (readiness fails) without the pod being killed (liveness still passes), modeling a
  realistic "degraded but not crashed" dependency failure.
- **`kafka-producer/`** and **`kafka-consumer/`** — use `kafka-python-ng` (a maintained
  fork; the original `kafka-python` is broken on Python 3.12). The consumer computes
  **real** lag via `end_offsets() - position()` (not simulated), and needs an initial
  `poll()` plus a retry loop (up to 60s) before `consumer.assignment()` returns anything,
  because partition assignment in this library only happens after a poll completes — the
  `CONSUMER_STOPPED` fault path originally never polled, which was a real bug.
- `infra/webhook-receiver/app.py` is not part of `target-app/` but lives alongside it in
  role: a tiny FastAPI service that logs every Alertmanager webhook payload to JSONL and
  exposes `/alerts` — used only for `faults/lib.py`'s `wait_for_alert()` confirmation,
  not part of the actual investigation path (that's `agent/webhook.py`, a different
  service on a different port).

All 5 Dockerfiles for these services set `PYTHONUNBUFFERED=1` — Python fully buffers
stdout when it's not attached to a TTY, which made `kubectl logs` show nothing at all
until this was added.

---

## 7. `evals/` — the eval harness and the project's central narrative

`make eval` runs this end to end and produces `evals/report.md` / `evals/results.json`
— the numbers in `README.md` come from here, not from hand-written claims.

| File | Role |
|---|---|
| `scenarios.yaml` | Ground truth per fault scenario: expected `root_cause_label`, `component`, `expected_alerts`. Not Python — this is the answer key `scorer.py` compares against. |
| `baseline.py` | `investigate_baseline()` — a deliberately dumb comparison pipeline: exactly 5 fixed tool calls (no adaptive investigation at all) plus exactly 1 LLM call, no retries. Exists so the report can show whether the full adaptive `agent/orchestrator.py` loop is actually earning its larger token/time budget versus this fixed floor. |
| `judge.py` | `judge_result()` — an optional LLM-as-judge scorer using a second, smaller model (`EVAL_JUDGE_MODEL`, e.g. `qwen2.5:3b`) to grade explanation quality rather than label correctness. Fully implemented but **not exercised** in the actual documented eval run (time/cost tradeoff) — present as a capability, not as something the current `evals/report.md` numbers depend on. |
| `scorer.py` | `score_run()` — exact-match scoring: `root_cause_label` **and** `component` both correct is the strict metric used in the real report. `classify_failure_mode()` — regex-based classification of *why* a run was wrong (`wrong_root_cause_or_component`, `schema_validation_failed`, `inconclusive`, `wallclock_timeout`, ...) by parsing the reason text `inconclusive_result()` embeds in the hypothesis field. `aggregate()` rolls per-scenario results into the overall report. |
| `runner.py` | `run_one()` drives a single scenario end-to-end: real fault inject, wait for the real investigation to complete, score it, reset. `_reset_with_retry()` retries fault reset up to 4 times with 20s backoff — added after a transient Kubernetes API TLS timeout crashed the whole harness mid-run and lost already-computed results; `main_async()` wraps every scenario in its own try/except for the same reason (one scenario's infrastructure hiccup shouldn't lose the rest of the run). `EVAL_RUNS_PER_SCENARIO` (code default 5) controls repetitions per scenario; the actual documented run used 1 because a full N=5 matrix was projected to take many hours on this hardware. |

**The central result, and why it's the point of this project**: the first full real run
scored **0% exact-match accuracy**. `scorer.py`'s failure-mode classification pointed at
something concrete: the model was inventing `root_cause_label` values straight from the
alert name or a variable it noticed in evidence (e.g. `crash_loop_backoff`,
`force_crash_on_boot`) instead of picking a real root cause — and because
`agent/schema.py` only required `root_cause_label` to be *a string*, every one of those
was schema-valid, so nothing caught it before scoring. The fix (documented in
`docs/failure-modes.md` with real before/after hypothesis text) was adding a closed
`enum` to `agent/schema.py`'s `OUTPUT_SCHEMA` and an explicit closed list + "don't invent
labels" instruction to `agent/prompts.py`'s `SYSTEM_PROMPT` — re-verifying the affected
scenarios afterward took them from 0/3 to 3/3, same model, same budget, nothing else
changed.

---

## 8. `ui/` — the human approval surface

`ui/app.py` (port 8080, `make ui`) is a small server-rendered-HTML FastAPI app. It is
**not** the executor — it holds no state of its own and talks to `executor/app.py` only
over HTTP via `httpx`, deliberately never importing executor code directly, so the UI
and the write-capable executor remain genuinely separate processes and trust boundaries.
It renders pending proposals (`_proposal_card()`) and relays Approve/Reject clicks as
POSTs to the executor's `/proposals/{id}/approve` or `/reject`.

---

## 9. `infra/` — cluster, observability, and safety plumbing (not Python, but load-bearing)

| Path | Role |
|---|---|
| `kind/kind-config.yaml` | kind cluster definition with hostPort mappings for Grafana (3000), Prometheus (9090), Alertmanager (9093), webhook-receiver (8000), Loki (3100). |
| `helm-values/kube-prometheus-stack-values.yaml` | Grafana/Alertmanager/Prometheus resource limits (raised during development after real OOMKills — see `docs/DECISIONS.md`), and the Alertmanager receiver config with **two** webhook targets: the in-cluster `webhook-receiver` service and `http://host.docker.internal:8010/webhook` (the host-running `agent/webhook.py`). Keeps the `"null"` receiver entry kube-prometheus-stack's bundled Watchdog alert routes to — removing it breaks Alertmanager's own config validation. |
| `alert-rules/alert-rules.yaml` | The 6 `PrometheusRule`s this project depends on, each with a `root_cause_hint` annotation — the signal `agent/webhook.py` uses to tell "one of ours" apart from kube-prometheus-stack's bundled defaults. |
| `rbac/{serviceaccount,role,rolebinding}.yaml` | The investigation-side RBAC: `incident-agent-reader` ServiceAccount, namespaced `Role` (get/list/watch only, no secrets), `RoleBinding`. This is the actual safety mechanism `tools/kube_read.py` depends on — see §4/§3 above. |
| `redpanda/redpanda.yaml` | Plain `Deployment` (not the Redpanda Operator/Helm chart) running Redpanda in dev-container mode — chosen for being light enough for one broker on a resource-constrained kind cluster. |
| `webhook-receiver/` | The small alert-logging service described in §6, plus its own `k8s.yaml` manifest. |
| `preflight.sh` | Checks hardware (CPU/RAM/disk) and required tools, prints actionable `brew install` commands for anything missing. |

---

## 10. `traces/` and `replay/` — making investigations inspectable

- `traces/viewer.html` is a vanilla-JS waterfall viewer that reads `traces/spans.jsonl`
  (written by `agent/tracing.py`'s `JSONFileSpanExporter`) and renders one waterfall per
  `trace_id` — i.e. per investigation.
- `replay/index.html` is a fully static, dependency-free 4-step stepper (Alert → Scope &
  Evidence → Hypothesis → Approval) that plays back one recorded investigation
  (`replay/data/investigation.json`, a real trace) plus one real executor
  approve/audit-log demo — built so the whole demo can be hosted free on GitHub Pages
  with no backend at all.

---

## 11. `tests/`

69 tests total, `make test` / `pytest tests/ -q`. Notable patterns, not just coverage:

- `tests/test_kube_read.py` proves the RBAC boundary with **real** 403s from a live
  Kubernetes API server using the actual restricted ServiceAccount token — not a mock
  asserting the code merely lacks a `delete` method.
- `tests/test_executor.py` (13 tests) proves unapproved and non-allowlisted actions never
  reach `kubectl`, by calling `execute_action()` directly with disallowed inputs and
  asserting it refuses.
- `tests/test_tracing.py` uses OTel's `InMemorySpanExporter` via
  `agent.tracing.reset_tracer_for_testing()` so span assertions don't need
  `traces/spans.jsonl` or any env var configuration.
- `tests/test_baseline.py` / `tests/test_scorer.py` test the eval pipeline's logic
  (exact-match scoring, failure-mode classification) against synthetic inputs, without
  needing a live cluster.

---

## 12. Where to make a given kind of change

| If you want to... | Start here |
|---|---|
| Add a new fault scenario | `faults/`, following the `inject()/reset()/run()` shape in `faults/lib.py`; add ground truth to `evals/scenarios.yaml`; add a `PrometheusRule` to `infra/alert-rules/alert-rules.yaml` with a `root_cause_hint` annotation; add the label to `agent/schema.py`'s `ROOT_CAUSE_LABELS` and `agent/prompts.py`'s closed list. |
| Add a new MCP tool | A new file in `tools/` using FastMCP, add it to `agent/mcp_client.py`'s `TOOL_SERVERS` dict — nothing else needs to change, the orchestrator picks up new tools automatically via `ToolSet.schemas()`. |
| Change the investigation method | `agent/prompts.py`'s `SYSTEM_PROMPT` — this is the only place the method is specified. |
| Add a new write action | `executor/allowlist.py`'s `ALLOWED_ACTIONS` + a new `_<action>()` function with the same re-validate-everything pattern as `_scale`/`_rollout_undo`. |
| Swap the LLM backend | A new `LLMProvider` subclass in `agent/llm.py` + a branch in `get_default_provider()`. |
| Swap the observability stack | See `docs/customer-deployment.md`'s "Adapting to a different observability/alerting stack" section — the MCP tool *interface* is what `agent/prompts.py`/`orchestrator.py` depend on, not the backend behind it. |
