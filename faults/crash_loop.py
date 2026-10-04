#!/usr/bin/env python3
"""Fault 5: crash loop from a bad env var.

Patches cart with FORCE_CRASH_ON_BOOT=true (a hook built into
target-app/cart/app.py: sys.exit(1) before the server starts listening),
so the container crashes immediately and repeatedly until kubelet's
restart backoff reports CrashLoopBackOff.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib

SCENARIO = "crash_loop"
COMPONENT = "cart"
ROOT_CAUSE_LABEL = "crash_loop_bad_config"
EXPECTED_ALERTS = ["CrashLoopBackOff"]

INJECT_PATCH = {
    "spec": {"template": {"spec": {"containers": [{
        "name": "cart",
        "env": [{"name": "FORCE_CRASH_ON_BOOT", "value": "true"}],
    }]}}}
}

RESET_PATCH = {
    "spec": {"template": {"spec": {"containers": [{
        "name": "cart",
        "env": [{"name": "FORCE_CRASH_ON_BOOT", "$patch": "delete"}],
    }]}}}
}


def inject():
    """Patch cart's Deployment to set FORCE_CRASH_ON_BOOT=true, after
    recording ground truth.

    Every new pod exits immediately on boot (before it can ever pass its
    readiness probe), so kubelet's restart backoff escalates and the
    container ends up in CrashLoopBackOff, which should fire that alert.
    """
    run_id, _ = lib.record_fault_start(
        SCENARIO, COMPONENT, ROOT_CAUSE_LABEL,
        {"force_crash_on_boot": True, "expected_alerts": EXPECTED_ALERTS},
    )
    lib.patch_deployment(COMPONENT, INJECT_PATCH)
    print(f"[{SCENARIO}] injected (run {run_id})")


def reset():
    """Remove FORCE_CRASH_ON_BOOT (via $patch: delete), wait for the rollout,
    and mark the ground-truth record resolved.
    """
    lib.patch_deployment(COMPONENT, RESET_PATCH)
    lib.wait_rollout(COMPONENT)
    lib.record_fault_end(SCENARIO)
    print(f"[{SCENARIO}] reset, cart restored to healthy")


def run() -> bool:
    """Full fault cycle: inject, poll up to 5 min for CrashLoopBackOff on the
    webhook receiver, reset regardless of outcome, and report pass/fail.
    """
    since = lib.now_iso()
    inject()
    print(f"[{SCENARIO}] waiting up to 5 min for {EXPECTED_ALERTS}...")
    alert = lib.wait_for_alert(EXPECTED_ALERTS, since, timeout_s=300)
    ok = alert is not None
    print(f"[{SCENARIO}] " + (f"alert FIRED: {alert['labels']['alertname']}" if ok else "alert DID NOT FIRE"))
    reset()
    return ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inject", "reset", "run"])
    args = parser.parse_args()
    if args.action == "inject":
        inject()
    elif args.action == "reset":
        reset()
    else:
        sys.exit(0 if run() else 1)
