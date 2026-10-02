#!/usr/bin/env python3
"""Fault 4: Kafka consumer lag via a stopped consumer.

Patches kafka-consumer with CONSUMER_STOPPED=true (a hook built into
target-app/kafka-consumer/app.py): it stays connected to the consumer group
(so it doesn't look crashed) but never polls, so lag grows steadily while
kafka-producer keeps producing at its normal rate.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib

SCENARIO = "kafka_lag"
COMPONENT = "kafka-consumer"
ROOT_CAUSE_LABEL = "kafka_consumer_lag"
EXPECTED_ALERTS = ["KafkaConsumerLagHigh"]

# target-app/k8s/kafka-consumer.yaml declares CONSUMER_STOPPED="false"
# explicitly (unlike cart's fault hooks, which are absent by default), so
# reset is a plain overwrite - no "$patch: delete" needed.
INJECT_PATCH = {
    "spec": {"template": {"spec": {"containers": [{
        "name": "kafka-consumer",
        "env": [{"name": "CONSUMER_STOPPED", "value": "true"}],
    }]}}}
}

RESET_PATCH = {
    "spec": {"template": {"spec": {"containers": [{
        "name": "kafka-consumer",
        "env": [{"name": "CONSUMER_STOPPED", "value": "false"}],
    }]}}}
}


def inject():
    run_id, _ = lib.record_fault_start(
        SCENARIO, COMPONENT, ROOT_CAUSE_LABEL,
        {"consumer_stopped": True, "expected_alerts": EXPECTED_ALERTS},
    )
    lib.patch_deployment(COMPONENT, INJECT_PATCH)
    print(f"[{SCENARIO}] injected (run {run_id})")


def reset():
    lib.patch_deployment(COMPONENT, RESET_PATCH)
    lib.wait_rollout(COMPONENT)
    lib.record_fault_end(SCENARIO)
    print(f"[{SCENARIO}] reset, kafka-consumer restored to healthy")


def run() -> bool:
    since = lib.now_iso()
    inject()
    print(f"[{SCENARIO}] waiting up to 7 min for {EXPECTED_ALERTS}...")
    alert = lib.wait_for_alert(EXPECTED_ALERTS, since, timeout_s=420)
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
