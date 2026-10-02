#!/usr/bin/env python3
"""Fault 2: bad deploy via a broken image tag.

Scales cart to 0 first, sets its image to a tag that was never built/loaded
into the kind nodes, then scales back to 1. This guarantees a single pod
stuck in ImagePullBackOff (rather than a rolling update that might keep the
old healthy pod running alongside a failed one), so the deployment reliably
goes unavailable.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib

SCENARIO = "bad_deploy"
COMPONENT = "cart"
ROOT_CAUSE_LABEL = "bad_deploy"
EXPECTED_ALERTS = ["DeploymentUnavailable"]
MANIFEST = "target-app/k8s/cart.yaml"
BROKEN_IMAGE = "incident-agent/cart:broken-tag-never-built"


def inject():
    run_id, _ = lib.record_fault_start(
        SCENARIO, COMPONENT, ROOT_CAUSE_LABEL,
        {"broken_image": BROKEN_IMAGE, "expected_alerts": EXPECTED_ALERTS},
    )
    lib.kubectl("-n", lib.NAMESPACE, "scale", "deployment", COMPONENT, "--replicas=0")
    time.sleep(3)
    lib.kubectl("-n", lib.NAMESPACE, "set", "image", f"deployment/{COMPONENT}", f"{COMPONENT}={BROKEN_IMAGE}")
    lib.kubectl("-n", lib.NAMESPACE, "scale", "deployment", COMPONENT, "--replicas=1")
    print(f"[{SCENARIO}] injected (run {run_id})")


def reset():
    lib.kubectl("-n", lib.NAMESPACE, "apply", "-f", str(lib.ROOT / MANIFEST))
    lib.wait_rollout(COMPONENT)
    lib.record_fault_end(SCENARIO)
    print(f"[{SCENARIO}] reset, cart restored to healthy manifest")


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
