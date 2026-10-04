# Customer deployment guide

This project is built and verified against a local kind cluster for portfolio/demo
purposes. This document describes what actually changes to run the same architecture
against a real cluster and a real alerting stack.

## Permissions model

The agent's investigation path and the executor's write path use **completely separate
credentials**, by design:

| Component | Credential | Scope |
|---|---|---|
| `kube_read` tool (agent) | Dedicated ServiceAccount token, minted per call via `kubectl create token` | `get/list/watch` on `pods`, `pods/log`, `events`, `services`, `deployments`, `replicasets` in one namespace. No secrets. No write verbs anywhere. |
| `executor` service | Whatever `kubectl` context it runs under (in this project, the same admin kubeconfig used for local dev - **this is the one thing that must change for production**, see below) | Only the actions in `executor/allowlist.py`'s `ALLOWED_ACTIONS` dict, each bounds-checked at execution time against `ALLOWED_TARGETS` and (for `scale`) a replica range. |

In production, the executor should run with its **own** scoped ServiceAccount too - a
`Role` granting exactly `patch` on `deployments` (for `rollout undo` and `scale`,
nothing else) in the namespaces it's allowed to touch. The agent's investigation
ServiceAccount and the executor's action ServiceAccount should never be the same
identity, so a bug in the read-only investigation path cannot, even in principle,
reach a write verb.

## RBAC: what's actually in this repo

`infra/rbac/role.yaml` and `infra/rbac/rolebinding.yaml` define the investigation-side
Role exactly as described above, scoped to the `incident-agent` namespace via a
namespaced `Role` (not a `ClusterRole`) since the demo app only lives in one
namespace. For a real deployment spanning multiple namespaces, either:
- duplicate the `Role`/`RoleBinding` pair per namespace the agent should be able to
  read, or
- switch to a `ClusterRole` + `ClusterRoleBinding` if the agent needs to investigate
  across the whole cluster (still get/list/watch only, still no secrets).

The executor's write-side RBAC is **not yet included in this repo** - it currently
reuses the same local kubeconfig as everything else, which is fine for a single-user
local demo and explicitly called out as the one thing to fix before any real
deployment (see `docs/DECISIONS.md`, Phase 5).

## Rollout plan for a real environment

1. **Read-only first.** Deploy the agent, the 4 MCP tool servers, and the webhook
   intake with the investigation-side RBAC only. Point Alertmanager's webhook at it.
   Let it run for a period producing hypotheses with zero write capability at all -
   the executor doesn't even need to be deployed yet. This is low-risk and immediately
   useful: a human still does everything, just with a pre-built hypothesis and
   evidence trail waiting for them.
2. **Add the approval UI, still no execution.** Deploy the executor with an *empty*
   `ALLOWED_ACTIONS` (or a dry-run flag that logs what it would have run without
   calling `kubectl`). This validates the proposal → approval → audit pipeline without
   any real risk.
3. **Enable the allowlist for one action, one namespace.** Start with the lowest-risk
   action (`rollout undo` is generally safer than `scale`, since a rollback mostly
   targets a known-good prior state) in one non-critical namespace. Watch the audit
   log for a while.
4. **Expand the allowlist and target namespaces incrementally**, each addition
   reviewed the same way the original two actions were: bounded parameters, a fixed
   target list, re-checked at execution time regardless of what the proposal claims.

Do not skip straight to step 4. The value of this architecture is that each step is
independently safe to leave running for an extended period before moving to the next.

## Data handling

- **What leaves the cluster**: nothing, by default. The reference deployment runs
  Ollama locally - no alert content, logs, or metrics are sent to a third-party API.
  If a hosted LLM provider is substituted (see `agent/llm.py`'s `LLMProvider`
  interface), whatever gets included in tool-call results and the investigation
  prompt *does* leave the cluster to that provider - review `tools/*.py`'s output
  caps and `tools/log_search.py`'s normalization (which already strips most
  high-cardinality values like IPs and IDs) before pointing a hosted provider at
  production logs.
- **What gets persisted locally**: `traces/spans.jsonl` and `traces/<incident>.json`
  (full conversation history including tool inputs/outputs, truncated per-field),
  `evals/runs/*.json` (fault ground truth), `executor/audit.jsonl` (every
  approve/reject decision, append-only, never mutated or deleted by the running
  system). None of this is encrypted at rest in this reference implementation - treat
  the trace/audit storage like you would application logs containing production data,
  because that's what it is.
- **Secrets**: the investigation-side RBAC explicitly excludes the `secrets` resource
  entirely (verified by `tests/test_kube_read.py::test_restricted_client_cannot_read_secrets`).
  If a future tool needs secret access for some reason, that's a deliberate RBAC
  change to review on its own, not an incidental side effect of a broader grant.

## Adapting to a different observability/alerting stack

This project is built on Prometheus + Alertmanager + Loki because they're free and
self-hostable, but the architecture doesn't assume them specifically:

- **Alert intake** (`agent/webhook.py`): expects an Alertmanager-shaped webhook
  payload (`{"alerts": [{"status", "labels", "annotations", ...}]}`). Datadog, Splunk,
  PagerDuty, and most other alerting tools can send webhooks in a configurable shape -
  either configure the alerting tool to send Alertmanager-compatible JSON, or add a
  small adapter in front of `_alert_summary()` that maps the real payload shape to the
  same `{alertname, namespace, severity, summary, description, root_cause_hint}`
  fields the rest of the system expects. `root_cause_hint` is this project's own
  convention (an alert annotation naming the likely root-cause category) - it's used
  purely to filter which alerts are worth investigating and isn't required for the
  agent to function if your alerting tool doesn't have an equivalent.
- **Metrics tool** (`tools/prometheus_query.py`): talks to Prometheus's HTTP API
  directly. For Datadog, replace this tool's implementation with Datadog's Metrics API
  client, keeping the same MCP tool interface (`prometheus_query_instant`,
  `prometheus_query_range_stats`) so `agent/orchestrator.py` and the system prompt
  don't need to change - only what's inside the tool.
- **Log tool** (`tools/log_search.py`): talks to Loki's LogQL API. For Splunk, swap
  the query construction and result parsing for Splunk's REST search API, keeping the
  same pattern-summarization behavior (group by normalized pattern, cap samples) since
  that's what keeps a small local model from drowning in raw log volume regardless of
  the log backend.
- **Kube read tool**: unaffected by the observability stack choice - it talks directly
  to the Kubernetes API regardless of what's generating alerts.

In all cases, the MCP tool *interface* (name, parameters, return shape) is what
`agent/prompts.py` and `agent/orchestrator.py` are written against - swapping an
implementation behind that interface shouldn't require touching the orchestrator.

## Running air-gapped

The reference stack is already close to air-gapped-compatible:

- **LLM**: Ollama runs entirely locally once the model is pulled
  (`ollama pull qwen2.5:7b`) - no network access needed at inference time. Pull the
  model (and `nomic-embed-text` for runbook search) on a connected machine first and
  transfer the model blobs (`$OLLAMA_MODELS`) to the air-gapped host.
- **Container images**: `make up` builds the 5 sample-app images locally via `docker
  build` and loads them into kind with `kind load docker-image` - no registry pull for
  those. The observability stack (`kube-prometheus-stack`, `loki-stack`) does pull
  from public Helm chart repos and public image registries at install time; for a
  real air-gapped environment, mirror those charts/images to an internal registry and
  point `infra/install-observability.sh`'s `helm repo add` calls and the Helm values
  files' `image.repository` fields at the internal mirror instead.
- **Runbooks**: `runbooks/*.md` are plain files in the repo - no external dependency.
  `tools/runbook_search.py` falls back to BM25 (`rank_bm25`, a pure-Python library
  with no network calls) automatically if Ollama's embedding endpoint is unreachable,
  so runbook search degrades gracefully rather than failing in a fully offline
  environment that for some reason doesn't have the embedding model pulled.
- **Everything else** (the agent, tools, executor, UI) is plain Python with no
  outbound network calls beyond the cluster's own API server, Prometheus, Loki, and
  Ollama - all of which are either in-cluster or localhost in this architecture.

The main air-gapped-specific work is mirroring the Helm charts/images for the
observability stack, since that's the one piece in this reference implementation that
assumes internet access at install time.
