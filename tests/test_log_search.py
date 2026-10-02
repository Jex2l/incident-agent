"""Tests for the log_search tool. Requires a live Loki. Loki has no kind
hostPort mapping (unlike Grafana/Prometheus/Alertmanager/webhook-receiver),
so this module manages its own kubectl port-forward for the test session,
same pattern as infra/verify-phase1.sh.
"""
import os
import subprocess
import time

import httpx
import pytest

from tools.log_search import normalize_line, search_logs

KUBE_CONTEXT = os.environ.get("KUBE_CONTEXT", "kind-incident-agent")


@pytest.fixture(scope="module", autouse=True)
def loki_port_forward():
    proc = subprocess.Popen(
        ["kubectl", "--context", KUBE_CONTEXT, "-n", "monitoring", "port-forward", "svc/loki", "3100:3100"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(20):
            try:
                if httpx.get("http://localhost:3100/ready", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            proc.terminate()
            pytest.skip("Loki not reachable via port-forward - is the cluster up?")
        yield
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_normalize_line_collapses_ids_and_numbers():
    a = normalize_line('INFO: 10.244.1.26:54268 - "GET /cart/123 HTTP/1.1" 200 OK')
    b = normalize_line('INFO: 10.244.1.9:39472 - "GET /cart/456 HTTP/1.1" 200 OK')
    assert a == b


def test_normalize_line_collapses_uuids():
    a = normalize_line("request 550e8400-e29b-41d4-a716-446655440000 failed")
    b = normalize_line("request 123e4567-e89b-12d3-a456-426614174000 failed")
    assert a == b


def test_search_logs_returns_pattern_summary_not_raw_dump():
    result = search_logs(namespace="incident-agent", pod="cart", since_minutes=30)
    assert result["total_lines"] >= 0
    assert "patterns" in result
    if result["patterns"]:
        pattern = result["patterns"][0]
        assert {"pattern", "count", "samples"} <= pattern.keys()
        # samples must be capped, not every matching line
        assert len(pattern["samples"]) <= 3


def test_search_logs_caps_pattern_count():
    from tools.log_search import MAX_PATTERNS
    result = search_logs(namespace="incident-agent", since_minutes=30)
    assert len(result["patterns"]) <= MAX_PATTERNS


def test_search_logs_with_query_text_filter():
    result = search_logs(namespace="incident-agent", query_text="GET", since_minutes=30)
    assert "GET" in result["query"]


def test_search_logs_handles_bad_logql_gracefully(monkeypatch):
    # Loki rejects a selector with no label matchers at all (our _build_selector
    # always adds namespace, so simulate a malformed query_text injection instead).
    import tools.log_search as ls
    monkeypatch.setattr(ls, "_build_selector", lambda *a, **k: "{malformed")
    result = search_logs(namespace="incident-agent", since_minutes=5)
    assert "error" in result
    assert result["patterns"] == []
