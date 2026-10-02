"""Cart service: tiny FastAPI app backed by Redis (the "cache" dependency).

Deliberately includes a couple of fault-injection hooks read from env vars at
startup, so Phase 2 fault scripts can trigger realistic failure modes via a
plain `kubectl set env` / deployment patch instead of needing custom code
paths at request time:
  - FORCE_CRASH_ON_BOOT=true   -> crash-loop fault
  - MEMORY_BALLAST_MB=<n>      -> hold n MB resident, paired with a low
                                   memory limit by the OOM fault script
  - LATENCY_MS=<n>             -> added to every request, for throttling/
                                   latency-type faults
"""
import os
import sys
import time

from fastapi import FastAPI, HTTPException, Response
import redis
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

if os.environ.get("FORCE_CRASH_ON_BOOT", "").lower() == "true":
    print("[cart] FORCE_CRASH_ON_BOOT set, exiting to simulate a crash loop", file=sys.stderr)
    sys.exit(1)

_BALLAST_MB = int(os.environ.get("MEMORY_BALLAST_MB", "0") or "0")
_ballast = bytearray(_BALLAST_MB * 1024 * 1024) if _BALLAST_MB > 0 else None

LATENCY_MS = int(os.environ.get("LATENCY_MS", "0") or "0")
REDIS_HOST = os.environ.get("REDIS_HOST", "cache")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))

app = FastAPI(title="cart")
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_connect_timeout=1.5, socket_timeout=1.5)

REQUESTS = Counter("http_requests_total", "HTTP requests", ["service", "method", "path", "status"])
LATENCY = Histogram("http_request_duration_seconds", "HTTP request duration", ["service", "method", "path"])
SERVICE = "cart"


@app.middleware("http")
async def metrics_middleware(request, call_next):
    start = time.time()
    if LATENCY_MS:
        time.sleep(LATENCY_MS / 1000.0)
    path = request.url.path.split("/")[1] or "root"
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        REQUESTS.labels(SERVICE, request.method, path, str(status)).inc()
        LATENCY.labels(SERVICE, request.method, path).observe(time.time() - start)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "ballast_mb": _BALLAST_MB}


@app.get("/readyz")
def readyz():
    try:
        redis_client.ping()
    except redis.RedisError as e:
        raise HTTPException(status_code=503, detail=f"cache unavailable: {e}")
    return {"status": "ready"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/cart/{session}")
def get_cart(session: str):
    try:
        items = redis_client.hgetall(f"cart:{session}")
    except redis.RedisError as e:
        raise HTTPException(status_code=503, detail=f"cache unavailable: {e}")
    return {"session": session, "items": {k.decode(): int(v) for k, v in items.items()}}


@app.post("/cart/{session}/add")
def add_to_cart(session: str, item: str, qty: int = 1):
    try:
        redis_client.hincrby(f"cart:{session}", item, qty)
    except redis.RedisError as e:
        raise HTTPException(status_code=503, detail=f"cache unavailable: {e}")
    return {"session": session, "item": item, "qty": qty}


@app.post("/cart/{session}/checkout")
def checkout(session: str):
    try:
        items = redis_client.hgetall(f"cart:{session}")
        redis_client.delete(f"cart:{session}")
    except redis.RedisError as e:
        raise HTTPException(status_code=503, detail=f"cache unavailable: {e}")
    if not items:
        raise HTTPException(status_code=400, detail="cart is empty")
    return {"session": session, "receipt": {k.decode(): int(v) for k, v in items.items()}}
