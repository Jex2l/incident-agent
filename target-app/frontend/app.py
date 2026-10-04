"""Frontend service: tiny shop UI that calls the cart service.

Runs a background traffic generator so Prometheus has continuous baseline
metrics to compare against during fault injection, without needing a human
clicking through the UI during eval runs.
"""
import asyncio
import os
import random
import time

import httpx
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

CART_URL = os.environ.get("CART_URL", "http://cart:8080")
TRAFFIC_INTERVAL_S = float(os.environ.get("TRAFFIC_INTERVAL_S", "2"))
ITEMS = ["widget", "gadget", "gizmo", "thingamajig"]

app = FastAPI(title="frontend")

REQUESTS = Counter("http_requests_total", "HTTP requests", ["service", "method", "path", "status"])
LATENCY = Histogram("http_request_duration_seconds", "HTTP request duration", ["service", "method", "path"])
SERVICE = "frontend"


@app.middleware("http")
async def metrics_middleware(request, call_next):
    """Time every request and record it into the Prometheus counters/histogram
    below, keyed by (service, method, path, status) so Grafana/alert rules can
    distinguish this service's traffic from the others sharing the same
    metric names.
    """
    start = time.time()
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
    """Liveness probe target - always returns ok if the process is up."""
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    """Prometheus scrape endpoint."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", response_class=HTMLResponse)
def index():
    """Trivial shop landing page, just enough UI to have something to point
    a browser at; the real traffic driving metrics comes from the background
    loop below, not from anyone loading this page.
    """
    items = "".join(f"<li>{i}</li>" for i in ITEMS)
    return f"<html><body><h1>incident-agent demo shop</h1><ul>{items}</ul></body></html>"


async def _traffic_loop():
    """Continuously exercise the cart service (view/add/checkout) so there is
    always baseline request volume and latency flowing into Prometheus, even
    when nobody is manually using the demo shop. This is what makes "traffic
    drops to zero" or "latency spikes" visible on dashboards during fault
    injection, instead of a flat line with no data either way.
    """
    session = "demo-session"
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            try:
                action = random.choice(["view", "add", "checkout"])
                if action == "view":
                    await client.get(f"{CART_URL}/cart/{session}")
                elif action == "add":
                    item = random.choice(ITEMS)
                    await client.post(f"{CART_URL}/cart/{session}/add", params={"item": item, "qty": 1})
                else:
                    await client.post(f"{CART_URL}/cart/{session}/checkout")
            except Exception as e:
                # Swallow errors on purpose: a cart outage shouldn't crash the
                # traffic generator, it should just keep hammering the cart so
                # the outage itself shows up as failed-request metrics.
                print(f"[frontend traffic-gen] cart call failed: {e}")
            await asyncio.sleep(TRAFFIC_INTERVAL_S)


@app.on_event("startup")
async def start_traffic_generator():
    """Kick off the background traffic generator as a fire-and-forget task
    once the app is serving, rather than blocking startup on it.
    """
    asyncio.create_task(_traffic_loop())
