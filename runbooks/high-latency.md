---
root_cause_label: high_latency
---
# High Latency (general)

## Symptoms
- Alert: `HighLatency` - p95 request latency above 1s for a service.

## Likely causes (roughly in order of frequency)
- CPU throttling (see cpu-throttling runbook) - check throttled-periods
  ratio first, it's cheap to rule in/out.
- A slow downstream dependency (DB query, cache, external API) on the
  request's critical path.
- Lock contention or a newly introduced synchronous/blocking call in a
  recent deploy.
- Insufficient replicas for current request volume (queueing).

## Diagnosis
1. Check CPU throttling ratio for the service first - it's a common and
   easy-to-confirm cause.
2. Check latency of the service's own dependencies (cache, DB) separately
   from the service's own processing time, if such metrics exist.
3. Check recent deploys for anything that changed the request path.
4. Check replica count vs. request rate per replica.

## Remediation
This is a symptom, not a root cause - remediation depends on what the
diagnosis above finds. See the specific runbook for the underlying cause
(cpu-throttling, dependency-down, bad-deploy) once identified.
