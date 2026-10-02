"""Tests for the kube_read tool. Requires a live kind cluster with
infra/rbac/ applied (see infra/deploy-app.sh) - these are integration
tests, not mocks, because the thing being tested is enforcement by the
real Kubernetes API server.
"""
import pytest
from kubernetes import client

from tools.kube_read import (
    NAMESPACE,
    get_pod_logs,
    get_restricted_api_client,
    list_deployments,
    list_events,
    list_pods,
)


@pytest.fixture(scope="module")
def any_pod_name():
    result = list_pods()
    assert result["pods"], "expected at least one pod in the namespace to test against"
    return result["pods"][0]["name"]


def test_list_pods_returns_structured_data():
    result = list_pods()
    assert result["namespace"] == NAMESPACE
    assert isinstance(result["pods"], list)
    assert result["count"] >= 1
    pod = result["pods"][0]
    assert {"name", "phase", "node", "containers"} <= pod.keys()


def test_list_pods_caps_output():
    result = list_pods()
    assert isinstance(result["truncated"], bool)
    assert len(result["pods"]) <= 20


def test_get_pod_logs_returns_capped_lines(any_pod_name):
    result = get_pod_logs(any_pod_name, tail_lines=5)
    assert result["pod"] == any_pod_name
    assert "lines" in result
    assert len(result["lines"]) <= 5


def test_get_pod_logs_caps_at_max_even_if_more_requested(any_pod_name):
    from tools.kube_read import MAX_LOG_LINES
    result = get_pod_logs(any_pod_name, tail_lines=MAX_LOG_LINES * 10)
    assert len(result["lines"]) <= MAX_LOG_LINES


def test_list_events_returns_structured_data():
    result = list_events()
    assert result["namespace"] == NAMESPACE
    assert isinstance(result["events"], list)


def test_list_deployments_returns_structured_data():
    result = list_deployments()
    assert result["namespace"] == NAMESPACE
    assert result["count"] >= 1
    dep = result["deployments"][0]
    assert {"name", "desired_replicas", "available_replicas", "ready_replicas", "conditions"} <= dep.keys()


def test_restricted_client_cannot_delete_pods(any_pod_name):
    """The actual safety boundary: even calling a write method directly
    against the same restricted client the tool uses must be rejected by
    the API server (403), not merely absent from the tool's own code."""
    api_client = get_restricted_api_client()
    v1 = client.CoreV1Api(api_client)
    with pytest.raises(client.exceptions.ApiException) as exc_info:
        v1.delete_namespaced_pod(any_pod_name, NAMESPACE)
    assert exc_info.value.status == 403


def test_restricted_client_cannot_patch_deployments():
    api_client = get_restricted_api_client()
    apps_v1 = client.AppsV1Api(api_client)
    with pytest.raises(client.exceptions.ApiException) as exc_info:
        apps_v1.patch_namespaced_deployment("cart", NAMESPACE, {"spec": {"replicas": 99}})
    assert exc_info.value.status == 403


def test_restricted_client_cannot_read_secrets():
    """RBAC explicitly excludes secrets, separate from the write ban."""
    api_client = get_restricted_api_client()
    v1 = client.CoreV1Api(api_client)
    with pytest.raises(client.exceptions.ApiException) as exc_info:
        v1.list_namespaced_secret(NAMESPACE)
    assert exc_info.value.status == 403
