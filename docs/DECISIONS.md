# Decisions log

Running log of non-obvious choices made autonomously while building incident-agent, per the project's working rules (ask only when truly blocked; otherwise decide, record, continue).

## 2026-10-02 — Phase 0

- **External SSD for Docker/Ollama data.** Internal disk had only ~18GB free (228GB volume, mostly consumed by system data). User has a 1TB external SSD mounted at `/Volumes/JEX2L` with ~492GB free — this project already lives there. Decision: point Ollama model storage at `/Volumes/JEX2L/ollama-models` via `OLLAMA_MODELS` (exported by the Makefile for all `make` targets) and recommend the user set Docker Desktop's VM disk image location to `/Volumes/JEX2L/docker-data` (a GUI setting I cannot set programmatically). Documented in `.env.example` and `infra/preflight.sh`.
- **Default LLM model kept at `qwen2.5:7b`** per spec, configurable via `LLM_MODEL`. Machine has 16GB RAM / Apple M2 (Metal, no CUDA) which can run a 7B q4 model adequately via Ollama. Noted in preflight output: if RAM/disk pressure appears during eval runs, fall back to `qwen2.5:3b`.
- **uv preferred, pip fallback.** `make venv` uses `uv` if present (faster, better lockfile behavior) and transparently falls back to `python3 -m venv` + `pip` if not installed, so the project isn't blocked on one more missing tool.
- **kind cluster topology: 1 control-plane + 1 worker**, with hostPort mappings for Grafana (3000), Prometheus (9090), Alertmanager (9093), and the webhook receiver (8000) baked into `infra/kind/kind-config.yaml` — avoids needing `kubectl port-forward` running in the background for day-to-day demo use.
- **Phase 1 scripts stubbed, not implemented.** `infra/install-observability.sh` and `infra/deploy-app.sh` exist as clearly-labeled placeholders that exit 2 with a "not implemented yet (Phase 1)" message, rather than being half-built. `make up` will stop earlier at `preflight` anyway since Docker/kind/kubectl/helm are not yet installed on this machine.

## Open / deferred

- Docker Desktop, kind, kubectl, helm, uv are not installed on the dev machine as of Phase 0. User was given exact `brew install` commands. Phases 1+ that need a live cluster cannot be verified until these are installed; such results will be marked "unverified" per working rule #2 until then.
