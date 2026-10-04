"""Minimal Alertmanager webhook receiver.

Phase 1 scope: prove alerts actually reach something outside Alertmanager.
Logs every payload to stdout and appends it to an append-only JSONL file so
Phase 4's agent orchestrator (or a verification script) can read it back.
"""
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request

LOG_PATH = Path(os.environ.get("ALERT_LOG_PATH", "/data/alerts.jsonl"))
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="webhook-receiver")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/alerts")
async def receive_alert(request: Request):
    payload = await request.json()
    record = {"received_at": time.time(), "payload": payload}
    print(f"[webhook-receiver] received alert group with {len(payload.get('alerts', []))} alert(s)")
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return {"status": "received"}


@app.get("/alerts")
def list_alerts(limit: int = 20):
    if not LOG_PATH.exists():
        return {"alerts": []}
    # errors="replace": a prior ungraceful shutdown (observed in practice -
    # this project's cluster runs under enough CPU contention that Docker
    # Desktop itself has crashed mid-write) can leave partial/binary bytes
    # at the end of an append-only file. Decode leniently and skip any line
    # that still doesn't parse as JSON, rather than let one bad line 500 the
    # whole endpoint.
    text = LOG_PATH.read_bytes().decode("utf-8", errors="replace")
    lines = text.strip().splitlines()
    records = []
    for line in lines[-limit:]:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"alerts": records}
