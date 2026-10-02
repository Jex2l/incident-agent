---
root_cause_label: dependency_down
---
# Dependency Down (cache/DB unreachable)

## Symptoms
- Alert: `DeploymentUnavailable` on the *dependent* service (not the
  dependency itself) - e.g. a service whose readiness probe checks its
  cache/DB connection starts failing readiness once the dependency is gone,
  even though the dependent service's own pod is still running.
- Dependent service's readiness probe failing with connection refused/timeout
  to the dependency's address.
- The dependency's own deployment may show 0 available replicas if it was
  scaled down or crashed.

## Likely causes
- The dependency (cache/DB) was scaled to 0, crashed, or is unreachable due
  to a network/DNS issue.
- A deploy to the dependency broke it (see bad-deploy runbook) and the
  failure cascaded to everything that depends on it.

## Diagnosis
1. Check the dependency's own pod/deployment status first - is it running
   at all? `kube_deployment_status_replicas_available` for the dependency.
2. If the dependency looks healthy, check network/DNS from the dependent
   pod's perspective (Service exists, endpoints populated, correct port).
3. Check the dependent service's readiness probe target - does it match
   the dependency's current Service name/port?

## Remediation
- Restore the dependency first (scale back up, fix its crash loop, or roll
  back its bad deploy) - the dependent service(s) should self-heal once
  their readiness probes start passing again.
- Do not restart the dependent service(s) as the first action; they are a
  symptom, not the cause, here.
