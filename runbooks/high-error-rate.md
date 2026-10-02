---
root_cause_label: high_error_rate
---
# High HTTP Error Rate (general)

## Symptoms
- Alert: `HighErrorRate` - more than 10% of requests to a service returning 5xx.

## Likely causes (roughly in order of frequency)
- A dependency the service calls is down or erroring (see dependency-down runbook).
- A bad deploy introduced a bug or broke config (see bad-deploy runbook).
- Resource exhaustion (OOM kills, CPU throttling) causing requests to fail
  or time out under load.
- A downstream rate limit or quota being hit.

## Diagnosis
1. Check the error rate by endpoint/path, not just overall - is it one
   specific route or everything?
2. Check recent deploys to this service and anything it depends on.
3. Check pod health (restarts, OOMKilled, CrashLoopBackOff) for the service.
4. Check logs for the actual error messages/stack traces, not just the
   status code.

## Remediation
This is a symptom, not a root cause - remediation depends on what the
diagnosis above finds. See the specific runbook for the underlying cause
(bad-deploy, dependency-down, oom-kill, cpu-throttling) once identified.
