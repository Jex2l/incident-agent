#!/usr/bin/env python3
"""Fault 1: OOM kill via low memory limit.

Patches cart to hold a memory ballast (MEMORY_BALLAST_MB, a hook built into
target-app/cart/app.py) while dropping its memory limit below that size, so
the kubelet OOMKills the container deterministically on startup rather than
relying on organic memory growth under load.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib

SCENARIO = "oom_kill"
COMPONENT = "cart"
ROOT_CAUSE_LABEL = "oom_kill"
EXPECTED_ALERTS = ["PodOOMKilled"]

# Original values from target-app/k8s/cart.yaml, needed to undo the patch precisely.
ORIGINAL_MEMORY_LIMIT = "128Mi"
ORIGINAL_MEMORY_REQUEST = "48Mi"

INJECT_PATCH = {
    "spec": {"template": {"spec": {"containers": [{
        "name": "cart",
        "env": [{"name": "MEMORY_BALLAST_MB", "value": "300"}],
        "resources": {
            "limits": {"memory": "64Mi"},
            "requests": {"memory": "48Mi"},
        },
    }]}}}
}

RESET_PATCH = {
    "spec": {"template": {"spec": {"containers": [{
        "name": "cart",
        "env": [{"name": "MEMORY_BALLAST_MB", "$patch": "delete"}],
        "resources": {
            "limits": {"memory": ORIGINAL_MEMORY_LIMIT},
            "requests": {"memory": ORIGINAL_MEMORY_REQUEST},
        },
    }]}}}
}


def inject():
    """Patch cart's Deployment: add MEMORY_BALLAST_MB=300 and lower its memory
    limit to 64Mi (below the ballast size), and record ground truth first.

    The ballast env var tells cart's app code to allocate ~300Mi of memory on
    boot; since the container's limit is only 64Mi, the kubelet kills it for
    exceeding its cgroup memory limit almost immediately and deterministically,
    which should fire PodOOMKilled.
    """
    run_id, _ = lib.record_fault_start(
        SCENARIO, COMPONENT, ROOT_CAUSE_LABEL,
        {"memory_ballast_mb": 300, "memory_limit": "64Mi", "expected_alerts": EXPECTED_ALERTS},
    )
    lib.patch_deployment(COMPONENT, INJECT_PATCH)
    print(f"[{SCENARIO}] injected (run {run_id})")


def reset():
    """Remove MEMORY_BALLAST_MB (via $patch: delete) and restore cart's
    original memory limit/request, then wait for the rollout and mark the
    ground-truth record resolved.
    """
    lib.patch_deployment(COMPONENT, RESET_PATCH)
    lib.wait_rollout(COMPONENT)
    lib.record_fault_end(SCENARIO)
    print(f"[{SCENARIO}] reset, cart restored to healthy")


def run() -> bool:
    """Full fault cycle: inject, poll up to 5 min for PodOOMKilled on the
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
