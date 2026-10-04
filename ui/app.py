"""Approval UI: a simple FastAPI + server-rendered HTML page. Talks to the
executor service only over HTTP (never imports its code or touches its
store directly) - this is a presentation layer, not a privileged one; the
executor is the only thing with write-capable credentials.
"""
import html
import os

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

EXECUTOR_URL = os.environ.get("EXECUTOR_URL", "http://localhost:8020")

app = FastAPI(title="incident-agent approval UI")


def _esc(value) -> str:
    """HTML-escape a value for safe interpolation into the hand-built markup
    below (this UI has no template engine). Returns "" for None instead of
    the literal string "None" so empty fields render as blank, not noise."""
    return html.escape(str(value)) if value is not None else ""


def _proposal_card(p: dict) -> str:
    """Render one proposal (as returned by the executor's /proposals API)
    as an HTML card: pending proposals get Approve/Reject buttons that
    POST back to this app's own /approve and /reject routes; already
    decided proposals show their outcome instead (including whether an
    approved action actually executed, since approval and execution are
    separate steps on the executor side)."""
    status_color = {"pending": "#b58900", "approved": "#2aa198", "rejected": "#dc322f"}.get(p["status"], "#657b83")
    actions_html = ""
    if p["status"] == "pending":
        actions_html = f"""
        <form method="post" action="/approve/{p['id']}" style="display:inline">
          <button type="submit" style="background:#2aa198;color:white;border:none;padding:6px 14px;border-radius:4px;cursor:pointer">Approve</button>
        </form>
        <form method="post" action="/reject/{p['id']}" style="display:inline;margin-left:8px">
          <button type="submit" style="background:#dc322f;color:white;border:none;padding:6px 14px;border-radius:4px;cursor:pointer">Reject</button>
        </form>
        """
    else:
        exec_note = ""
        if p["status"] == "approved":
            exec_note = (f"executed: {_esc(p['execution_result'])}" if p["executed"]
                         else f"approved but refused at execution: {_esc(p['execution_error'])}")
        actions_html = f"<em>{_esc(p['status'])}</em> &mdash; {exec_note}"

    evidence_rows = "".join(
        f"<li><b>{_esc(e.get('tool'))}</b>: {_esc(e.get('summary'))}</li>"
        for e in (p.get("alert", {}) or {}).get("evidence", [])
    )

    return f"""
    <div style="border:1px solid #ddd;border-radius:6px;padding:16px;margin-bottom:16px">
      <div style="display:flex;justify-content:space-between">
        <h3 style="margin:0">{_esc(p['alert'].get('alertname', p['incident_id']))}</h3>
        <span style="color:{status_color};font-weight:bold">{_esc(p['status']).upper()}</span>
      </div>
      <p><b>Incident:</b> {_esc(p['incident_id'])}</p>
      <p><b>Hypothesis:</b> {_esc(p['hypothesis'])}</p>
      <p><b>Root cause:</b> {_esc(p['root_cause_label'])} (confidence {_esc(p['confidence'])})</p>
      <p><b>Proposed action:</b> <code>{_esc(p['action'])}</code> on <code>{_esc(p['target'])}</code>
         {f"with params {_esc(p['params'])}" if p.get('params') else ""}</p>
      {f"<ul>{evidence_rows}</ul>" if evidence_rows else ""}
      <div style="margin-top:8px">{actions_html}</div>
    </div>
    """


@app.get("/", response_class=HTMLResponse)
async def index():
    """Approval queue landing page. Fetches the current proposal list from
    the executor over HTTP (this process holds no proposal state itself)
    and renders it server-side; there's no client-side JS, so every
    approve/reject is a plain form POST followed by a redirect back here."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{EXECUTOR_URL}/proposals")
        resp.raise_for_status()
        proposals = resp.json()["proposals"]

    cards = "".join(_proposal_card(p) for p in proposals) or "<p>No proposals yet.</p>"
    return f"""
    <html>
    <head><title>incident-agent approvals</title></head>
    <body style="font-family:sans-serif;max-width:800px;margin:40px auto;padding:0 20px">
      <h1>Incident Agent &mdash; Approval Queue</h1>
      <p style="color:#657b83">Proposals are read-only suggestions from the agent. Nothing executes until you click Approve.</p>
      {cards}
    </body>
    </html>
    """


@app.post("/approve/{proposal_id}")
async def approve(proposal_id: str):
    """Forward the human's approval decision to the executor; this app does
    not itself decide whether the action is allowed to run (that allowlist
    check lives in the executor) and does not execute anything directly -
    it is only a relay. 303 redirect back to "/" so a page refresh doesn't
    resubmit the POST."""
    async with httpx.AsyncClient() as client:
        await client.post(f"{EXECUTOR_URL}/proposals/{proposal_id}/approve")
    return RedirectResponse(url="/", status_code=303)


@app.post("/reject/{proposal_id}")
async def reject(proposal_id: str):
    """Forward a rejection to the executor; same relay-only role as approve()."""
    async with httpx.AsyncClient() as client:
        await client.post(f"{EXECUTOR_URL}/proposals/{proposal_id}/reject")
    return RedirectResponse(url="/", status_code=303)


@app.get("/healthz")
def healthz():
    """Liveness check for this UI process only - does not probe the
    executor, so a healthy UI can still show stale/unreachable data if the
    executor is down (the index route's own httpx call would fail there)."""
    return {"status": "ok"}
