"""Webhook intake: Alertmanager posts here. Each firing alert that carries
our `root_cause_hint` annotation (set on all 6 incident-agent.* alert rules,
absent on kube-prometheus-stack's bundled default alerts like Watchdog or
NodeClockNotSynchronising) kicks off an investigation in the background and
responds immediately, so Alertmanager never blocks on the LLM.
"""
import asyncio
import json
import os
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request

from agent.budgets import Budget
from agent.mcp_client import ToolSet
from agent.orchestrator import investigate
from agent.schema import inconclusive_result

ROOT = Path(__file__).resolve().parent.parent
INCIDENTS_DIR = ROOT / "evals" / "runs"
INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)

KUBE_CONTEXT = os.environ.get("KUBE_CONTEXT", "kind-incident-agent")
_state: dict = {}


def _start_loki_port_forward() -> subprocess.Popen:
    """Loki has no kind hostPort mapping (unlike Grafana/Prometheus/
    Alertmanager), so log_search needs this running for the agent's
    lifetime. Same pattern as infra/verify-phase1.sh and the test fixture
    in tests/test_log_search.py."""
    return subprocess.Popen(
        ["kubectl", "--context", KUBE_CONTEXT, "-n", "monitoring", "port-forward", "svc/loki", "3100:3100"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _state["loki_pf"] = _start_loki_port_forward()
    async with ToolSet() as toolset:
        _state["toolset"] = toolset
        yield
    _state["loki_pf"].terminate()


app = FastAPI(title="incident-agent webhook", lifespan=lifespan)


def _alert_summary(alert: dict) -> dict:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    return {
        "alertname": labels.get("alertname"),
        "namespace": labels.get("namespace"),
        "severity": labels.get("severity"),
        "labels": labels,
        "summary": annotations.get("summary"),
        "description": annotations.get("description"),
        "root_cause_hint": annotations.get("root_cause_hint"),
        "starts_at": alert.get("startsAt"),
    }


async def _run_investigation(alert_summary: dict):
    incident_id = f"incident-{alert_summary.get('alertname', 'unknown')}-{int(time.time())}"
    try:
        result = await investigate(
            alert_summary,
            _state["toolset"],
            budget=Budget(),
            incident_id=incident_id,
        )
    except Exception as e:
        # This is a background task with no caller to propagate to - an
        # uncaught exception here (e.g. an LLM request timing out) would
        # otherwise just vanish into asyncio's default exception logging,
        # leaving no record at all that the incident was ever investigated.
        result = inconclusive_result(f"investigation crashed: {type(e).__name__}: {e}", [])
    record = {"incident_id": incident_id, "alert": alert_summary, "result": result, "completed_at": time.time()}
    (INCIDENTS_DIR / f"{incident_id}.json").write_text(json.dumps(record, indent=2, default=str))


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/webhook")
async def receive_alert(request: Request):
    payload = await request.json()
    started = []
    for alert in payload.get("alerts", []):
        if alert.get("status") != "firing":
            continue
        summary = _alert_summary(alert)
        if not summary["root_cause_hint"]:
            continue  # not one of our alerts (e.g. kube-prometheus-stack's Watchdog)
        asyncio.create_task(_run_investigation(summary))
        started.append(summary["alertname"])
    return {"status": "accepted", "investigations_started": started}


@app.get("/incidents")
def list_incidents(limit: int = 20):
    files = sorted(INCIDENTS_DIR.glob("incident-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {"incidents": [json.loads(p.read_text()) for p in files[:limit]]}
