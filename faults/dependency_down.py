#!/usr/bin/env python3
"""Fault 6: dependency down (cache scaled to 0).

Scales cache (redis) to 0 replicas. cart's own pod keeps running, but its
readiness probe (/readyz, which pings redis) starts failing, so cart drops
out of its Service's endpoints and the cart Deployment's available replica
count goes to 0 - a realistic "my dependency is down" failure mode, visible
as DeploymentUnavailable on cart even though the root cause is cache.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib

SCENARIO = "dependency_down"
COMPONENT = "cache"  # root cause location
AFFECTED_COMPONENT = "cart"  # where the symptom is visible
ROOT_CAUSE_LABEL = "dependency_down"
EXPECTED_ALERTS = ["DeploymentUnavailable"]
CACHE_MANIFEST = "target-app/k8s/cache.yaml"


def inject():
    run_id, _ = lib.record_fault_start(
        SCENARIO, COMPONENT, ROOT_CAUSE_LABEL,
        {"affected_component": AFFECTED_COMPONENT, "expected_alerts": EXPECTED_ALERTS},
    )
    lib.kubectl("-n", lib.NAMESPACE, "scale", "deployment", "cache", "--replicas=0")
    print(f"[{SCENARIO}] injected (run {run_id})")


def reset():
    lib.kubectl("-n", lib.NAMESPACE, "apply", "-f", str(lib.ROOT / CACHE_MANIFEST))
    lib.wait_rollout("cache")
    # cart self-heals once redis is reachable again; confirm it actually does.
    lib.wait_rollout(AFFECTED_COMPONENT)
    lib.record_fault_end(SCENARIO)
    print(f"[{SCENARIO}] reset, cache and cart restored to healthy")


def run() -> bool:
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
