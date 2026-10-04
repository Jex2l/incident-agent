#!/usr/bin/env python3
"""Fault 3: CPU throttling / noisy neighbor.

Patches cart with a sharply reduced CPU limit/request (so kubelet's CFS
quota genuinely throttles it under the frontend's background traffic,
giving the agent real container_cpu_cfs_throttled_periods_total evidence to
find later) combined with the LATENCY_MS hook built into cart/app.py as a
deterministic backstop. Relying purely on real throttling under kind's
light synthetic traffic would be too flaky for a reliable demo/eval; the
injected latency guarantees the p95 threshold is crossed regardless of how
much actual CPU contention occurs.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib

SCENARIO = "cpu_throttle"
COMPONENT = "cart"
ROOT_CAUSE_LABEL = "cpu_throttling"
EXPECTED_ALERTS = ["HighLatency"]

# Original values from target-app/k8s/cart.yaml.
ORIGINAL_CPU_LIMIT = "250m"
ORIGINAL_CPU_REQUEST = "25m"

# LATENCY_MS=1200 is added to every cart request EXCEPT /healthz and /readyz
# (see target-app/cart/app.py's middleware, which special-cases those two
# paths). That exclusion matters: if the injected sleep applied to /readyz
# too, the kubelet's own readiness probe would start timing out and cart
# would be marked NotReady / pulled out of Service endpoints - which would
# manifest as an availability fault, not the latency fault this scenario is
# meant to simulate. An earlier version of this fault didn't exclude those
# paths and took down the probe along with real traffic.
INJECT_PATCH = {
    "spec": {"template": {"spec": {"containers": [{
        "name": "cart",
        "env": [{"name": "LATENCY_MS", "value": "1200"}],
        "resources": {
            "limits": {"cpu": "80m"},
            "requests": {"cpu": "80m"},
        },
    }]}}}
}

RESET_PATCH = {
    "spec": {"template": {"spec": {"containers": [{
        "name": "cart",
        "env": [{"name": "LATENCY_MS", "$patch": "delete"}],
        "resources": {
            "limits": {"cpu": ORIGINAL_CPU_LIMIT},
            "requests": {"cpu": ORIGINAL_CPU_REQUEST},
        },
    }]}}}
}


def inject():
    """Patch cart's Deployment: drop CPU limit/request to 80m and set
    LATENCY_MS=1200, after recording ground truth.

    The low CPU allotment makes the CFS quota genuinely throttle cart under
    the frontend's background load, while LATENCY_MS is a deterministic
    backstop that guarantees the HighLatency p95 threshold is crossed even if
    kind's synthetic traffic isn't heavy enough to cause real throttling.
    """
    run_id, _ = lib.record_fault_start(
        SCENARIO, COMPONENT, ROOT_CAUSE_LABEL,
        {"cpu_limit": "80m", "latency_ms": 1200, "expected_alerts": EXPECTED_ALERTS},
    )
    lib.patch_deployment(COMPONENT, INJECT_PATCH)
    print(f"[{SCENARIO}] injected (run {run_id})")


def reset():
    """Remove LATENCY_MS (via $patch: delete) and restore cart's original CPU
    limit/request, then wait for the rollout and mark ground truth resolved.
    """
    lib.patch_deployment(COMPONENT, RESET_PATCH)
    lib.wait_rollout(COMPONENT)
    lib.record_fault_end(SCENARIO)
    print(f"[{SCENARIO}] reset, cart restored to healthy")


def run() -> bool:
    """Full fault cycle: inject, poll up to 5 min for HighLatency on the
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
