#!/usr/bin/env bash
# Installs Prometheus/Alertmanager/Grafana (kube-prometheus-stack) and
# Loki+Promtail (loki-stack) via Helm, with minimal resource requests.
set -euo pipefail

KUBE_CONTEXT="${1:?usage: install-observability.sh <kube-context>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VALUES_DIR="${SCRIPT_DIR}/helm-values"

if ! command -v helm >/dev/null 2>&1; then
  echo "[error] helm is not installed. Run: brew install helm" >&2
  exit 1
fi

echo "[observability] Using kube context: ${KUBE_CONTEXT}"
kubectl config use-context "${KUBE_CONTEXT}" >/dev/null

echo "[observability] Adding Helm repos..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null

kubectl --context "${KUBE_CONTEXT}" create namespace monitoring --dry-run=client -o yaml | kubectl --context "${KUBE_CONTEXT}" apply -f -
kubectl --context "${KUBE_CONTEXT}" create namespace incident-agent --dry-run=client -o yaml | kubectl --context "${KUBE_CONTEXT}" apply -f -

echo "[observability] Installing kube-prometheus-stack (Prometheus + Alertmanager + Grafana)..."
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --kube-context "${KUBE_CONTEXT}" \
  --namespace monitoring \
  -f "${VALUES_DIR}/kube-prometheus-stack-values.yaml" \
  --wait --timeout 10m

echo "[observability] Installing Loki + Promtail..."
helm upgrade --install loki grafana/loki-stack \
  --kube-context "${KUBE_CONTEXT}" \
  --namespace monitoring \
  -f "${VALUES_DIR}/loki-stack-values.yaml" \
  --wait --timeout 10m

echo "[observability] Wiring Loki into Grafana as a datasource..."
kubectl --context "${KUBE_CONTEXT}" apply -f "${VALUES_DIR}/grafana-loki-datasource.yaml"

echo "[observability] Done. Grafana: http://localhost:3000 (admin/admin)"
echo "[observability]       Prometheus: http://localhost:9090"
echo "[observability]       Alertmanager: http://localhost:9093"
