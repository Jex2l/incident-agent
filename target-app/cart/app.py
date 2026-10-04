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
import asyncio
import os
import sys
import time

from fastapi import FastAPI, HTTPException, Response
import redis
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

# Fault hook: fail before the app ever starts serving, so Kubernetes sees a
# container that exits immediately and puts the pod into CrashLoopBackOff -
# this simulates a bad deploy/config rather than a runtime failure.
if os.environ.get("FORCE_CRASH_ON_BOOT", "").lower() == "true":
    print("[cart] FORCE_CRASH_ON_BOOT set, exiting to simulate a crash loop", file=sys.stderr)
    sys.exit(1)

# Fault hook: allocate and hold a block of memory at import time. Combined
# with a low memory limit set by the OOM fault script, this pushes the pod
# over its limit so the kubelet OOMKills it - a controlled way to reproduce
# "service keeps getting OOMKilled" without a real memory leak.
_BALLAST_MB = int(os.environ.get("MEMORY_BALLAST_MB", "0") or "0")
_ballast = bytearray(_BALLAST_MB * 1024 * 1024) if _BALLAST_MB > 0 else None

# Fault hook: artificial per-request delay, applied in the middleware below.
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
    """Apply the LATENCY_MS fault hook (if set) and record every request into
    the Prometheus counters/histogram below.
    """
    start = time.time()
    # Health/readiness probes must stay fast regardless of injected latency -
    # otherwise the readiness probe itself times out, the pod never goes
    # Ready, the Service never routes traffic to it, and the fault never
    # shows up in any request metric at all.
    if LATENCY_MS and request.url.path not in ("/healthz", "/readyz"):
        await asyncio.sleep(LATENCY_MS / 1000.0)
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
    """Liveness probe target - deliberately does not check Redis, so a cache
    outage doesn't also kill otherwise-healthy cart pods. Exposes the
    current ballast size so it's visible that an OOM fault is active.
    """
    return {"status": "ok", "ballast_mb": _BALLAST_MB}


@app.get("/readyz")
def readyz():
    """Readiness probe target - does check Redis, since the cart can't serve
    real traffic without it, so a cache outage should pull the pod out of
    the Service's endpoints.
    """
    try:
        redis_client.ping()
    except redis.RedisError as e:
        raise HTTPException(status_code=503, detail=f"cache unavailable: {e}")
    return {"status": "ready"}


@app.get("/metrics")
def metrics():
    """Prometheus scrape endpoint."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/cart/{session}")
def get_cart(session: str):
    """Return the current contents of one session's cart from Redis."""
    try:
        items = redis_client.hgetall(f"cart:{session}")
    except redis.RedisError as e:
        raise HTTPException(status_code=503, detail=f"cache unavailable: {e}")
    return {"session": session, "items": {k.decode(): int(v) for k, v in items.items()}}


@app.post("/cart/{session}/add")
def add_to_cart(session: str, item: str, qty: int = 1):
    """Increment an item's quantity in the session's cart hash."""
    try:
        redis_client.hincrby(f"cart:{session}", item, qty)
    except redis.RedisError as e:
        raise HTTPException(status_code=503, detail=f"cache unavailable: {e}")
    return {"session": session, "item": item, "qty": qty}


@app.post("/cart/{session}/checkout")
def checkout(session: str):
    """Read back the cart as a receipt and clear it from Redis."""
    try:
        items = redis_client.hgetall(f"cart:{session}")
        redis_client.delete(f"cart:{session}")
    except redis.RedisError as e:
        raise HTTPException(status_code=503, detail=f"cache unavailable: {e}")
    if not items:
        raise HTTPException(status_code=400, detail="cart is empty")
    return {"session": session, "receipt": {k.decode(): int(v) for k, v in items.items()}}
