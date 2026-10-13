# incident-agent

An on-call incident triage agent: it receives Prometheus/Alertmanager alerts from a local Kubernetes cluster, investigates using read-only tools (PromQL, log search, kubectl, runbooks), and outputs a root-cause hypothesis with linked evidence plus a proposed remediation. Any write action requires explicit human approval and runs through a separate executor service. Entirely free and open source — local LLM via Ollama, local kind cluster, no paid APIs or cloud.

## Contents

- [Why](#why)
- [Quickstart](#quickstart)
- [Architecture](#architecture)
- [Project structure](#project-structure)
- [What's actually been verified](#whats-actually-been-verified)

## Why

On-call engineers spend the first several minutes of every incident doing the same mechanical investigation: check the alert, look at a dashboard, grep some logs, check recent deploys. That repetition is exactly what pushes MTTR up and burns people out under alert fatigue — not because the work is hard, but because it's slow and repetitive *before* the hard part even starts. This project builds an agent that automates that first pass: producing a hypothesis with linked evidence a human can verify in seconds, not minutes, while keeping every write action behind explicit human approval and infrastructure-enforced (not prompt-enforced) read-only access for the investigation itself.

**A real measured result, not a demo claim:** the eval harness (`make eval`) produces `evals/report.md` and `evals/results.json` from actual runs against a live kind cluster — fault injected, real alert fired, real agent investigation, scored against ground truth, every number traceable to a timestamped trace file. The first full run scored **0% exact-match accuracy** (strict: root-cause label *and* affected component both correct, N=1 per scenario). That number immediately pointed at something concrete and fixable: the agent's `root_cause_label` output wasn't constrained to a real taxonomy, so it invented labels straight from the alert name or a variable it saw in evidence (`crash_loop_backoff`, `force_crash_on_boot`) instead of picking an actual root cause. One prompt + schema fix later (see `docs/failure-modes.md` for the full before/after with real hypothesis text), re-running the affected scenarios took them from **0/3 to 3/3 exact-match**, same model, same budget, nothing else changed. That loop — real eval, real failure, concrete fix, re-verified improvement — is the actual deliverable here, not just the agent itself.

## Quickstart

Prerequisites (all free):

```bash
brew install --cask docker   # launch it once after installing
brew install kind kubectl helm uv
brew install ollama          # or https://ollama.com/download
```

Check what's missing and get actionable fixes:

```bash
make preflight
```

Bring up the cluster, observability stack, and sample app:

```bash
make up
```

Pull the default model (qwen2.5:7b, ~4.7GB) and start the agent's webhook intake and the approval UI:

```bash
ollama pull qwen2.5:7b
make agent      # webhook intake + investigation loop, port 8010
make executor   # approval-gated executor service, port 8020
make ui         # approval UI, port 8080
```

Trigger a real incident and watch it get investigated end to end with zero manual steps:

```bash
python3 faults/oom_kill.py run   # inject, wait for the alert, let the agent investigate, reset
```

Run the fault suite, the eval harness, or view a recorded trace:

```bash
make faults          # all 6 fault scenarios, verifies each alert fires and resets cleanly
make eval             # real eval run against the live cluster -> evals/report.md
make trace-viewer    # OpenTelemetry span waterfall for any saved investigation
make replay           # play back a recorded incident (no live cluster needed)
```

## Architecture

```mermaid
flowchart LR
    FI[Fault injector] --> APP[Sample app on kind]
    APP --> PROM[Prometheus + Loki]
    PROM --> AM[Alertmanager]
    AM -->|webhook| AGENT[Agent orchestrator]
    AGENT <--> TOOLS[MCP tools:\nprometheus_query, log_search,\nkube_read, runbook_search]
    AGENT -->|propose_action| APPROVAL[Approval UI/API]
    APPROVAL -->|approve| EXEC[Executor service\nallowlisted actions]
    EXEC --> APP
    AGENT -.OTel traces.-> TRACE[JSON traces + viewer]
```

Safety is enforced by infrastructure, not prompts: the agent's `kube_read` tool authenticates as a dedicated Kubernetes ServiceAccount with a read-only Role (no secrets, no write verbs) — verified directly by attempting a real `delete`/`patch`/secrets-read through that exact client and confirming the API server returns 403, not by checking the tool's own code lacks those methods (`tests/test_kube_read.py`). Write actions exist only in a separate executor service (its own process, its own port) behind an approval API, a small action allowlist re-checked at execution time, and an append-only JSONL audit log.

## Project structure

- **`agent/`** — the investigation loop itself: `webhook.py` receives Alertmanager webhooks and kicks off an investigation, `orchestrator.py` runs the step-by-step tool-calling loop under budgets, `llm.py` wraps the local Ollama model, `mcp_client.py` talks to the MCP tool servers, `prompts.py` and `schema.py` hold the prompt and the structured-output schema the agent's conclusion is validated against, and `budgets.py`/`tracing.py` handle step/token/wallclock limits and OpenTelemetry spans.
- **`tools/`** — the four read-only MCP tools the agent investigates with: `prometheus_query.py`, `log_search.py`, `kube_read.py` (the restricted ServiceAccount client), and `runbook_search.py`, plus shared helpers in `common.py`.
- **`executor/`** — the only service with write-capable credentials: `app.py` exposes the approval API (propose/approve/reject/execute), `allowlist.py` re-checks every action against a small allowlist at execution time regardless of what was approved, and `store.py` persists proposals and the append-only audit log.
- **`dashboard/`** — a single-page operator dashboard (`app.py`) aggregating active Alertmanager alerts, live pod status (via `kubectl`, operator-level access), in-progress and completed agent investigations, and pending executor approvals — with Approve/Reject right on the page. Read-mostly; talks to the agent/executor only over HTTP.
- **`ui/`** — the minimal approval queue UI (`app.py`): lists pending/decided remediation proposals from the executor with Approve/Reject forms. Same relay-only role as `dashboard/`, just scoped to approvals rather than the full operator view (see note on overlap below).
- **`faults/`** — the fault injectors used to generate real incidents: `oom_kill.py`, `bad_deploy.py`, `cpu_throttle.py`, `crash_loop.py`, `dependency_down.py`, `kafka_lag.py` (one per scenario, each injects/waits/resets), `lib.py` with shared kubectl helpers, and `run_all.py` to run the full suite.
- **`evals/`** — the eval harness: `runner.py` drives a fault + alert + investigation end-to-end per scenario, `scorer.py` and `judge.py` score the result against ground truth, `baseline.py` holds the comparison baseline, `scenarios.yaml`/`scenarios/` define the scenario set, and `report.md`/`results.json` are the harness's own output (`make eval`).
- **`runbooks/`** — the Markdown runbooks (`oom-kill.md`, `bad-deploy.md`, `cpu-throttling.md`, `crash-loop-bad-config.md`, `dependency-down.md`, `kafka-consumer-lag.md`, plus `high-error-rate.md`/`high-latency.md`) that `tools/runbook_search.py` searches over during an investigation — one of the agent's four evidence sources alongside metrics, logs, and cluster state.

`dashboard/app.py` and `ui/app.py` are independent FastAPI processes rather than a shared library, so some code (an `_esc()` HTML-escape helper, the approve/reject relay routes, card markup) is duplicated between them by design — each is its own deployable service, not a shared import.

## What's actually been verified

Every phase in this project was built against a live cluster and real local LLM, not mocked out, and every claim below is backed by a committed artifact:

- **6 fault scenarios** (OOM kill, bad deploy, CPU throttling, Kafka consumer lag, crash loop, dependency down) each reliably trigger their alert and reset cleanly (`faults/`, `make faults`).
- **4 MCP tools** (`prometheus_query`, `log_search`, `kube_read`, `runbook_search`) return trimmed, capped JSON and are exercised through the real MCP protocol, not direct function calls (`tools/`, `tests/`).
- **The investigation loop** runs under hard budgets (steps/tokens/wallclock), validates its own output against a JSON schema with bounded retry, and can honestly say "inconclusive" rather than fabricate a root cause (`agent/orchestrator.py`).
- **The approval gate** was verified live, not just in unit tests: a real `scale` proposal was approved and genuinely scaled a deployment; a `rollout_undo` proposal was rejected and the deployment's generation never changed; a fake non-allowlisted action was "approved" and still refused at the allowlist check (`docs/DECISIONS.md`, Phase 5).
- **Every LLM call and tool call produces an OpenTelemetry span** (truncated input/output, token counts, latency) with a real waterfall viewer (`traces/viewer.html`, `make trace-viewer`).
- **The eval harness produces real, unfakeable numbers** and the project's own failure-mode iteration loop is documented with real before/after data, not projected (`evals/`, `docs/failure-modes.md`).

See `docs/customer-deployment.md` for deployment/permissions details, `docs/failure-modes.md` for the honest failure list, and `docs/DECISIONS.md` for a running log of every non-obvious choice made while building this (including several real infrastructure crashes survived mid-build - this machine running a full kind cluster and local 7B inference simultaneously is a genuine, documented hardware constraint).
