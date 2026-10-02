#!/usr/bin/env bash
# Builds the target-app images, loads them into kind, and deploys the sample
# app, Redpanda, the webhook receiver, and alert rules.
set -euo pipefail

KUBE_CONTEXT="${1:?usage: deploy-app.sh <kube-context>}"
CLUSTER_NAME="${KUBE_CONTEXT#kind-}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v kind >/dev/null 2>&1; then
  echo "[error] kind is not installed. Run: brew install kind" >&2
  exit 1
fi

kubectl config use-context "${KUBE_CONTEXT}" >/dev/null

echo "[deploy-app] Building images..."
# Plain "name path" pairs instead of an associative array: macOS ships bash
# 3.2 by default, which predates bash 4's declare -A.
IMAGES="
cart target-app/cart
frontend target-app/frontend
kafka-producer target-app/kafka-producer
kafka-consumer target-app/kafka-consumer
webhook-receiver infra/webhook-receiver
"
echo "${IMAGES}" | while read -r name path; do
  [ -z "${name}" ] && continue
  ctx="${ROOT_DIR}/${path}"
  echo "  building incident-agent/${name}:dev from ${ctx}"
  docker build -q -t "incident-agent/${name}:dev" "${ctx}" >/dev/null
  echo "  loading incident-agent/${name}:dev into kind cluster '${CLUSTER_NAME}'"
  kind load docker-image "incident-agent/${name}:dev" --name "${CLUSTER_NAME}"
done

echo "[deploy-app] Applying namespace..."
kubectl --context "${KUBE_CONTEXT}" apply -f "${ROOT_DIR}/target-app/k8s/namespace.yaml"

echo "[deploy-app] Deploying Redpanda..."
kubectl --context "${KUBE_CONTEXT}" apply -f "${ROOT_DIR}/infra/redpanda/redpanda.yaml"

echo "[deploy-app] Deploying webhook receiver..."
kubectl --context "${KUBE_CONTEXT}" apply -f "${ROOT_DIR}/infra/webhook-receiver/k8s.yaml"

echo "[deploy-app] Deploying target app (cache, cart, frontend, kafka producer/consumer)..."
kubectl --context "${KUBE_CONTEXT}" apply -f "${ROOT_DIR}/target-app/k8s/cache.yaml"
kubectl --context "${KUBE_CONTEXT}" apply -f "${ROOT_DIR}/target-app/k8s/cart.yaml"
kubectl --context "${KUBE_CONTEXT}" apply -f "${ROOT_DIR}/target-app/k8s/frontend.yaml"
kubectl --context "${KUBE_CONTEXT}" apply -f "${ROOT_DIR}/target-app/k8s/kafka-producer.yaml"
kubectl --context "${KUBE_CONTEXT}" apply -f "${ROOT_DIR}/target-app/k8s/kafka-consumer.yaml"

echo "[deploy-app] Applying ServiceMonitors (requires Prometheus Operator CRDs)..."
kubectl --context "${KUBE_CONTEXT}" apply -f "${ROOT_DIR}/target-app/k8s/servicemonitors.yaml"

echo "[deploy-app] Applying alert rules..."
kubectl --context "${KUBE_CONTEXT}" apply -f "${ROOT_DIR}/infra/alert-rules/alert-rules.yaml"

echo "[deploy-app] Waiting for rollouts..."
for dep in cache cart frontend kafka-producer kafka-consumer redpanda webhook-receiver; do
  kubectl --context "${KUBE_CONTEXT}" -n incident-agent rollout status deployment/"${dep}" --timeout=180s
done

echo "[deploy-app] Done."
echo "[deploy-app] webhook-receiver: http://localhost:8000/alerts"
echo "[deploy-app] frontend has no host port mapping; use: kubectl -n incident-agent port-forward svc/frontend 8081:8080"
