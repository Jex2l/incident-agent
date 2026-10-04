SHELL := /bin/bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c

PROJECT_ROOT := $(shell pwd)
SSD_PATH ?= /Volumes/JEX2L
export OLLAMA_MODELS ?= $(SSD_PATH)/ollama-models
CLUSTER_NAME ?= incident-agent
KUBE_CONTEXT := kind-$(CLUSTER_NAME)
VENV := $(PROJECT_ROOT)/.venv
PY := $(VENV)/bin/python

.PHONY: help preflight venv up down faults eval ui agent replay test clean

help:
	@echo "incident-agent targets:"
	@echo "  make preflight  - check machine/tooling prerequisites"
	@echo "  make venv       - create Python venv and install deps (uv if available, else pip)"
	@echo "  make up         - preflight, create kind cluster, install observability, deploy target app"
	@echo "  make down       - tear down the kind cluster"
	@echo "  make faults     - run fault injection scenarios (requires 'make up')"
	@echo "  make agent      - start the agent's webhook intake server (requires 'make up')"
	@echo "  make eval       - run the eval harness end to end, writes evals/report.md"
	@echo "  make ui         - start the approval UI/API locally"
	@echo "  make replay     - serve the static replay site locally"
	@echo "  make test       - run pytest"

preflight:
	@bash infra/preflight.sh

venv:
	@if command -v uv >/dev/null 2>&1; then \
		echo "Using uv to create venv + install deps..."; \
		uv venv $(VENV); \
		uv pip install -p $(PY) -e ".[dev]"; \
	else \
		echo "uv not found, falling back to python3 -m venv + pip (slower)."; \
		python3 -m venv $(VENV); \
		$(PY) -m pip install --upgrade pip; \
		$(PY) -m pip install -e ".[dev]"; \
	fi
	@echo "Venv ready at $(VENV)"

up: preflight
	@echo "== Bringing up incident-agent cluster =="
	@mkdir -p "$(OLLAMA_MODELS)"
	@bash infra/kind/create-cluster.sh "$(CLUSTER_NAME)"
	@bash infra/install-observability.sh "$(KUBE_CONTEXT)"
	@bash infra/deploy-app.sh "$(KUBE_CONTEXT)"
	@echo "== Cluster up. kubectl context: $(KUBE_CONTEXT) =="

down:
	@if ! command -v kind >/dev/null 2>&1; then \
		echo "kind is not installed, so there is no cluster to delete."; \
	else \
		kind delete cluster --name "$(CLUSTER_NAME)" || echo "No cluster named $(CLUSTER_NAME) found."; \
	fi

faults: venv
	@$(PY) faults/run_all.py

eval: venv
	@$(PY) -m evals.runner

ui: venv
	@$(PY) -m uvicorn ui.app:app --reload --port 8080

agent: venv
	@$(PY) -m uvicorn agent.webhook:app --port 8010

replay:
	@echo "Serving replay/ at http://localhost:4000"
	@cd replay && python3 -m http.server 4000

test: venv
	@$(PY) -m pytest -q

clean:
	@rm -rf $(VENV) .pytest_cache **/__pycache__
