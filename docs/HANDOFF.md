# incident-agent — handoff brief

This is a complete context handoff for the `incident-agent` project, written so a
fresh conversation (no prior memory of this build) can pick up exactly where it left
off. Everything below is accurate as of commit `4c8af88` ("Phase 9: replay site and
docs") — all 10 planned phases complete and independently re-verified.

---

## 1. What this project is and why it exists

**incident-agent** is a portfolio project for a Forward Deployed Engineer (FDE) job
search: an on-call incident triage AI agent. It receives Prometheus/Alertmanager
alerts from a local Kubernetes cluster, investigates using tools (PromQL, log search,
read-only kubectl, runbook retrieval), outputs a root-cause hypothesis with linked
evidence, and proposes a remediation. Any write action (rollback, scale) requires
explicit human approval and is executed by a **separate** service. The project
includes fault injection, an eval harness that reports real accuracy/cost/latency
numbers, and a static replay demo deployable to GitHub Pages.

### Hard constraints (non-negotiable, from the original spec)

- **Free only.** No paid APIs, no paid cloud. LLM via Ollama locally, default model
  `qwen2.5:7b` (configurable via `LLM_MODEL` env var), must support tool calling.
- **Provider abstraction**: all LLM access goes through `agent/llm.py`'s
  `LLMProvider` interface so other free providers can be added later without
  touching the orchestrator.
- **Cluster**: kind. **Kafka**: Redpanda single-node. **Observability**: Prometheus,
  Alertmanager, Grafana, Loki + Promtail via Helm. **Tracing**: OpenTelemetry
  (Jaeger or JSON files + viewer if Jaeger is too heavy for the hardware).
- **Safety enforced by infrastructure, not prompts**: the agent's `kube_read` tool
  uses a Kubernetes ServiceAccount with read-only RBAC (no secrets, no write verbs).
  Write actions exist only in a separate executor service behind an approval API,
  with an action allowlist and an append-only JSONL audit log.
- **Language**: Python 3.11+ for agent/tools/orchestrator/evals.
- **Never fabricate or hand-edit eval numbers.** Every number in `evals/report.md`
  must come from a real run. This rule was tested hard by real infrastructure
  failures mid-build (see §5) and held every time — bad/incomplete data was deleted
  and re-run for real, never patched.
- Work was to proceed in 10 ordered phases (0–9), each with explicit acceptance
  criteria, stopping to report after each phase. **All 10 are now done.**

---

## 2. Architecture

```
fault injector → sample app on kind → Prometheus + Loki → Alertmanager
    → webhook → agent orchestrator → MCP tool servers (prometheus_query,
    log_search, kube_read, runbook_search) and propose_action
    → approval UI/API → executor service → cluster
```

Every LLM call and tool call is traced via OpenTelemetry.

### Key architectural decisions worth knowing

- **The agent runs on the host, not in-cluster.** It needs `ollama`, local
  `kubectl`, and to spawn MCP tool subprocesses. Alertmanager (in-cluster) reaches
  it via Docker Desktop's `host.docker.internal:8010` DNS name — verified reachable
  with a direct test before relying on it.
- **The agent calls tools only through real MCP client sessions**
  (`agent/mcp_client.py`: `stdio_client` + `ClientSession`, one subprocess per
  `tools/*.py` server), never by importing `tools/*.py` directly. This exercises the
  actual MCP protocol path end to end, not just the underlying Python functions.
- **Two completely separate credential paths**: the agent's `kube_read` tool mints a
  ServiceAccount token (`incident-agent-reader`, namespaced `Role`, no secrets, no
  write verbs — `infra/rbac/`) fresh per call via `kubectl create token`. The
  executor service is a **separate process on a separate port** and is the only
  thing with write-capable credentials. Verified live: approving a non-allowlisted
  fake action was refused at the allowlist check with the cluster untouched;
  rejecting a real proposal left the deployment's `metadata.generation` unchanged.
- **Redpanda runs as a plain Deployment**, not the Operator/Helm chart — too heavy
  for one dev-mode broker on a resource-constrained kind cluster.
- **Tracing exports to JSON files (`traces/spans.jsonl`) by default, not Jaeger** —
  explicitly allowed by the spec as a fallback, and the right call given this
  machine's documented resource ceiling (see §5). Goes through the real
  OpenTelemetry SDK with a custom `JSONFileSpanExporter`, so switching to a real
  collector later is a one-line env var change (`TRACE_EXPORTER=otlp`), not a
  rewrite.
- **The output schema is strict and mechanically enforced**:
  `{hypothesis, root_cause_label, component, confidence, evidence[], proposed_action|null, status: "diagnosed"|"inconclusive"}`,
  validated via `jsonschema` with a **closed enum** for `root_cause_label` (added in
  Phase 8 after discovering the model invented labels — see §6). A malformed or
  invalid-label output triggers a bounded "fix your output" retry
  (`agent/orchestrator.py`, `MAX_FORMAT_RETRIES=2`), not a crash.
- **Hard budgets** on every investigation: max steps, max tokens, max wallclock
  (`agent/budgets.py`). When exceeded, the agent returns `status: "inconclusive"`
  with whatever evidence it gathered — never fabricates a hypothesis to avoid saying
  "I don't know."

---

## 3. Repo structure (what's actually in each directory)

```
infra/            kind config, Helm values (kube-prometheus-stack, loki-stack),
                   RBAC manifests, alert rules (6 PrometheusRules), Redpanda
                   manifest, webhook-receiver (dumb Alertmanager→JSONL logger,
                   used by fault scripts' wait_for_alert), preflight/deploy scripts
target-app/       5 tiny FastAPI/Python services: frontend (+ background traffic
                   generator), cart (+ built-in fault hooks: FORCE_CRASH_ON_BOOT,
                   MEMORY_BALLAST_MB, LATENCY_MS), cache (stock redis:7-alpine),
                   kafka-producer/consumer (real consumer-group lag via
                   end_offsets()-position())
faults/           6 fault scenarios (oom_kill, bad_deploy, cpu_throttle, kafka_lag,
                   crash_loop, dependency_down), each with inject/reset/run,
                   sharing faults/lib.py. run_all.py runs all 6.
tools/            4 MCP tool servers (FastMCP): prometheus_query, log_search,
                   kube_read, runbook_search (SQLite + Ollama embeddings, BM25
                   fallback). Each returns trimmed/capped JSON.
agent/            llm.py (provider interface), mcp_client.py (ToolSet, spawns the
                   4 tool subprocesses), budgets.py, schema.py (output JSON schema
                   + closed root_cause_label enum), prompts.py (investigation-
                   method system prompt), orchestrator.py (the investigation loop),
                   webhook.py (FastAPI intake, filters to alerts carrying our
                   root_cause_hint annotation), tracing.py (OTel setup)
executor/         allowlist.py (the only write choke point: rollout_undo + scale,
                   bounded, target-list-restricted), store.py (proposal + append-
                   only audit log), app.py (FastAPI service, own port 8020)
ui/               app.py — simple server-rendered HTML approval queue, talks to
                   the executor only over HTTP (never imports its code)
evals/            scenarios.yaml (ground truth), baseline.py (fixed 5-tool-call +
                   1-LLM-call comparison pipeline), judge.py (optional LLM-judge,
                   built but not exercised — see §5), scorer.py, runner.py
                   (make eval entry point), report.md + results.json (real,
                   committed eval output)
runbooks/         8 markdown runbooks (one per root_cause_label + 2 symptom-level
                   fallbacks), indexed into SQLite by tools/runbook_search.py
traces/           traces/viewer.html (static OTel waterfall viewer), spans.jsonl
                   (gitignored, regenerated per run)
replay/           index.html — static, dependency-free incident replay (GitHub
                   Pages ready), replay/data/ bundles one real investigation trace
                   + one real executor approval demo
docs/             DECISIONS.md (running log of every non-obvious choice, read this
                   for full detail), failure-modes.md (honest list with real
                   before/after), customer-deployment.md, evidence/ (saved real
                   artifacts per phase)
tests/            pytest suite, 69 tests total, all passing. Covers tool safety
                   (including the write-rejection proof), executor safety,
                   scorer/baseline logic, and OTel span creation — all with mocks
                   where live infra isn't needed, real integration tests where it
                   matters (kube_read, prometheus_query, log_search, executor).
.github/workflows/deploy-replay.yml   GitHub Pages deploy, path-filtered to replay/**
README.md, DEMO_SCRIPT.md, Makefile, pyproject.toml, .env.example
```

---

## 4. Phase-by-phase summary (all 10 done, each independently re-tested)

| Phase | Commit | What shipped | Real verification |
|---|---|---|---|
| 0 | `42298ce`, `bd74f7f` | Repo scaffold, `Makefile`, `infra/preflight.sh`, `pyproject.toml`, git init | `make up` fails gracefully with actionable install commands when prerequisites are missing |
| 1 | `bfade57`, `f4ccdbb` | kind cluster, 5-service sample app, Redpanda, Prometheus/Alertmanager/Grafana/Loki, 6 alert rules | `infra/verify-phase1.sh`: 7/7 — real fault → real alert → real webhook delivery |
| 2 | `e71892d` | 6 fault injectors (`faults/*.py`) | `faults/run_all.py`: 6/6, re-run multiple times across sessions, always clean |
| 3 | `3302e6d` | 4 MCP tool servers | 27 tests; `kube_read` write-rejection proven via **real 403s** from the live API server (not mocked) |
| 4 | `b63a9e7` | Agent investigation loop, webhook intake | 4 of 6 fault scenarios diagnosed live with zero manual steps, saved traces |
| 5 | `da75a6c` | Approval gate + executor | Verified live 3x: real `scale` approved and executed, real `rollout_undo` rejected (generation unchanged), real fake non-allowlisted action refused |
| 6 | `33c203b` | OpenTelemetry tracing + static viewer | 10 tests (InMemorySpanExporter); real browser click-through of a real 28-span trace, found + fixed a dark-mode CSS bug by actually loading it |
| 7 | `935fe34` | Eval harness (`make eval`) | Real 6-scenario report, **survived Docker Desktop crashing outright and Ollama crashing separately mid-run** — recovered by deleting invalid partial results and re-running for real, never patching numbers |
| 8 | `e9f22a9` | Iteration on eval failures | Found root cause (model inventing `root_cause_label` values), fixed prompt + schema, re-verified live: **3/3, up from 0/3** |
| 9 | `4c8af88` | Replay site, README, customer-deployment.md, DEMO_SCRIPT.md | Real browser click-through post-commit; found + fixed a real JS re-render bug (decision text being wiped) |

**Current `pytest tests/` status: 69/69 passing.**

**Current real eval result** (`evals/report.md`, N=1 per scenario, `qwen2.5:7b`,
full report before the Phase 8 fix): 0% exact-match accuracy, 33% inconclusive rate
for the full agent vs. 83% for the baseline. The Phase 8 fix was verified against 3
of those 6 scenarios afterward (3/3 correct) but **the full `evals/report.md` has
not been re-run since the Phase 8 fix** — this is the most obvious next action if
picking this project back up (see §7).

---

## 5. Hardware reality (important — read before running anything expensive)

This machine (Apple M2, 8 cores, 16GB RAM) is a **genuine, repeatedly-documented
hard limit** for running a full kind cluster and local 7B-parameter inference
simultaneously for extended periods:

- CPU repeatedly spiked to **1700%–6000%+** (multi-core saturation) during
  investigation runs, causing real Kubernetes API server TLS handshake timeouts.
- **Docker Desktop crashed outright twice** during this build (not just restarted —
  the main process disappeared, only the privileged helper remained). Both times it
  was relaunched (`open -a Docker`) and the cluster recovered without needing a
  full `kind delete`/recreate (though that was needed once too, documented in
  `docs/DECISIONS.md` Phase 4).
- **Ollama itself crashed once** (zero process running) under the same pressure.
  Relaunched with `open -a Ollama`, verified functional before resuming.
- A corrupted `alerts.jsonl` line (from one of these ungraceful crashes) caused
  `infra/webhook-receiver/app.py`'s `/alerts` endpoint to 500 on **every** request
  — fixed to decode leniently and skip unparseable lines (Phase 7).
- **Resource mitigations already applied**: Docker Desktop's VM memory was lowered
  from ~14GB to ~7.75GB (user request, verified the cluster still fits
  comfortably — peak usage observed ~3.8GB). Ollama's model storage and Docker's
  VM disk image were both migrated from the internal disk to an external SSD
  mounted at `/Volumes/JEX2L` (`OLLAMA_MODELS=/Volumes/JEX2L/ollama-models`,
  Docker's own "Models location"/"Disk image location" settings) — this needed to
  be set at the **app level** (Ollama.app's own Settings UI), not just as a
  one-off env var prefix on `ollama pull`, because the already-running Ollama
  server ignores env vars set only for CLI invocations.
- A single full agent investigation can take anywhere from **~30 seconds to ~480
  seconds** (the wallclock budget) depending on current CPU contention. Budget
  defaults were raised accordingly: `AGENT_WALLCLOCK_TIMEOUT_S=480` (originally
  120, found to be too short for even one multi-tool-call turn),
  `OllamaProvider`'s httpx timeout `240s` (originally 120).
- A single full `make eval` run (6 scenarios, N=1, agent + baseline) took
  **roughly 5 hours** including infrastructure recovery. The spec's suggested
  default of N=5 per scenario would take proportionally longer and was
  deliberately **not** run in full — this was a user-confirmed scope decision
  (see `docs/DECISIONS.md`, Phase 7). The code's own default stays at N=5
  (`EVAL_RUNS_PER_SCENARIO` env var overrides it).

**Practical implication for whoever picks this up next**: before starting any
multi-scenario live run, check `docker stats --no-stream` and `kubectl get pods -A`
first. If CPU is already elevated or pods are flapping, let it settle. Expect to
possibly relaunch Docker Desktop and/or Ollama mid-session — this is normal for
this project on this hardware, not a sign something is broken. The system's own
resilience mechanisms (retry-with-backoff on fault reset, try/except around every
eval-harness scenario, graceful tool-call error handling) were specifically built
to survive this and have proven themselves repeatedly.

---

## 6. Real bugs found and fixed (useful if similar symptoms reappear)

This list is chronological and each one was found by actually running the system
against the live cluster, not by inspection:

1. **Grafana OOMKilled** at boot (192Mi limit too low for Grafana 13.x + 2
   sidecars) — raised limits.
2. **Alertmanager config rejected itself**: `undefined receiver "null" used in
   route` — kube-prometheus-stack's bundled "Watchdog" alert routes to a receiver
   literally named `null` by convention; overriding `receivers` without keeping
   that entry breaks validation.
3. **macOS ships bash 3.2**, which predates `declare -A` (bash 4+) — `faults/*.sh`
   style scripts rewritten to avoid associative arrays.
4. **`kafka-python` 2.0.2 is unmaintained and broken on Python 3.12** — switched to
   the `kafka-python-ng` fork (same API).
5. **Kafka consumer timeout misconfiguration**: `request_timeout_ms` must exceed
   `session_timeout_ms`.
6. **Python buffers stdout fully off-TTY** — `kubectl logs` showed nothing for any
   service until `PYTHONUNBUFFERED=1` was added to every Dockerfile.
7. **`kubectl patch` + `kubectl apply` interaction gap**: `apply`'s 3-way merge
   can't remove a field that `patch` added out-of-band (not in
   `last-applied-configuration`) — fault reset scripts switched to explicit
   `"$patch": "delete"` directives instead of reapplying the pristine manifest.
8. **PromQL `and` requires identical label sets by default** — the `PodOOMKilled`
   alert never fired until `and ignoring(reason)` was added.
9. **A fault's own injected latency delayed its readiness probe too** —
   `cpu_throttle`'s `LATENCY_MS` hook applied to `/readyz` as well as business
   routes, so the faulty pod never went Ready and the fault never showed up in any
   metric. Fixed by exempting health endpoints from the delay.
10. **kafka-python-ng only assigns partitions after `poll()` completes** — the
    `CONSUMER_STOPPED` fault path never polled, so lag was never computed. Fixed
    with an initial join-and-retry loop.
11. **Prometheus returns 4xx-with-JSON-body, Loki returns 4xx-with-plain-text-body**
    for bad queries — both tools originally called `raise_for_status()`/
    `resp.json()` unconditionally and crashed instead of returning a graceful tool
    error.
12. **Namespace conflation in `kube_read.py`**: the query namespace (whatever the
    model passed, e.g. a hallucinated `"default"`) was also used to mint the
    ServiceAccount token — but the SA only exists in the fixed `NAMESPACE`. Fixed
    by decoupling token-minting namespace from query namespace.
13. **A crashed background investigation vanished silently** — uncaught exceptions
    in `asyncio.create_task()` just disappear. Fixed with try/except that always
    writes a result, plus bounded LLM-error retry inside the orchestrator loop.
14. **Ollama's 120s default timeout too short for this hardware** — raised to 240s
    (see §5).
15. **`infra/webhook-receiver/app.py`'s `/alerts` 500'd on every request** after an
    ungraceful Docker crash corrupted one line of its JSONL log — fixed to decode
    leniently and skip unparseable lines.
16. **An uncaught exception during fault reset crashed the entire eval harness**
    mid-run, losing already-computed results — fixed with `_reset_with_retry()`
    (4 attempts, 20s backoff) plus try/except around each scenario.
17. **The model invented `root_cause_label` values** from the alert name or a
    variable name in evidence instead of picking a real root cause, because the
    prompt only gave examples and the schema accepted any string. Fixed with a
    closed `enum` in `agent/schema.py` + explicit instructions in
    `agent/prompts.py` (Phase 8's main fix — see `docs/failure-modes.md` for full
    detail).
18. **A replay-page JS bug**: Approve/Reject handlers set `innerHTML` directly then
    triggered a full re-render that wiped it. Fixed by moving decision state into
    the render function itself.

---

## 7. What's left / natural next steps (nothing is blocking, all optional)

The spec's 10 phases are all complete and the acceptance criteria for each were
met and re-verified. Nothing is "broken" or "incomplete" in the sense of blocking
further work. Natural next steps, roughly in order of value:

1. **Re-run the full `make eval`** (all 6 scenarios) now that Phase 8's prompt/
   schema fix is in place, to get a真 complete, current `evals/report.md` reflecting
   the fix across all scenarios, not just the 3 re-verified individually. Budget
   ~2-5 hours and expect to babysit Docker/Ollama stability per §5.
2. **Consider running the optional LLM-judge** (`evals/judge.py`, already built,
   `EVAL_LLM_JUDGE=true` with a second model like `qwen2.5:3b` pulled) and/or a
   3B-vs-7B model comparison (`LLM_MODEL=qwen2.5:3b make eval`) — both were
   deliberately skipped for time in the Phase 7 report but are fully implemented.
3. **Record the actual demo video** per `DEMO_SCRIPT.md` — update any spoken
   numbers to match whatever's in `evals/report.md` at recording time.
4. **Address the `wallclock_timeout` / `invalid_json_output` /
   `schema_validation_failed` failure modes still listed as "known, not fixed" in
   `docs/failure-modes.md`** — candidates: a JSON-mode/structured-output
   constraint if Ollama's API supports one for this model, or tightening budgets
   further.
5. **Deploy the replay site to GitHub Pages for real** (push to a GitHub remote,
   the workflow at `.github/workflows/deploy-replay.yml` handles the rest) —
   this repo has not yet been pushed to GitHub as of this handoff; only local git
   history exists (`main` branch, 13 commits, user `Jex2l`).
6. **The executor's write-side RBAC is explicitly deferred** (documented in
   `docs/customer-deployment.md`): it currently reuses the same local admin
   kubeconfig as everything else, since this is a single-user local demo. A real
   deployment should give it its own scoped ServiceAccount — the doc explains
   exactly what that Role should grant.

---

## 8. Environment specifics (useful for resuming locally)

- Working directory: `/Volumes/JEX2L/PROJECTS/INCIDENT_AGENT` (lives on an
  external SSD mounted at `/Volumes/JEX2L`, ~480GB free as of last check).
- kind cluster name: `incident-agent`, kubectl context: `kind-incident-agent`.
  **Never** touches any other kube context.
- Ollama models: `/Volumes/JEX2L/ollama-models` (set via Ollama.app's own Settings,
  not just an env var — see §5). Models present: `qwen2.5:7b` (4.7GB),
  `nomic-embed-text` (274MB).
- Docker Desktop: VM memory capped at ~7.75GB, disk image relocated to
  `/Volumes/JEX2L/docker-data`.
- Python env: `.venv/` via `uv` (falls back to `pip` if `uv` missing),
  `pyproject.toml` has all pinned deps. `make venv` sets it up.
- Key ports when running locally: Grafana `3000`, Prometheus `9090`, Alertmanager
  `9093`, webhook-receiver `8000` (all kind hostPort-mapped), Loki `3100` (no
  hostPort mapping on an *existing* cluster created before that was added to
  `kind-config.yaml` — scripts that need it self-manage a `kubectl port-forward`),
  agent webhook `8010`, executor `8020`, UI `8080`, trace viewer `4200`, replay
  `4000` (all via `make agent`/`make executor`/`make ui`/`make trace-viewer`/
  `make replay`).
- `make help` lists every available target.

---

## 9. How to verify the current state is still good

```bash
cd /Volumes/JEX2L/PROJECTS/INCIDENT_AGENT
git status --short              # should be clean
git log --oneline               # should show 13 commits ending in 4c8af88
kubectl --context kind-incident-agent -n incident-agent get pods   # all Running
source .venv/bin/activate && python3 -m pytest tests/ -q           # 69 passed
```

If the cluster isn't up (e.g. fresh machine restart), `make up` rebuilds it from
scratch — this has been done multiple times this session and always works cleanly
once Docker/Ollama are confirmed healthy first.

For full narrative detail beyond this summary, `docs/DECISIONS.md` is the complete,
chronological, phase-by-phase decision log — it's long but it's the ground truth
for *why* behind every choice summarized above.
