"""Consumes order events from Redpanda and reports real consumer-group lag.

Fault hooks (set via env, patched by Phase 2 fault scripts):
  - CONSUMER_DELAY_MS=<n>   -> artificial per-message processing delay, to
                               build up lag under load (slow consumer fault)
  - CONSUMER_STOPPED=true   -> connect but never poll, so lag grows
                               unbounded (stopped consumer fault)
"""
import os
import time

from kafka import KafkaConsumer, TopicPartition
from kafka.errors import KafkaError
from prometheus_client import Counter, Gauge, start_http_server

KAFKA_BROKERS = os.environ.get("KAFKA_BROKERS", "redpanda:9092")
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "orders")
GROUP_ID = os.environ.get("CONSUMER_GROUP", "orders-consumer-group")
CONSUMER_DELAY_MS = int(os.environ.get("CONSUMER_DELAY_MS", "0") or "0")
CONSUMER_STOPPED = os.environ.get("CONSUMER_STOPPED", "").lower() == "true"
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9100"))
LAG_POLL_INTERVAL_S = 5.0

PROCESSED = Counter("kafka_consumer_messages_total", "Messages processed", ["topic", "group"])
LAG = Gauge("kafka_consumer_lag_messages", "Consumer group lag in messages", ["topic", "group"])


def connect(retries: int = 30, delay_s: float = 2.0) -> KafkaConsumer:
    last_err = None
    for _ in range(retries):
        try:
            return KafkaConsumer(
                KAFKA_TOPIC,
                bootstrap_servers=KAFKA_BROKERS,
                group_id=GROUP_ID,
                auto_offset_reset="earliest",
                enable_auto_commit=True,
                consumer_timeout_ms=1000,
                request_timeout_ms=6000,
            )
        except KafkaError as e:
            last_err = e
            print(f"[kafka-consumer] waiting for broker: {e}")
            time.sleep(delay_s)
    raise RuntimeError(f"could not connect to Kafka brokers: {last_err}")


def report_lag(consumer: KafkaConsumer):
    try:
        partitions = consumer.assignment()
        if not partitions:
            return
        total_lag = 0
        end_offsets = consumer.end_offsets(list(partitions))
        for tp in partitions:
            position = consumer.position(tp)
            highwater = end_offsets.get(tp, position)
            total_lag += max(0, highwater - position)
        LAG.labels(KAFKA_TOPIC, GROUP_ID).set(total_lag)
    except KafkaError as e:
        print(f"[kafka-consumer] lag check failed: {e}")


def main():
    start_http_server(METRICS_PORT)
    consumer = connect()
    print(f"[kafka-consumer] consuming {KAFKA_BROKERS}/{KAFKA_TOPIC} group={GROUP_ID} "
          f"delay_ms={CONSUMER_DELAY_MS} stopped={CONSUMER_STOPPED}")
    last_lag_check = 0.0
    while True:
        if CONSUMER_STOPPED:
            # Stay connected (so the consumer group / pod still shows "ready"), but
            # never poll, so lag grows just like a hung consumer would in production.
            time.sleep(LAG_POLL_INTERVAL_S)
            report_lag(consumer)
            continue

        for message in consumer:
            if CONSUMER_DELAY_MS:
                time.sleep(CONSUMER_DELAY_MS / 1000.0)
            PROCESSED.labels(KAFKA_TOPIC, GROUP_ID).inc()

        now = time.time()
        if now - last_lag_check > LAG_POLL_INTERVAL_S:
            report_lag(consumer)
            last_lag_check = now


if __name__ == "__main__":
    main()
