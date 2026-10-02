---
root_cause_label: kafka_consumer_lag
---
# Kafka Consumer Lag High

## Symptoms
- Alert: `KafkaConsumerLagHigh`.
- `kafka_consumer_lag_messages` for the consumer group climbing and staying
  above threshold.
- Downstream effects (e.g. stale data, delayed order processing) may show up
  before the lag alert itself does.

## Likely causes
- Consumer process stopped polling (hung, deadlocked, or crashed without
  the pod restarting - e.g. stuck in a blocking call).
- Consumer is alive but processing too slowly per message (a slow downstream
  dependency, an added synchronous call, or a regression in per-message cost).
- Producer rate increased beyond what the consumer group can keep up with
  at its current parallelism.
- Consumer group rebalancing repeatedly (flapping membership), spending more
  time rejoining than consuming.

## Diagnosis
1. Check `kafka_consumer_messages_total` rate - near zero means the consumer
   has effectively stopped processing even if the pod looks healthy.
2. Check consumer pod logs/events for crashes, restarts, or exceptions around
   when lag started climbing.
3. Check for a recent deploy to the consumer that could have introduced a
   slow dependency call or a blocking bug.
4. Compare producer rate (`kafka_producer_messages_total`) against consumer
   rate - if producer rate increased and consumer rate didn't, it's a
   capacity issue, not a stall.

## Remediation
- If the consumer stalled: restart it (`kubectl rollout restart deployment/<name>`)
  after identifying and fixing the root cause, otherwise it will stall again.
- If it's a genuine capacity shortfall: scale out consumer replicas (more
  consumers in the group, up to the partition count) or optimize the
  per-message processing path.
- If caused by a recent deploy: roll back and investigate the regression.
