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

# Bounds for the `scale` action. MIN_REPLICAS=0 permits scaling a deployment
# to zero (e.g. to stop a runaway workload) but nothing negative; MAX_REPLICAS
# caps how far a single approved action can scale something up, so a bad or
# manipulated proposal can't, say, request 500 replicas.
MIN_REPLICAS = 0
MAX_REPLICAS = 3


class ActionError(Exception):
    """Raised by any validation step in this module, and only by a validation
    step - never by a failed kubectl call being swallowed as "fine". Callers
    (app.py) must treat an ActionError as proof that nothing ran against the
    cluster: the whole point of raising instead of returning a status code is
    that there's no code path where an ActionError and "the action executed"
    can both be true.
    """


def _run_kubectl(*args: str) -> str:
    """Thin subprocess wrapper around the kubectl binary. This is the only
    function in the entire project that actually shells out to the cluster -
    every write path funnels through here, and every call site above it is
    one of the two allowlisted action functions below, never raw user input.
    """
    cmd = ["kubectl", "--context", KUBE_CONTEXT] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise ActionError(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _validate_target(target: str):
    """Shared target check used by every action function. Keeping it in one
    place means adding a new action later can't accidentally skip the
    "only touch deployments we own" rule - there's no way to call
    _run_kubectl with a target that hasn't passed through here first.
    """
    if target not in ALLOWED_TARGETS:
        raise ActionError(f"target '{target}' is not in the allowed targets list: {sorted(ALLOWED_TARGETS)}")


def _rollout_undo(target: str, params: dict) -> str:
    """Roll a deployment back to its previous revision. `params` is unused
    here (rollout undo takes no parameters) but is accepted so every function
    in ALLOWED_ACTIONS has the same (target, params) signature and can be
    called uniformly from execute_action() below.
    """
    _validate_target(target)
    return _run_kubectl("-n", NAMESPACE, "rollout", "undo", f"deployment/{target}")


def _scale(target: str, params: dict) -> str:
    """Scale a deployment to a specific replica count. Both the type and the
    range of `replicas` are checked before anything touches kubectl: a
    non-int (e.g. a string, None, or a float from sloppy JSON) could either
    crash kubectl in a confusing way or, worse, be coerced into something
    unintended, so it's rejected outright rather than cast.
    """
    _validate_target(target)
    replicas = params.get("replicas")
    if not isinstance(replicas, int):
        raise ActionError(f"scale requires an integer 'replicas' param, got {replicas!r}")
    if not (MIN_REPLICAS <= replicas <= MAX_REPLICAS):
        raise ActionError(f"replicas={replicas} is outside the allowed bounds [{MIN_REPLICAS}, {MAX_REPLICAS}]")
    return _run_kubectl("-n", NAMESPACE, "scale", f"deployment/{target}", f"--replicas={replicas}")


# Action name -> executor function. This is the whole allowlist; anything
# not a key here is refused before any kubectl command is built. Adding a
# capability to the executor means adding an entry here with its own
# validation - there is no generic "run this action" path that bypasses it.
ALLOWED_ACTIONS = {
    "rollout_undo": _rollout_undo,
    "scale": _scale,
}


def execute_action(action: str, target: str, params: dict | None = None) -> str:
    """The single entry point every caller (currently just app.py's approve
    endpoint) must go through to perform a write. This is deliberately the
    LAST line of defense, not the first: a proposal's action/target/params
    were already shaped back when the investigation finished, and a human
    already approved them through the API - but none of that is trusted here.
    Everything is re-checked, from scratch, against the allowlist, right
    before the kubectl call:

      - `action not in ALLOWED_ACTIONS` catches anything that isn't exactly
        "rollout_undo" or "scale" - a typo, a renamed action, or a
        maliciously/accidentally injected action name that slipped past
        upstream validation (or was never validated upstream at all) is
        refused here with no side effects.
      - Each action function then re-validates its own target via
        _validate_target(), and _scale() re-validates its replica count -
        so even a disallowed target or out-of-bounds replica count that
        somehow made it into a stored, human-approved proposal is still
        caught here, at the last possible moment, before subprocess.run()
        is ever called.

    This re-validation is intentionally redundant with any checks done when
    the proposal was first created (store.py) or approved (app.py): the
    premise is that upstream code can be buggy or compromised, so the only
    check that's allowed to matter for "did we actually run kubectl" is the
    one immediately adjacent to the kubectl call itself. Raises ActionError
    on any failure - callers must not treat a raised ActionError as
    "executed".
    """
    params = params or {}
    if action not in ALLOWED_ACTIONS:
        raise ActionError(f"action '{action}' is not in the allowlist: {sorted(ALLOWED_ACTIONS)}")
    return ALLOWED_ACTIONS[action](target, params)
