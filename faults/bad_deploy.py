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
    """Scale cart to 0, set its image to a tag that was never built/loaded
    into the kind nodes, then scale back to 1, after recording ground truth.

    Scaling to 0 before swapping the image (rather than just `set image` on
    the running deployment) avoids a rolling update, where Kubernetes would
    keep the old, healthy pod serving traffic alongside the new pod stuck in
    ImagePullBackOff - that would make the deployment still look available.
    Scaling to 0 first guarantees the only pod that comes up is the broken
    one, so the deployment's available replica count reliably drops to 0 and
    DeploymentUnavailable should fire. The `time.sleep(3)` gives the scale-down
    a moment to actually terminate the old pod before the image is swapped.
    """
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
    """Re-apply the pristine cart manifest to restore the original image and
    replica count, wait for the rollout, and mark the ground-truth record
    resolved.

    Unlike the other faults, this one's only change (the image tag, via
    `kubectl set image`) IS reflected in the Deployment's
    last-applied-configuration annotation, so a plain `kubectl apply` of the
    original manifest correctly reverts it - no `$patch: delete` strategic
    patch is needed here.
    """
    lib.kubectl("-n", lib.NAMESPACE, "apply", "-f", str(lib.ROOT / MANIFEST))
    lib.wait_rollout(COMPONENT)
    lib.record_fault_end(SCENARIO)
    print(f"[{SCENARIO}] reset, cart restored to healthy manifest")


def run() -> bool:
    """Full fault cycle: inject, poll up to 5 min for DeploymentUnavailable on
    the webhook receiver, reset regardless of outcome, and report pass/fail.
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
