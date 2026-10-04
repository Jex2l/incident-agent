"""log_search MCP tool: read-only Loki log search for the agent.

Returns trimmed, structured JSON: log lines are normalized (numbers, UUIDs,
IPs replaced with placeholders) and grouped into patterns with counts, plus
a few sample raw lines per pattern - not a dump of every matching line.
"""
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.common import cap_list

LOKI_URL = os.environ.get("LOKI_URL", "http://localhost:3100")
MAX_PATTERNS = int(os.environ.get("LOG_SEARCH_MAX_PATTERNS", "10"))
SAMPLES_PER_PATTERN = 3
HTTP_TIMEOUT = 10.0

# Order matters: UUIDs and IPs are matched and replaced before the generic
# digit run (_NUM_RE), because a UUID/IP is itself a sequence of digit runs -
# if _NUM_RE ran first it would shred them into "<uuid>"-unfriendly fragments
# and the UUID/IP regexes would no longer match the mangled remains.
_NUM_RE = re.compile(r"\d+")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_IP_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


def normalize_line(line: str) -> str:
    """Collapse a log line into a pattern: strip IDs/numbers/IPs so repeated
    errors with different request IDs or timestamps group together.

    Without this, a burst of the same underlying error (e.g. "connection
    refused to 10.0.3.7") would show up as hundreds of distinct "unique"
    lines differing only by IP/request-id/timestamp, flooding the result
    with near-duplicates instead of surfacing one pattern with a count. The
    normalized string is used only as a grouping key (see search_logs) -
    the original raw lines are preserved separately as samples.
    """
    line = _UUID_RE.sub("<uuid>", line)
    line = _IP_RE.sub("<ip>", line)
    line = _NUM_RE.sub("#", line)
    return line.strip()


def _build_selector(namespace: str, pod: str | None, container: str | None) -> str:
    """Build a LogQL stream selector like {namespace="x", pod=~"y.*"}.
    pod is matched as a regex prefix (pod=~"y.*") rather than an exact match
    because Kubernetes pod names carry a generated suffix (e.g. a ReplicaSet
    hash + random string), so callers only know the Deployment/workload name
    prefix, not the exact live pod name.
    """
    parts = [f'namespace="{namespace}"']
    if pod:
        parts.append(f'pod=~"{pod}.*"')
    if container:
        parts.append(f'container="{container}"')
    return "{" + ", ".join(parts) + "}"


def search_logs(
    namespace: str,
    pod: str | None = None,
    container: str | None = None,
    query_text: str | None = None,
    since_minutes: int = 15,
    limit: int = 500,
) -> dict:
    """Query Loki for logs matching the given namespace/pod/container/text
    filters over the last `since_minutes`, then summarize the results into
    normalized error patterns (see normalize_line) ranked by frequency, each
    with a few sample raw lines - rather than returning up to `limit` raw
    lines directly, which would be too much for a small local model's
    context window.

    Returns a dict with `patterns` (capped to MAX_PATTERNS, richest-first)
    and `truncated` if more patterns existed than fit; on a Loki-side error
    returns an `error` key with empty `patterns` instead of raising.
    """
    selector = _build_selector(namespace, pod, container)
    logql = selector if not query_text else f'{selector} |= "{query_text}"'

    end = time.time()
    start = end - since_minutes * 60
    resp = httpx.get(
        f"{LOKI_URL}/loki/api/v1/query_range",
        params={
            "query": logql,
            "start": int(start * 1e9),
            "end": int(end * 1e9),
            "limit": limit,
            "direction": "backward",
        },
        timeout=HTTP_TIMEOUT,
    )
    # Loki returns 4xx with a *plain text* error body for a malformed LogQL
    # query (not JSON like Prometheus), so neither raise_for_status() nor
    # resp.json() is safe to call unconditionally here.
    if resp.status_code != 200:
        return {"query": logql, "error": resp.text.strip(), "patterns": []}
    body = resp.json()
    if body.get("status") != "success":
        return {"query": logql, "error": body.get("error", "unknown error"), "patterns": []}

    counts: Counter = Counter()
    samples: dict[str, list] = defaultdict(list)
    total_lines = 0
    # Group every returned line by its normalized pattern, counting
    # occurrences and keeping only the first few raw lines per pattern as
    # representative samples (not all of them - that would reintroduce the
    # same volume problem normalization is meant to solve).
    for stream in body["data"]["result"]:
        for ts, line in stream.get("values", []):
            total_lines += 1
            pattern = normalize_line(line)
            counts[pattern] += 1
            if len(samples[pattern]) < SAMPLES_PER_PATTERN:
                samples[pattern].append(line)

    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    patterns = [
        {"pattern": pattern, "count": count, "samples": samples[pattern]}
        for pattern, count in ranked
    ]
    patterns, truncated = cap_list(patterns, MAX_PATTERNS)

    return {
        "query": logql,
        "since_minutes": since_minutes,
        "total_lines": total_lines,
        "pattern_count": len(ranked),
        "truncated": truncated,
        "patterns": patterns,
    }


def build_server():
    """Wire search_logs up as an MCP tool for the agent to call."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("log_search")

    @mcp.tool()
    def search_pod_logs(
        namespace: str,
        pod: str | None = None,
        container: str | None = None,
        query_text: str | None = None,
        since_minutes: int = 15,
        limit: int = 500,
    ) -> dict:
        """Search logs in Loki for a namespace (optionally filtered by pod
        prefix, container, or substring), summarized into error patterns
        with counts and a few sample lines each - not a raw log dump."""
        return search_logs(namespace, pod, container, query_text, since_minutes, limit)

    return mcp


if __name__ == "__main__":
    # Run as a standalone MCP server over stdio when launched directly;
    # tests import search_logs/normalize_line directly instead.
    build_server().run()
