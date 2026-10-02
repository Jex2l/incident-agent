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
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", response_class=HTMLResponse)
def index():
    items = "".join(f"<li>{i}</li>" for i in ITEMS)
    return f"<html><body><h1>incident-agent demo shop</h1><ul>{items}</ul></body></html>"


async def _traffic_loop():
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
                print(f"[frontend traffic-gen] cart call failed: {e}")
            await asyncio.sleep(TRAFFIC_INTERVAL_S)


@app.on_event("startup")
async def start_traffic_generator():
    asyncio.create_task(_traffic_loop())
