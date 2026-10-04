"""The action allowlist: the only write actions the executor will ever run,
each with explicit bounds. Nothing outside ALLOWED_ACTIONS executes, no
matter what a proposal asks for - this is checked again at execution time
(not just when the proposal was created), so a proposal can never smuggle
through an action that isn't allowlisted.
"""
import os
import subprocess

KUBE_CONTEXT = os.environ.get("KUBE_CONTEXT", "kind-incident-agent")
NAMESPACE = os.environ.get("INCIDENT_AGENT_NAMESPACE", "incident-agent")

# Only these deployments may ever be targeted - blocks an action from being
# pointed at kube-system, monitoring, or anything outside the sample app.
ALLOWED_TARGETS = {"cache", "cart", "frontend", "kafka-producer", "kafka-consumer", "redpanda", "webhook-receiver"}

MIN_REPLICAS = 0
MAX_REPLICAS = 3


class ActionError(Exception):
    """Raised when an action fails validation - never executed."""


def _run_kubectl(*args: str) -> str:
    cmd = ["kubectl", "--context", KUBE_CONTEXT] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise ActionError(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _validate_target(target: str):
    if target not in ALLOWED_TARGETS:
        raise ActionError(f"target '{target}' is not in the allowed targets list: {sorted(ALLOWED_TARGETS)}")


def _rollout_undo(target: str, params: dict) -> str:
    _validate_target(target)
    return _run_kubectl("-n", NAMESPACE, "rollout", "undo", f"deployment/{target}")


def _scale(target: str, params: dict) -> str:
    _validate_target(target)
    replicas = params.get("replicas")
    if not isinstance(replicas, int):
        raise ActionError(f"scale requires an integer 'replicas' param, got {replicas!r}")
    if not (MIN_REPLICAS <= replicas <= MAX_REPLICAS):
        raise ActionError(f"replicas={replicas} is outside the allowed bounds [{MIN_REPLICAS}, {MAX_REPLICAS}]")
    return _run_kubectl("-n", NAMESPACE, "scale", f"deployment/{target}", f"--replicas={replicas}")


# Action name -> executor function. This is the whole allowlist; anything
# not a key here is refused before any kubectl command is built.
ALLOWED_ACTIONS = {
    "rollout_undo": _rollout_undo,
    "scale": _scale,
}


def execute_action(action: str, target: str, params: dict | None = None) -> str:
    """Validate and execute an action. Raises ActionError on any validation
    failure - callers must not treat a raised ActionError as "executed"."""
    params = params or {}
    if action not in ALLOWED_ACTIONS:
        raise ActionError(f"action '{action}' is not in the allowlist: {sorted(ALLOWED_ACTIONS)}")
    return ALLOWED_ACTIONS[action](target, params)
