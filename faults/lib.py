"""Shared helpers for fault injection scripts: kubectl wrapper, ground-truth
record read/write, and polling the webhook receiver for a matching alert.
"""
import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "evals" / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

KUBE_CONTEXT = os.environ.get("KUBE_CONTEXT", "kind-incident-agent")
NAMESPACE = os.environ.get("INCIDENT_AGENT_NAMESPACE", "incident-agent")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "http://localhost:8000")


def kubectl(*args, check=True):
    cmd = ["kubectl", "--context", KUBE_CONTEXT] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout.strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_fault_start(scenario: str, component: str, root_cause_label: str, details: dict | None = None) -> tuple[str, Path]:
    run_id = f"{scenario}-{int(time.time())}"
    record = {
        "run_id": run_id,
        "scenario": scenario,
        "component": component,
        "root_cause_label": root_cause_label,
        "namespace": NAMESPACE,
        "injected_at": now_iso(),
        "reset_at": None,
        "status": "active",
        "details": details or {},
    }
    path = RUNS_DIR / f"{run_id}.json"
    path.write_text(json.dumps(record, indent=2))
    print(f"[fault] ground truth recorded: {path.relative_to(ROOT)}")
    return run_id, path


def latest_active_record(scenario: str) -> tuple[Path, dict] | tuple[None, None]:
    for path in sorted(RUNS_DIR.glob(f"{scenario}-*.json"), reverse=True):
        record = json.loads(path.read_text())
        if record.get("status") == "active":
            return path, record
    return None, None


def record_fault_end(scenario: str) -> Path | None:
    path, record = latest_active_record(scenario)
    if path is None:
        print(f"[fault] WARNING: no active ground-truth record found for '{scenario}'")
        return None
    record["reset_at"] = now_iso()
    record["status"] = "resolved"
    path.write_text(json.dumps(record, indent=2))
    print(f"[fault] ground truth marked resolved: {path.relative_to(ROOT)}")
    return path


def wait_for_alert(alertnames: list[str], since_iso: str, timeout_s: int = 300, poll_s: int = 5) -> dict | None:
    """Poll the webhook receiver for a firing alert in alertnames received after since_iso."""
    since_ts = datetime.fromisoformat(since_iso).timestamp()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{WEBHOOK_URL}/alerts?limit=50", timeout=5) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"[fault] webhook poll failed (will retry): {e}")
            data = {"alerts": []}
        for rec in data.get("alerts", []):
            if rec["received_at"] < since_ts:
                continue
            payload = rec["payload"]
            if payload.get("status") != "firing":
                continue
            for alert in payload.get("alerts", []):
                if alert.get("status") == "firing" and alert["labels"].get("alertname") in alertnames:
                    return alert
        time.sleep(poll_s)
    return None


def wait_rollout(deployment: str, timeout_s: int = 120):
    kubectl("-n", NAMESPACE, "rollout", "status", f"deployment/{deployment}", f"--timeout={timeout_s}s")


def patch_deployment(deployment: str, patch: dict):
    """Apply a strategic-merge patch to a deployment's pod template.

    Fault scripts use this for both inject and reset. Reset patches that
    need to remove an env var added by inject (rather than overwrite it)
    must use the strategic-merge "$patch: delete" directive, e.g.
    {"name": "FOO", "$patch": "delete"} - plain `kubectl apply` of the
    original manifest does NOT work for this: since `patch` mutates the
    live object without updating its last-applied-configuration
    annotation, `apply`'s 3-way merge (which diffs last-applied vs.
    new-desired to decide what to remove) never sees the added field and
    silently leaves it in place.
    """
    kubectl("-n", NAMESPACE, "patch", "deployment", deployment, "--type=strategic", "-p", json.dumps(patch))
