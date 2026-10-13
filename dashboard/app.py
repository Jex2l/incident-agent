"""The operator dashboard: one page, polling every few seconds, showing
everything you'd otherwise open four separate tabs for - active alerts
(from Alertmanager), the sample app's pod health (from kubectl, the same
admin-level access the fault scripts use - this is a human-operator tool,
not the agent's restricted investigation path), the agent's investigations
(including ones still in progress, via the placeholder record
agent/webhook.py now writes the moment it starts one), and pending
remediation approvals (from the executor, with Approve/Reject right here).

Like ui/app.py, this process holds no state of its own and talks to the
agent/executor only over HTTP - it is a read-mostly aggregation + relay
layer, not a privileged one. It shells out to kubectl directly for pod
status (the same way faults/lib.py does) since pod listing isn't sensitive
in the way write actions are, and this is explicitly an operator-facing
tool, not something the LLM agent can reach.

This page is a *summary*, not a replacement for the full tools it
aggregates: Grafana's actual metric graphs, Prometheus's raw PromQL
console, and the trace viewer's full span-by-span waterfall are linked at
the top for the cases - debugging a weird metric, reading the agent's exact
tool-by-tool reasoning - where a summary genuinely isn't enough.
"""
import html
import json
import os
import subprocess
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

ROOT = Path(__file__).resolve().parent.parent
INCIDENTS_DIR = ROOT / "evals" / "runs"

AGENT_URL = os.environ.get("AGENT_URL", "http://localhost:8010")
EXECUTOR_URL = os.environ.get("EXECUTOR_URL", "http://localhost:8020")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
ALERTMANAGER_URL = os.environ.get("ALERTMANAGER_URL", "http://localhost:9093")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3000")
TRACE_VIEWER_URL = os.environ.get("TRACE_VIEWER_URL", "http://localhost:4200/viewer.html")

KUBE_CONTEXT = os.environ.get("KUBE_CONTEXT", "kind-incident-agent")
NAMESPACE = os.environ.get("INCIDENT_AGENT_NAMESPACE", "incident-agent")

# How often the page re-fetches /api/state. Short enough to feel live for a
# human watching a fault happen, long enough not to hammer kubectl/Prometheus
# with a subprocess spawn every second.
POLL_INTERVAL_MS = 3000

app = FastAPI(title="incident-agent dashboard")


def _esc(value) -> str:
    """HTML-escape for safe interpolation into hand-built markup - same
    helper as ui/app.py, duplicated rather than imported so this module has
    no import-time dependency on the ui package (they're independent
    processes/services, not a shared library)."""
    return html.escape(str(value)) if value is not None else ""


# ---------------------------------------------------------------------------
# Data gathering: each of these degrades to an empty list/dict on failure
# rather than raising, so one unreachable service (e.g. Prometheus not up
# yet) doesn't take down the whole page - the corresponding section just
# renders as empty/unavailable instead.
# ---------------------------------------------------------------------------

def _get_pods() -> list[dict]:
    """Current pod status in the sample-app namespace via `kubectl get pods
    -o json`, parsed down to just what the dashboard needs: name, phase,
    aggregate readiness, total restarts, and any waiting-state reason
    (CrashLoopBackOff, ErrImagePull, etc.) - the same signals a human would
    look for first.
    """
    try:
        proc = subprocess.run(
            ["kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE, "get", "pods", "-o", "json"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return []
        data = json.loads(proc.stdout)
    except Exception:
        return []

    pods = []
    for item in data.get("items", []):
        statuses = item.get("status", {}).get("containerStatuses", [])
        reason = None
        for c in statuses:
            waiting_reason = c.get("state", {}).get("waiting", {}).get("reason")
            terminated_reason = c.get("lastState", {}).get("terminated", {}).get("reason")
            reason = waiting_reason or terminated_reason or reason
        pods.append({
            "name": item["metadata"]["name"],
            "phase": item.get("status", {}).get("phase"),
            "ready": bool(statuses) and all(c.get("ready") for c in statuses),
            "restarts": sum(c.get("restartCount", 0) for c in statuses),
            "reason": reason,
        })
    return sorted(pods, key=lambda p: p["name"])


async def _get_alerts(client: httpx.AsyncClient) -> list[dict]:
    """Currently active Alertmanager alerts, filtered to ones carrying our
    own `root_cause_hint` annotation - the same filter agent/webhook.py uses
    to tell "one of our 6 PrometheusRules" apart from kube-prometheus-stack's
    bundled defaults (Watchdog, NodeClockNotSynchronising, etc.), which fire
    constantly and would otherwise drown out the signal that actually
    matters here.
    """
    try:
        resp = await client.get(f"{ALERTMANAGER_URL}/api/v2/alerts", timeout=5)
        resp.raise_for_status()
        raw = resp.json()
    except Exception:
        return []

    alerts = []
    for a in raw:
        labels = a.get("labels", {})
        annotations = a.get("annotations", {})
        if not annotations.get("root_cause_hint"):
            continue
        alerts.append({
            "alertname": labels.get("alertname"),
            "namespace": labels.get("namespace"),
            "state": a.get("status", {}).get("state"),
            "starts_at": a.get("startsAt"),
            "summary": annotations.get("summary"),
        })
    return sorted(alerts, key=lambda a: a["starts_at"] or "", reverse=True)


def _get_investigations(limit: int = 10) -> list[dict]:
    """Recent investigation records straight from evals/runs/ (the same
    files agent/webhook.py writes and executor/store.py reads) - read
    directly off disk rather than through the agent's /incidents HTTP route
    so this still works even if the agent process is down, showing whatever
    was last written. Includes in-progress placeholders (status
    "investigating"), which is what lets this page show the agent's
    conclusion live instead of only after it finishes.
    """
    files = sorted(INCIDENTS_DIR.glob("incident-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files[:limit]:
        try:
            out.append(json.loads(p.read_text()))
        except Exception:
            continue
    return out


async def _get_proposals(client: httpx.AsyncClient) -> list[dict]:
    """Pending/decided remediation proposals from the executor - the only
    place a write action can originate. Returns [] if the executor isn't
    reachable rather than failing the whole page."""
    try:
        resp = await client.get(f"{EXECUTOR_URL}/proposals", timeout=5)
        resp.raise_for_status()
        return resp.json()["proposals"]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Rendering: every section is built as a standalone HTML fragment so
# /api/state can hand them back individually and the page's small bit of JS
# can swap each one in place, without a client-side templating framework.
# ---------------------------------------------------------------------------

def _render_alerts(alerts: list[dict]) -> str:
    if not alerts:
        return "<p class='muted'>No incident-agent alerts currently firing.</p>"
    rows = "".join(
        f"<tr><td>{_esc(a['alertname'])}</td><td><span class='badge badge-{_esc(a['state'])}'>{_esc(a['state'])}</span></td>"
        f"<td>{_esc(a['summary'])}</td><td class='muted'>{_esc(a['starts_at'])}</td></tr>"
        for a in alerts
    )
    return f"<table><tr><th>Alert</th><th>State</th><th>Summary</th><th>Started</th></tr>{rows}</table>"


def _render_pods(pods: list[dict]) -> str:
    if not pods:
        return "<p class='muted'>No pods found (is the cluster up? is kubectl context correct?).</p>"
    rows = []
    for p in pods:
        reason_html = f"<span class='badge badge-bad'>{_esc(p['reason'])}</span>" if p["reason"] else ""
        rows.append(
            f"<tr><td>{_esc(p['name'])}</td><td>{'yes' if p['ready'] else 'no'}</td>"
            f"<td>{_esc(p['phase'])}</td><td>{_esc(p['restarts'])}</td>"
            f"<td>{reason_html}</td></tr>"
        )
    return f"<table><tr><th>Pod</th><th>Ready</th><th>Phase</th><th>Restarts</th><th>Reason</th></tr>{''.join(rows)}</table>"


_STATUS_BADGE = {"investigating": "pending", "diagnosed": "ok", "inconclusive": "bad"}


def _render_investigations(records: list[dict]) -> str:
    if not records:
        return "<p class='muted'>No investigations yet - inject a fault to see one appear here live.</p>"
    cards = []
    for rec in records:
        result = rec.get("result", {}) or {}
        status = result.get("status", "unknown")
        badge = _STATUS_BADGE.get(status, "pending")
        evidence_rows = "".join(
            f"<li><b>{_esc(e.get('tool'))}</b>: {_esc(e.get('summary'))}</li>"
            for e in (result.get("evidence") or [])
        )
        hypothesis = result.get("hypothesis") or ("Investigating…" if status == "investigating" else "(no hypothesis)")
        cards.append(f"""
        <div class="card">
          <div class="card-head">
            <b>{_esc(rec.get('alert', {}).get('alertname', rec.get('incident_id')))}</b>
            <span class="badge badge-{badge}">{_esc(status)}</span>
          </div>
          <p class="muted">{_esc(rec.get('incident_id'))}</p>
          <p>{_esc(hypothesis)}</p>
          {f"<p><b>Root cause:</b> {_esc(result.get('root_cause_label'))} &nbsp; <b>Component:</b> {_esc(result.get('component'))} &nbsp; <b>Confidence:</b> {_esc(result.get('confidence'))}</p>" if status != "investigating" else ""}
          {f"<ul>{evidence_rows}</ul>" if evidence_rows else ""}
          {f"<p><b>Proposed action:</b> <code>{_esc(result['proposed_action'].get('action'))}</code> on <code>{_esc(result['proposed_action'].get('target'))}</code></p>" if result.get('proposed_action') else ""}
        </div>
        """)
    return "".join(cards)


def _render_proposals(proposals: list[dict]) -> str:
    pending = [p for p in proposals if p["status"] == "pending"]
    if not pending:
        return "<p class='muted'>No pending approvals.</p>"
    cards = []
    for p in pending:
        cards.append(f"""
        <div class="card">
          <div class="card-head">
            <b>{_esc(p['alert'].get('alertname', p['incident_id']))}</b>
            <span class="badge badge-pending">PENDING</span>
          </div>
          <p>{_esc(p['hypothesis'])}</p>
          <p><b>Proposed:</b> <code>{_esc(p['action'])}</code> on <code>{_esc(p['target'])}</code></p>
          <form method="post" action="/approve/{p['id']}" style="display:inline">
            <button type="submit" class="btn btn-ok">Approve</button>
          </form>
          <form method="post" action="/reject/{p['id']}" style="display:inline">
            <button type="submit" class="btn btn-bad">Reject</button>
          </form>
        </div>
        """)
    return "".join(cards)


async def _build_state_html() -> dict:
    """Gathers every section's data and renders each to an HTML fragment.
    Used both by the initial page load (so the page isn't blank on first
    paint) and by /api/state (so the page's JS can refresh it in place)."""
    async with httpx.AsyncClient() as client:
        alerts = await _get_alerts(client)
        proposals = await _get_proposals(client)
    pods = _get_pods()
    investigations = _get_investigations()
    return {
        "alerts_html": _render_alerts(alerts),
        "pods_html": _render_pods(pods),
        "investigations_html": _render_investigations(investigations),
        "proposals_html": _render_proposals(proposals),
    }


_PAGE_TEMPLATE = """
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>incident-agent dashboard</title>
<style>
  :root {{
    --bg: #f6f7f9;
    --surface: #ffffff;
    --surface-2: #fafbfc;
    --border: #e2e5ea;
    --text: #1a1d23;
    --text-muted: #6b7280;
    --accent: #3b5bdb;
    --accent-weak: #eef1fd;
    --ok: #16876e;
    --ok-bg: #e4f7f1;
    --bad: #c23b3b;
    --bad-bg: #fcebeb;
    --pending: #b5830a;
    --pending-bg: #fdf3e0;
    --neutral: #6b7280;
    --neutral-bg: #eceef1;
    --radius: 10px;
    --shadow: 0 1px 2px rgba(16, 24, 40, 0.04), 0 1px 3px rgba(16, 24, 40, 0.06);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #10131a;
      --surface: #171b24;
      --surface-2: #1c212c;
      --border: #2a2f3a;
      --text: #e6e8ec;
      --text-muted: #8b93a3;
      --accent: #7f9bff;
      --accent-weak: #1e2740;
      --ok: #3ddba6;
      --ok-bg: #123029;
      --bad: #ff6b6b;
      --bad-bg: #351b1c;
      --pending: #f0b94d;
      --pending-bg: #352b14;
      --neutral: #9aa3b2;
      --neutral-bg: #242a35;
      --shadow: 0 1px 2px rgba(0, 0, 0, 0.3), 0 1px 3px rgba(0, 0, 0, 0.4);
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    max-width: 1080px;
    margin: 0 auto;
    padding: 28px 20px 60px;
    line-height: 1.5;
  }}
  header {{
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 4px;
  }}
  h1 {{ margin: 0; font-size: 1.5rem; letter-spacing: -0.01em; }}
  h2 {{
    margin: 0 0 12px;
    font-size: 1.05rem;
    font-weight: 600;
    color: var(--text);
  }}
  .muted {{ color: var(--text-muted); font-size: 0.85em; }}
  .links {{ margin: 10px 0 28px; display: flex; flex-wrap: wrap; gap: 4px 16px; }}
  .links a {{
    color: var(--accent);
    font-size: 0.85em;
    text-decoration: none;
    font-weight: 500;
  }}
  .links a:hover {{ text-decoration: underline; }}
  section {{ margin-bottom: 32px; }}
  section > .panel {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 18px 20px;
  }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{
    text-align: left;
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
    font-size: 0.88em;
  }}
  th {{
    color: var(--text-muted);
    font-weight: 600;
    text-transform: uppercase;
    font-size: 0.72em;
    letter-spacing: 0.04em;
  }}
  tr:last-child td {{ border-bottom: none; }}
  .card {{
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px 16px;
    margin-bottom: 12px;
  }}
  .card:last-child {{ margin-bottom: 0; }}
  .card-head {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 4px;
  }}
  .card-head b {{ font-size: 0.98em; }}
  .card ul {{ margin: 8px 0 0; padding-left: 20px; font-size: 0.9em; }}
  .card li {{ margin-bottom: 2px; }}
  .card code {{
    background: var(--neutral-bg);
    border-radius: 4px;
    padding: 1px 6px;
    font-size: 0.92em;
  }}
  .badge {{
    display: inline-block;
    padding: 2px 10px;
    border-radius: 999px;
    font-size: 0.72em;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    line-height: 1.6;
  }}
  .badge-active, .badge-bad, .badge-firing {{ background: var(--bad-bg); color: var(--bad); }}
  .badge-pending {{ background: var(--pending-bg); color: var(--pending); }}
  .badge-ok, .badge-diagnosed, .badge-resolved {{ background: var(--ok-bg); color: var(--ok); }}
  .badge-unprocessed, .badge-suppressed {{ background: var(--neutral-bg); color: var(--neutral); }}
  .btn {{
    border: none;
    padding: 7px 16px;
    border-radius: 6px;
    cursor: pointer;
    color: white;
    margin-right: 8px;
    font-size: 0.86em;
    font-weight: 600;
  }}
  .btn-ok {{ background: var(--ok); }}
  .btn-bad {{ background: var(--bad); }}
  .btn:hover {{ filter: brightness(1.08); }}
  #updated {{ font-size: 0.8em; color: var(--text-muted); }}
  @media (max-width: 640px) {{
    body {{ padding: 18px 14px 48px; }}
    table, thead, tbody, th, td, tr {{ font-size: 0.82em; }}
    section > .panel {{ padding: 14px 14px; }}
  }}
</style>
</head>
<body>
  <header>
    <h1>Incident Agent &mdash; Dashboard</h1>
    <p class="muted">Auto-refreshes every {interval}s &middot; <span id="updated"></span></p>
  </header>
  <p class="links">
    Deep dives when the summary below isn't enough:
    <a href="{grafana}" target="_blank">Grafana</a>
    <a href="{prometheus}" target="_blank">Prometheus</a>
    <a href="{alertmanager}" target="_blank">Alertmanager</a>
    <a href="{trace_viewer}" target="_blank">Trace viewer</a>
  </p>

  <section>
    <h2>Active alerts</h2>
    <div class="panel" id="alerts">{alerts_html}</div>
  </section>

  <section>
    <h2>Pod status &mdash; {namespace}</h2>
    <div class="panel" id="pods">{pods_html}</div>
  </section>

  <section>
    <h2>Investigations</h2>
    <div class="panel" id="investigations">{investigations_html}</div>
  </section>

  <section>
    <h2>Pending approvals</h2>
    <div class="panel" id="proposals">{proposals_html}</div>
  </section>

<script>
async function refresh() {{
  try {{
    const resp = await fetch('/api/state');
    const data = await resp.json();
    document.getElementById('alerts').innerHTML = data.alerts_html;
    document.getElementById('pods').innerHTML = data.pods_html;
    document.getElementById('investigations').innerHTML = data.investigations_html;
    document.getElementById('proposals').innerHTML = data.proposals_html;
    document.getElementById('updated').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
  }} catch (e) {{
    document.getElementById('updated').textContent = 'Update failed: ' + e;
  }}
}}
setInterval(refresh, {interval_ms});
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    """Full page, server-rendered with real data on first paint (not
    blank-then-populate) - the inline <script> below then keeps it fresh by
    polling /api/state, which returns the same fragments this route renders
    directly."""
    sections = await _build_state_html()
    return _PAGE_TEMPLATE.format(
        interval=POLL_INTERVAL_MS // 1000,
        interval_ms=POLL_INTERVAL_MS,
        namespace=_esc(NAMESPACE),
        grafana=GRAFANA_URL,
        prometheus=PROMETHEUS_URL,
        alertmanager=ALERTMANAGER_URL,
        trace_viewer=TRACE_VIEWER_URL,
        **sections,
    )


@app.get("/api/state")
async def api_state():
    """JSON endpoint the page's own JS polls - same data as index(), just
    without the surrounding HTML skeleton, so refreshing doesn't reset
    scroll position or re-run the <script> tag."""
    return JSONResponse(await _build_state_html())


@app.post("/approve/{proposal_id}")
async def approve(proposal_id: str):
    """Relay-only, identical in spirit to ui/app.py's approve() - this
    dashboard never decides whether an action is allowed, it only forwards
    the human's decision to the executor, which re-validates everything
    itself before touching the cluster."""
    async with httpx.AsyncClient() as client:
        await client.post(f"{EXECUTOR_URL}/proposals/{proposal_id}/approve")
    return RedirectResponse(url="/", status_code=303)


@app.post("/reject/{proposal_id}")
async def reject(proposal_id: str):
    """Relay-only rejection, same as above."""
    async with httpx.AsyncClient() as client:
        await client.post(f"{EXECUTOR_URL}/proposals/{proposal_id}/reject")
    return RedirectResponse(url="/", status_code=303)


@app.get("/healthz")
def healthz():
    """Liveness check for this process only - does not imply the
    agent/executor/Prometheus/Alertmanager it aggregates are reachable;
    /api/state degrades each section independently if they're not."""
    return {"status": "ok"}
