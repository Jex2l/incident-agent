---
root_cause_label: cpu_throttling
---
# CPU Throttling / Noisy Neighbor

## Symptoms
- Alert: `HighLatency` (p95 request latency elevated, error rate often normal).
- `container_cpu_cfs_throttled_periods_total` rising relative to
  `container_cpu_cfs_periods_total` for the affected container.
- CPU usage pinned at or near the container's CPU limit.

## Likely causes
- CPU limit set too low for actual load (undersized at deploy time, or load
  grew since).
- A noisy neighbor on the same node consuming more than its share, causing
  node-level CPU pressure (check `node_cpu_seconds_total` / node-level
  saturation, not just this container's own limit).
- A recent code change made the hot path more CPU-expensive per request.

## Diagnosis
1. Query throttling ratio:
   `rate(container_cpu_cfs_throttled_periods_total[5m]) / rate(container_cpu_cfs_periods_total[5m])`
   - a ratio approaching 1 means the container is almost always throttled.
2. Compare the container's CPU limit to its actual usage
   (`container_cpu_usage_seconds_total` rate) - if usage is pinned at the
   limit, that's the throttle.
3. Check other pods on the same node for CPU pressure if this container's
   own limit looks reasonable.

## Remediation
- Raise the CPU limit/request if the workload genuinely needs more.
- If a noisy neighbor is the cause, consider resource requests/limits or
  pod anti-affinity to separate them, or move the noisy workload.
- If a code change regressed CPU cost per request, profile and fix, or roll
  back as an interim measure.
