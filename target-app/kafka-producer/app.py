"""Kafka producer service: produces synthetic order events to Redpanda at a
steady rate, so kafka-consumer has a continuous stream to process and so
consumer lag faults have real backlog to build up against rather than a
contrived counter.
"""
import json
import os
import random
import time

from kafka import KafkaProducer
from kafka.errors import KafkaError
from prometheus_client import Counter, start_http_server

KAFKA_BROKERS = os.environ.get("KAFKA_BROKERS", "redpanda:9092")
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "orders")
PRODUCE_INTERVAL_S = float(os.environ.get("PRODUCE_INTERVAL_S", "1"))
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9100"))

PRODUCED = Counter("kafka_producer_messages_total", "Messages produced", ["topic"])
ERRORS = Counter("kafka_producer_errors_total", "Producer errors", ["topic"])


def connect(retries: int = 30, delay_s: float = 2.0) -> KafkaProducer:
    """Retry connecting to the Kafka/Redpanda broker on startup, since the
    broker container may still be coming up when this service starts in
    docker-compose/k8s and a single failed connection attempt shouldn't be
    fatal.
    """
    last_err = None
    for _ in range(retries):
        try:
            return KafkaProducer(
                bootstrap_servers=KAFKA_BROKERS,
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                request_timeout_ms=5000,
            )
        except KafkaError as e:
            last_err = e
            print(f"[kafka-producer] waiting for broker: {e}")
            time.sleep(delay_s)
    raise RuntimeError(f"could not connect to Kafka brokers: {last_err}")


def main():
    """Connect, then loop forever producing one synthetic order event per
    PRODUCE_INTERVAL_S. Runs as a plain blocking loop (not FastAPI) since
    this service has no inbound HTTP API of its own, just a Prometheus
    metrics endpoint exposed via start_http_server.
    """
    start_http_server(METRICS_PORT)
    producer = connect()
    order_id = 0
    print(f"[kafka-producer] producing to {KAFKA_BROKERS}/{KAFKA_TOPIC} every {PRODUCE_INTERVAL_S}s")
    while True:
        order_id += 1
        event = {
            "order_id": order_id,
            "item": random.choice(["widget", "gadget", "gizmo", "thingamajig"]),
            "ts": time.time(),
        }
        try:
            producer.send(KAFKA_TOPIC, value=event)
            producer.flush(timeout=5)
            PRODUCED.labels(KAFKA_TOPIC).inc()
        except KafkaError as e:
            ERRORS.labels(KAFKA_TOPIC).inc()
            print(f"[kafka-producer] send failed: {e}")
        time.sleep(PRODUCE_INTERVAL_S)


if __name__ == "__main__":
    main()
