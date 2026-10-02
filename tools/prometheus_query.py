"""prometheus_query MCP tool: read-only PromQL access for the agent.

Returns trimmed, structured JSON rather than raw Prometheus API responses:
instant queries cap the number of series returned, and range queries are
downsampled to per-series summary stats (min/max/avg/last) instead of the
full raw timeseries, so a small local model isn't handed thousands of
(timestamp, value) pairs for one query.
"""
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import cap_list

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
MAX_SERIES = int(os.environ.get("PROMETHEUS_MAX_SERIES", "20"))
HTTP_TIMEOUT = 10.0


def _round(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return round(f, 4)


def query_instant(promql: str) -> dict:
    """Run an instant PromQL query. Returns a capped list of {labels, value}."""
    resp = httpx.get(
        f"{PROMETHEUS_URL}/api/v1/query",
        params={"query": promql},
        timeout=HTTP_TIMEOUT,
    )
    # Prometheus returns 4xx (not 200-with-error-body) for invalid PromQL,
    # but still sends a {"status":"error","error":...} JSON body - parse
    # that instead of raising, so bad queries come back as a tool error the
    # agent can see, not an unhandled exception.
    body = resp.json()
    if body.get("status") != "success":
        return {"query": promql, "error": body.get("error", "unknown error"), "series": []}

    result = body["data"]["result"]
    series = []
    for item in result:
        labels = {k: v for k, v in item["metric"].items() if k != "__name__"}
        _ts, value = item["value"]
        series.append({"labels": labels, "value": _round(value)})

    series, truncated = cap_list(series, MAX_SERIES)
    return {
        "query": promql,
        "result_type": body["data"]["resultType"],
        "series_count": len(result),
        "truncated": truncated,
        "series": series,
    }


def query_range_stats(promql: str, minutes: int = 15, step_seconds: int = 15) -> dict:
    """Run a range PromQL query and downsample each series to summary stats
    (min, max, avg, last) rather than returning the raw timeseries.
    """
    import time as _time

    end = _time.time()
    start = end - minutes * 60
    resp = httpx.get(
        f"{PROMETHEUS_URL}/api/v1/query_range",
        params={"query": promql, "start": start, "end": end, "step": f"{step_seconds}s"},
        timeout=HTTP_TIMEOUT,
    )
    body = resp.json()
    if body.get("status") != "success":
        return {"query": promql, "error": body.get("error", "unknown error"), "series": []}

    result = body["data"]["result"]
    series = []
    for item in result:
        labels = {k: v for k, v in item["metric"].items() if k != "__name__"}
        values = [float(v) for _ts, v in item["values"]] if item.get("values") else []
        if not values:
            continue
        series.append({
            "labels": labels,
            "min": _round(min(values)),
            "max": _round(max(values)),
            "avg": _round(sum(values) / len(values)),
            "last": _round(values[-1]),
            "samples": len(values),
        })

    series, truncated = cap_list(series, MAX_SERIES)
    return {
        "query": promql,
        "window_minutes": minutes,
        "series_count": len(result),
        "truncated": truncated,
        "series": series,
    }


def build_server():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("prometheus_query")

    @mcp.tool()
    def prometheus_query_instant(promql: str) -> dict:
        """Run an instant PromQL query against the cluster's Prometheus and
        return a capped list of current series values."""
        return query_instant(promql)

    @mcp.tool()
    def prometheus_query_range_stats(promql: str, minutes: int = 15, step_seconds: int = 15) -> dict:
        """Run a PromQL range query over the last N minutes and return
        per-series min/max/avg/last stats (not the raw timeseries)."""
        return query_range_stats(promql, minutes, step_seconds)

    return mcp


if __name__ == "__main__":
    build_server().run()
