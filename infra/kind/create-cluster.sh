#!/usr/bin/env bash
# Creates (or reuses) the kind cluster for incident-agent. Never touches other kube contexts.
set -euo pipefail

CLUSTER_NAME="${1:-incident-agent}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v kind >/dev/null 2>&1; then
  echo "[error] kind is not installed. Run: brew install kind" >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "[error] Docker is not running. Install/launch Docker Desktop, then retry." >&2
  exit 1
fi

if kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
  echo "[kind] Cluster '${CLUSTER_NAME}' already exists, reusing it."
else
  echo "[kind] Creating cluster '${CLUSTER_NAME}'..."
  kind create cluster --name "${CLUSTER_NAME}" --config "${SCRIPT_DIR}/kind-config.yaml"
fi

kubectl config use-context "kind-${CLUSTER_NAME}"
echo "[kind] kubectl context set to kind-${CLUSTER_NAME}"
kubectl cluster-info --context "kind-${CLUSTER_NAME}"
