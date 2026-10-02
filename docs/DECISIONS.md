# Decisions log

Running log of non-obvious choices made autonomously while building incident-agent, per the project's working rules (ask only when truly blocked; otherwise decide, record, continue).

## 2026-10-02 — Phase 0

- **External SSD for Docker/Ollama data.** Internal disk had only ~18GB free (228GB volume, mostly consumed by system data). User has a 1TB external SSD mounted at `/Volumes/JEX2L` with ~492GB free — this project already lives there. Decision: point Ollama model storage at `/Volumes/JEX2L/ollama-models` via `OLLAMA_MODELS` (exported by the Makefile for all `make` targets) and recommend the user set Docker Desktop's VM disk image location to `/Volumes/JEX2L/docker-data` (a GUI setting I cannot set programmatically). Documented in `.env.example` and `infra/preflight.sh`.
- **Default LLM model kept at `qwen2.5:7b`** per spec, configurable via `LLM_MODEL`. Machine has 16GB RAM / Apple M2 (Metal, no CUDA) which can run a 7B q4 model adequately via Ollama. Noted in preflight output: if RAM/disk pressure appears during eval runs, fall back to `qwen2.5:3b`.
- **uv preferred, pip fallback.** `make venv` uses `uv` if present (faster, better lockfile behavior) and transparently falls back to `python3 -m venv` + `pip` if not installed, so the project isn't blocked on one more missing tool.
- **kind cluster topology: 1 control-plane + 1 worker**, with hostPort mappings for Grafana (3000), Prometheus (9090), Alertmanager (9093), and the webhook receiver (8000) baked into `infra/kind/kind-config.yaml` — avoids needing `kubectl port-forward` running in the background for day-to-day demo use.
- **Phase 1 scripts stubbed, not implemented.** `infra/install-observability.sh` and `infra/deploy-app.sh` exist as clearly-labeled placeholders that exit 2 with a "not implemented yet (Phase 1)" message, rather than being half-built. `make up` will stop earlier at `preflight` anyway since Docker/kind/kubectl/helm are not yet installed on this machine.

## 2026-10-02 — Phase 1

- **Redpanda deployed as a plain single-node Deployment, not via the Redpanda Operator/Helm chart.** The operator brings its own CRDs and a cert-manager dependency, which is unnecessary weight for one dev-mode broker on a resource-constrained kind cluster. Uses `redpanda start --mode=dev-container` with `--overprovisioned` on the official image. The spec's "installed via Helm" requirement was read as applying to the Prometheus/Alertmanager/Grafana/Loki+Promtail observability stack specifically, which is Helm-installed.
- **Frontend runs its own background traffic generator** (an asyncio task calling the cart service every ~2s) instead of requiring a human to click through the UI. This gives Prometheus continuous baseline metrics so fault-injection deltas (error rate, latency) are visible without a manual load generator process.
- **Fault hooks built into cart/kafka-consumer app code now, used in Phase 2**: `FORCE_CRASH_ON_BOOT`, `MEMORY_BALLAST_MB`, `LATENCY_MS` on cart; `CONSUMER_DELAY_MS`, `CONSUMER_STOPPED` on kafka-consumer. Building these into the apps now means Phase 2's fault scripts can just patch a Deployment's env vars via `kubectl set env` rather than needing custom crash/leak code injected at fault time.
- **Kafka consumer lag is computed from real consumer-group offsets** (`KafkaConsumer.end_offsets()` minus `position()` per partition, summed) rather than a synthetic counter, so the `KafkaConsumerLagHigh` alert reflects genuine Kafka introspection.
- **ServiceMonitors/PrometheusRules don't carry the chart's release label**; instead `kube-prometheus-stack-values.yaml` sets `serviceMonitorSelectorNilUsesHelmValues/ruleSelectorNilUsesHelmValues: false` so Prometheus Operator picks up any ServiceMonitor/PrometheusRule cluster-wide. Simpler than threading Helm release labels through every manifest.
- **Grafana is installed once** (via kube-prometheus-stack) and Loki is wired into it as a second datasource via a labeled ConfigMap, rather than installing loki-stack's own bundled Grafana — avoids running two Grafana instances.

## 2026-10-02 — Phase 1 verification (live cluster)

Docker/kind/kubectl/helm were installed and Docker's VM disk relocated to the SSD (`/Volumes/JEX2L/docker-data`) by the user. Ran the full Phase 1 stack for real and fixed five bugs surfaced only by actually running it:

1. **Grafana OOMKilled on boot.** Its memory limit (192Mi) was too low for Grafana 13.x plus two sidecar containers (`grafana-sc-dashboard`, `grafana-sc-datasources`). Raised Grafana to 400Mi limit, Alertmanager to 256Mi, Prometheus to 1Gi, and gave the sidecars their own resource block, since Docker Desktop's VM actually has ~15GB available (not a reason to under-provision).
2. **Alertmanager config rejected at startup**: `undefined receiver "null" used in route`. kube-prometheus-stack's default rule bundle includes a permanently-firing "Watchdog" heartbeat alert routed to a receiver literally named `null` by convention; overriding `alertmanager.config.receivers` without keeping that entry breaks config validation. Added `- name: "null"` back to the receivers list.
3. **`deploy-app.sh` used `declare -A` (bash 4+ associative arrays)**, but macOS ships bash 3.2 by default (`/bin/bash`) for licensing reasons. Rewrote as a plain newline-delimited "name path" list piped through `while read`.
4. **`kafka-python` 2.0.2 is unmaintained and broken on Python 3.12** (`ModuleNotFoundError: No module named 'kafka.vendor.six.moves'`). Switched both kafka-producer and kafka-consumer to `kafka-python-ng==2.2.3`, the actively maintained fork with the same `import kafka` API — a drop-in replacement, not a rewrite.
5. **Kafka consumer crash-looped**: `KafkaConfigurationError: Request timeout (6000) must be larger than session timeout (10000)`. The client requires `request_timeout_ms > session_timeout_ms` (default 10000); mine was 6000. Raised to 20000.
6. **All Python services' logs were invisible** (`kubectl logs` showed nothing, even though the processes were working fine per their `/metrics`) because Python buffers stdout fully when it isn't a TTY. Added `ENV PYTHONUNBUFFERED=1` to all 5 Dockerfiles — necessary groundwork for Phase 3's log_search tool too.
7. **Loki has no kind hostPort mapping** (only Grafana/Prometheus/Alertmanager/webhook-receiver did), so `curl http://localhost:3100` failed from the host even though Loki was healthy inside the cluster. Fixed `verify-phase1.sh` to open its own `kubectl port-forward` for the duration of the check (self-sufficient regardless of kind-config), and added a NodePort(30100)/hostPort(3100) mapping to `kind-config.yaml` + `loki-stack-values.yaml` for direct access on future cluster creations (not retroactive — an existing cluster needs recreating to pick it up).

**Real result after fixes: `infra/verify-phase1.sh` passes 7/7** against the live cluster — pod health, Prometheus query, Loki query + real log content, and a manually-triggered `cache` scale-to-0 fault producing a `HighErrorRate` alert that reached the webhook receiver's JSONL log.

**Deferred**: kube-prometheus-stack's bundled default alert rules (e.g. `NodeClockNotSynchronising`) also reach the webhook alongside our 6 app-specific alerts. Harmless for Phase 1, but before Phase 4 wires the agent to this webhook, either disable `defaultRules` in the Helm values or have the agent filter to known `root_cause_hint`-bearing alerts, so the agent isn't triaging infra noise.

## Open / deferred

- Docker Desktop, kind, kubectl, helm, uv are not installed on the dev machine as of Phase 0. User was given exact `brew install` commands. Phases 1+ that need a live cluster cannot be verified until these are installed; such results will be marked "unverified" per working rule #2 until then.
