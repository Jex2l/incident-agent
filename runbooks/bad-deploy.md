---
root_cause_label: bad_deploy
---
# Bad Deploy (broken image or config)

## Symptoms
- Alert: `DeploymentUnavailable`.
- Pod stuck in `ImagePullBackOff`, `ErrImagePull`, or `CreateContainerConfigError`.
- `kube_deployment_status_replicas_available` below `kube_deployment_spec_replicas`.
- Error rate / latency may also spike if the old pods were scaled down during
  the rollout.

## Likely causes
- Image tag doesn't exist, was never pushed, or was pushed to the wrong registry.
- Bad config: invalid env var, missing ConfigMap/Secret key, bad command/args.
- Readiness probe misconfigured for the new revision (wrong port/path).

## Diagnosis
1. `kubectl describe pod <pod>` - check `Events` for `Failed to pull image`,
   `CreateContainerConfigError`, or probe failures.
2. Compare the failing pod's image tag / env against the last known-good
   revision (`kubectl rollout history deployment/<name>`).
3. Check the deployment's recent event history for when the bad rollout started.

## Remediation
- Roll back immediately to restore availability:
  `kubectl rollout undo deployment/<name>`.
- Fix the image tag or config in source, re-deploy once verified.
- If this was a canary/gradual rollout, pause further rollout until fixed.
