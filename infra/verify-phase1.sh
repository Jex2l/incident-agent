#!/usr/bin/env bash
# Phase 1 acceptance check:
#  1. app is healthy
#  2. metrics are queryable (Prometheus)
#  3. logs are queryable (Loki)
#  4. a manual fault makes an alert fire and reach the webhook receiver
set -uo pipefail

KUBE_CONTEXT="${1:-kind-incident-agent}"
PROM_URL="http://localhost:9090"
LOKI_URL="http://localhost:3100"
WEBHOOK_URL="http://localhost:8000"
PASS=0
FAIL=0

check() {
  local desc="$1"; shift
  if "$@"; then
    echo "  [ok] ${desc}"
    PASS=$((PASS+1))
  else
    echo "  [FAIL] ${desc}"
    FAIL=$((FAIL+1))
  fi
}

echo "== 1. App health =="
check "frontend pod ready" bash -c \
  "kubectl --context ${KUBE_CONTEXT} -n incident-agent get pods -l app=frontend -o jsonpath='{.items[0].status.containerStatuses[0].ready}' | grep -q true"
check "cart pod ready" bash -c \
  "kubectl --context ${KUBE_CONTEXT} -n incident-agent get pods -l app=cart -o jsonpath='{.items[0].status.containerStatuses[0].ready}' | grep -q true"

echo "== 2. Metrics queryable (Prometheus) =="
check "prometheus API reachable" bash -c "curl -sf ${PROM_URL}/-/healthy >/dev/null"
check "http_requests_total has samples" bash -c \
  "curl -sf '${PROM_URL}/api/v1/query?query=http_requests_total' | grep -q '\"result\":\[.\+\]'"

echo "== 3. Logs queryable (Loki) =="
check "loki API reachable" bash -c "curl -sf ${LOKI_URL}/ready >/dev/null"
check "loki has labels" bash -c "curl -sf '${LOKI_URL}/loki/api/v1/labels' | grep -q 'namespace'"

echo "== 4. Manual fault -> alert -> webhook =="
echo "  Scaling 'cache' to 0 replicas (dependency-down fault)..."
kubectl --context "${KUBE_CONTEXT}" -n incident-agent scale deployment/cache --replicas=0 >/dev/null

echo "  Waiting up to 4 minutes for HighErrorRate or DeploymentUnavailable alert to reach the webhook..."
FOUND=0
for i in $(seq 1 48); do
  if curl -sf "${WEBHOOK_URL}/alerts" 2>/dev/null | grep -qE "HighErrorRate|DeploymentUnavailable"; then
    FOUND=1
    break
  fi
  sleep 5
done
check "alert reached webhook receiver" test "${FOUND}" = "1"

echo "  Resetting: scaling 'cache' back to 1 replica..."
kubectl --context "${KUBE_CONTEXT}" -n incident-agent scale deployment/cache --replicas=1 >/dev/null
kubectl --context "${KUBE_CONTEXT}" -n incident-agent rollout status deployment/cache --timeout=120s >/dev/null

echo
echo "== Result: ${PASS} passed, ${FAIL} failed =="
[[ "${FAIL}" -eq 0 ]]
