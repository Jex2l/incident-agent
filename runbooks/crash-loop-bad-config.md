---
root_cause_label: crash_loop_bad_config
---
# CrashLoopBackOff (bad config / bad env var)

## Symptoms
- Alert: `CrashLoopBackOff`.
- `kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"}` is 1.
- Pod restart count climbing rapidly at first, with growing backoff delays
  between restarts (10s, 20s, 40s, ...).
- Container logs show the process exiting almost immediately after start
  (often with no meaningful log output at all if it crashes before logging
  is initialized).

## Likely causes
- A bad or missing environment variable causes the app to fail fast on
  startup (explicit validation, or an unhandled exception reading config).
- A bad command/args override.
- A missing or malformed ConfigMap/Secret the app requires at startup.

## Diagnosis
1. `kubectl logs <pod> --previous` to see output from the last crashed
   attempt (current logs may be empty if it crashed before the process
   finished starting).
2. `kubectl describe pod <pod>` - check `Environment` and recent `Events`
   for what changed.
3. Compare current env/config against the last known-good deployment
   revision.

## Remediation
- Identify and fix the bad config value, then redeploy.
- If this was introduced by a recent change: roll back
  (`kubectl rollout undo deployment/<name>`) to restore service immediately,
  fix forward afterward.
