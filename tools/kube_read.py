"""kube_read MCP tool: read-only Kubernetes access for the agent.

Safety is enforced by infrastructure, not by this code: every call goes
through a kubernetes.client.ApiClient authenticated as the
incident-agent-reader ServiceAccount (infra/rbac/), which has a Role
granting only get/list/watch on pods, pods/log, events, services,
deployments, and replicasets - no secrets, no write verbs on anything. Even
a buggy tool function cannot perform a write; the API server rejects it
with 403 regardless of what this code tries to do. See
tests/test_kube_read.py for a test that proves this directly.

Returns trimmed, structured JSON - pod/deployment listings are capped and
reduced to the fields that matter for triage, not the full API object; logs
are tail-limited server-side via the Kubernetes API itself.
"""
import os
import subprocess
import sys
from pathlib import Path

from kubernetes import client, config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import cap_list

KUBE_CONTEXT = os.environ.get("KUBE_CONTEXT", "kind-incident-agent")
NAMESPACE = os.environ.get("INCIDENT_AGENT_NAMESPACE", "incident-agent")
SERVICE_ACCOUNT = os.environ.get("KUBE_READ_SERVICE_ACCOUNT", "incident-agent-reader")
MAX_ITEMS = int(os.environ.get("KUBE_READ_MAX_ITEMS", "20"))
MAX_LOG_LINES = int(os.environ.get("KUBE_READ_MAX_LOG_LINES", "100"))


def _get_sa_token(service_account: str = SERVICE_ACCOUNT, duration: str = "1h") -> str:
    # The ServiceAccount always lives in NAMESPACE (where infra/rbac/ created
    # it) - this is NOT the namespace a tool call queries. A pod/log/event
    # listing can target any namespace the model asks for (list_pods(namespace=...)
    # etc.), but the token must always be minted from where the SA actually is.
    result = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE, "create", "token", service_account, f"--duration={duration}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to mint token for {service_account}: {result.stderr}")
    return result.stdout.strip()


def get_restricted_api_client(service_account: str = SERVICE_ACCOUNT) -> client.ApiClient:
    """Build an ApiClient authenticated as the read-only ServiceAccount's
    own token - NOT the admin kubeconfig identity used to mint it."""
    config.load_kube_config(context=KUBE_CONTEXT)
    base = client.Configuration.get_default_copy()
    token = _get_sa_token(service_account)
    restricted = client.Configuration()
    restricted.host = base.host
    restricted.ssl_ca_cert = base.ssl_ca_cert
    restricted.verify_ssl = True
    restricted.api_key = {"authorization": f"Bearer {token}"}
    restricted.api_key_prefix = {}
    return client.ApiClient(restricted)


def _container_summary(cs) -> dict:
    state = "unknown"
    reason = None
    if cs.state.running:
        state = "running"
    elif cs.state.waiting:
        state = "waiting"
        reason = cs.state.waiting.reason
    elif cs.state.terminated:
        state = "terminated"
        reason = cs.state.terminated.reason
    return {
        "name": cs.name,
        "ready": cs.ready,
        "restart_count": cs.restart_count,
        "state": state,
        "reason": reason,
        "image": cs.image,
    }


def list_pods(namespace: str = NAMESPACE, label_selector: str | None = None) -> dict:
    api_client = get_restricted_api_client()
    v1 = client.CoreV1Api(api_client)
    try:
        pods = v1.list_namespaced_pod(namespace, label_selector=label_selector or "").items
    except client.exceptions.ApiException as e:
        return {"namespace": namespace, "error": f"{e.status} {e.reason}"}
    items = [
        {
            "name": p.metadata.name,
            "phase": p.status.phase,
            "node": p.spec.node_name,
            "containers": [_container_summary(cs) for cs in (p.status.container_statuses or [])],
        }
        for p in pods
    ]
    items, truncated = cap_list(items, MAX_ITEMS)
    return {"namespace": namespace, "count": len(pods), "truncated": truncated, "pods": items}


def get_pod_logs(pod: str, namespace: str = NAMESPACE, container: str | None = None, tail_lines: int = MAX_LOG_LINES) -> dict:
    api_client = get_restricted_api_client()
    v1 = client.CoreV1Api(api_client)
    tail_lines = min(tail_lines, MAX_LOG_LINES)
    try:
        log_text = v1.read_namespaced_pod_log(pod, namespace, container=container, tail_lines=tail_lines)
    except client.exceptions.ApiException as e:
        return {"pod": pod, "namespace": namespace, "error": f"{e.status} {e.reason}"}
    lines = log_text.splitlines()
    return {"pod": pod, "namespace": namespace, "container": container, "line_count": len(lines), "lines": lines}


def list_events(namespace: str = NAMESPACE, involved_object_name: str | None = None) -> dict:
    api_client = get_restricted_api_client()
    v1 = client.CoreV1Api(api_client)
    field_selector = f"involvedObject.name={involved_object_name}" if involved_object_name else None
    try:
        events = v1.list_namespaced_event(namespace, field_selector=field_selector).items
    except client.exceptions.ApiException as e:
        return {"namespace": namespace, "error": f"{e.status} {e.reason}"}
    events = sorted(events, key=lambda e: e.last_timestamp or e.event_time or "", reverse=True)
    items = [
        {
            "type": e.type,
            "reason": e.reason,
            "message": e.message,
            "count": e.count,
            "last_timestamp": str(e.last_timestamp) if e.last_timestamp else None,
            "involved_object": f"{e.involved_object.kind}/{e.involved_object.name}",
        }
        for e in events
    ]
    items, truncated = cap_list(items, MAX_ITEMS)
    return {"namespace": namespace, "count": len(events), "truncated": truncated, "events": items}


def list_deployments(namespace: str = NAMESPACE) -> dict:
    api_client = get_restricted_api_client()
    apps_v1 = client.AppsV1Api(api_client)
    try:
        deployments = apps_v1.list_namespaced_deployment(namespace).items
    except client.exceptions.ApiException as e:
        return {"namespace": namespace, "error": f"{e.status} {e.reason}"}
    items = [
        {
            "name": d.metadata.name,
            "desired_replicas": d.spec.replicas,
            "available_replicas": d.status.available_replicas or 0,
            "ready_replicas": d.status.ready_replicas or 0,
            "conditions": [
                {"type": c.type, "status": c.status, "reason": c.reason}
                for c in (d.status.conditions or [])
            ],
        }
        for d in deployments
    ]
    items, truncated = cap_list(items, MAX_ITEMS)
    return {"namespace": namespace, "count": len(deployments), "truncated": truncated, "deployments": items}


def build_server():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("kube_read")

    @mcp.tool()
    def kube_list_pods(namespace: str = NAMESPACE, label_selector: str | None = None) -> dict:
        """List pods in a namespace with phase, node, and per-container
        ready/restart/state summary. Read-only (ServiceAccount-enforced)."""
        return list_pods(namespace, label_selector)

    @mcp.tool()
    def kube_get_pod_logs(pod: str, namespace: str = NAMESPACE, container: str | None = None, tail_lines: int = MAX_LOG_LINES) -> dict:
        """Get the last N lines of a pod's logs. Read-only (ServiceAccount-enforced)."""
        return get_pod_logs(pod, namespace, container, tail_lines)

    @mcp.tool()
    def kube_list_events(namespace: str = NAMESPACE, involved_object_name: str | None = None) -> dict:
        """List recent Kubernetes events in a namespace, optionally filtered
        to one object (e.g. a pod name). Read-only (ServiceAccount-enforced)."""
        return list_events(namespace, involved_object_name)

    @mcp.tool()
    def kube_list_deployments(namespace: str = NAMESPACE) -> dict:
        """List deployments in a namespace with replica counts and
        conditions. Read-only (ServiceAccount-enforced)."""
        return list_deployments(namespace)

    return mcp


if __name__ == "__main__":
    build_server().run()
