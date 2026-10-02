---
root_cause_label: oom_kill
---
# Pod OOMKilled

## Symptoms
- Alert: `PodOOMKilled`
- Pod shows `Last State: Terminated, Reason: OOMKilled` in `kubectl describe pod`.
- Restart count climbing; container may cycle between Running and CrashLoopBackOff.
- `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}` is 1.

## Likely causes
- Memory limit set too low for actual workload (memory leak, large batch, or
  genuinely undersized limit).
- A recent deploy introduced a memory leak or increased per-request memory use.
- Traffic spike pushed memory usage past the limit.

## Diagnosis
1. Check the container's configured memory limit vs. its usage trend leading
   up to the kill (`container_memory_working_set_bytes` in Prometheus).
2. Check recent deploys/rollouts on the deployment - did the limit or the
   image change recently?
3. Check logs just before the kill for signs of a leak (growing queues,
   unbounded caches) vs. a single large request.

## Remediation
- If the limit is simply undersized for legitimate load: raise
  `resources.limits.memory` on the deployment.
- If it's a memory leak introduced by a recent deploy: roll back
  (`kubectl rollout undo deployment/<name>`).
- If caused by a traffic spike: consider horizontal scaling (more replicas)
  in addition to or instead of raising the memory limit.
