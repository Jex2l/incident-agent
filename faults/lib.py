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
    """Run a kubectl command against the fixed kind context and return stdout.

    Every fault script shells out through this one wrapper (instead of the
    kubernetes python client) so all cluster mutations are visible as plain
    kubectl invocations in logs, and so `check` gives callers an easy way to
    opt out of raising on non-zero exit (e.g. for best-effort cleanup).
    """
    cmd = ["kubectl", "--context", KUBE_CONTEXT] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout.strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_fault_start(scenario: str, component: str, root_cause_label: str, details: dict | None = None) -> tuple[str, Path]:
    """Write a ground-truth record to evals/runs/ marking a fault as just-injected.

    This is the timestamp evals/ later scores the agent's investigation
    against (how fast it found the right component/root cause relative to
    `injected_at`). Called at the start of inject(), before the cluster is
    actually mutated, so the clock starts as close as possible to the real
    fault onset.
    """
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
    """Find the most recently started run for `scenario` that hasn't been resolved yet.

    Sorting filenames in reverse works because run_id embeds a unix
    timestamp, so lexicographic order matches chronological order.
    """
    for path in sorted(RUNS_DIR.glob(f"{scenario}-*.json"), reverse=True):
        record = json.loads(path.read_text())
        if record.get("status") == "active":
            return path, record
    return None, None


def record_fault_end(scenario: str) -> Path | None:
    """Mark the active ground-truth record for `scenario` as resolved, stamping reset_at."""
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
            # Webhook receiver may not be reachable yet (e.g. still starting);
            # treat a failed poll as "no alerts this round" rather than aborting.
            print(f"[fault] webhook poll failed (will retry): {e}")
            data = {"alerts": []}
        for rec in data.get("alerts", []):
            # received_at filters out alerts from a previous run of the same
            # scenario that are still sitting in the webhook receiver's buffer.
            if rec["received_at"] < since_ts:
                continue
            payload = rec["payload"]
            if payload.get("status") != "firing":
                continue
            # Alertmanager batches multiple alerts per webhook payload; only
            # match one whose own status is firing and name is one we expect
            # (a payload can be "firing" overall while bundling a resolved one).
            for alert in payload.get("alerts", []):
                if alert.get("status") == "firing" and alert["labels"].get("alertname") in alertnames:
                    return alert
        time.sleep(poll_s)
    return None


def wait_rollout(deployment: str, timeout_s: int = 120):
    """Block until `deployment`'s rollout finishes, via `kubectl rollout status`.

    Used after reset() patches to confirm the deployment actually becomes
    healthy again rather than just assuming the patch succeeded.
    """
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
