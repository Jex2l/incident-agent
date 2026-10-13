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


_STATUS_CLASS = {"pending": "pending", "approved": "ok", "rejected": "bad"}


def _proposal_card(p: dict) -> str:
    """Render one proposal (as returned by the executor's /proposals API)
    as an HTML card: pending proposals get Approve/Reject buttons that
    POST back to this app's own /approve and /reject routes; already
    decided proposals show their outcome instead (including whether an
    approved action actually executed, since approval and execution are
    separate steps on the executor side)."""
    status_class = _STATUS_CLASS.get(p["status"], "unprocessed")
    actions_html = ""
    if p["status"] == "pending":
        actions_html = f"""
        <form method="post" action="/approve/{p['id']}" class="inline-form">
          <button type="submit" class="btn btn-ok">Approve</button>
        </form>
        <form method="post" action="/reject/{p['id']}" class="inline-form">
          <button type="submit" class="btn btn-bad">Reject</button>
        </form>
        """
    else:
        exec_note = ""
        if p["status"] == "approved":
            exec_note = (f"executed: {_esc(p['execution_result'])}" if p["executed"]
                         else f"approved but refused at execution: {_esc(p['execution_error'])}")
        actions_html = f"<span class='outcome'><em>{_esc(p['status'])}</em> &mdash; {exec_note}</span>"

    evidence_rows = "".join(
        f"<li><b>{_esc(e.get('tool'))}</b>: {_esc(e.get('summary'))}</li>"
        for e in (p.get("alert", {}) or {}).get("evidence", [])
    )

    return f"""
    <div class="card">
      <div class="card-head">
        <h3>{_esc(p['alert'].get('alertname', p['incident_id']))}</h3>
        <span class="badge badge-{status_class}">{_esc(p['status']).upper()}</span>
      </div>
      <p><b>Incident:</b> {_esc(p['incident_id'])}</p>
      <p><b>Hypothesis:</b> {_esc(p['hypothesis'])}</p>
      <p><b>Root cause:</b> {_esc(p['root_cause_label'])} <span class="muted">(confidence {_esc(p['confidence'])})</span></p>
      <p><b>Proposed action:</b> <code>{_esc(p['action'])}</code> on <code>{_esc(p['target'])}</code>
         {f"with params {_esc(p['params'])}" if p.get('params') else ""}</p>
      {f"<ul>{evidence_rows}</ul>" if evidence_rows else ""}
      <div class="card-actions">{actions_html}</div>
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

    cards = "".join(_proposal_card(p) for p in proposals) or "<p class='muted'>No proposals yet.</p>"
    return f"""
    <html>
    <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>incident-agent approvals</title>
    <style>
      :root {{
        --bg: #f6f7f9;
        --surface: #fafbfc;
        --border: #e2e5ea;
        --text: #1a1d23;
        --text-muted: #6b7280;
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
          --surface: #1c212c;
          --border: #2a2f3a;
          --text: #e6e8ec;
          --text-muted: #8b93a3;
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
        max-width: 760px;
        margin: 0 auto;
        padding: 32px 20px 60px;
        line-height: 1.5;
      }}
      h1 {{ margin: 0 0 6px; font-size: 1.4rem; letter-spacing: -0.01em; }}
      p.intro {{ color: var(--text-muted); margin: 0 0 28px; font-size: 0.92em; }}
      .muted {{ color: var(--text-muted); }}
      .card {{
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: var(--radius);
        box-shadow: var(--shadow);
        padding: 18px 20px;
        margin-bottom: 16px;
      }}
      .card h3 {{ margin: 0; font-size: 1.02rem; }}
      .card p {{ margin: 8px 0; font-size: 0.92em; }}
      .card-head {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        justify-content: space-between;
        align-items: center;
      }}
      .card code {{
        background: var(--neutral-bg);
        border-radius: 4px;
        padding: 1px 6px;
        font-size: 0.92em;
      }}
      .card ul {{ margin: 6px 0; padding-left: 20px; font-size: 0.9em; }}
      .card-actions {{ margin-top: 12px; }}
      .inline-form {{ display: inline; }}
      .outcome {{ font-size: 0.9em; color: var(--text-muted); }}
      .badge {{
        display: inline-block;
        padding: 3px 12px;
        border-radius: 999px;
        font-size: 0.75em;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.03em;
      }}
      .badge-ok {{ background: var(--ok-bg); color: var(--ok); }}
      .badge-bad {{ background: var(--bad-bg); color: var(--bad); }}
      .badge-pending {{ background: var(--pending-bg); color: var(--pending); }}
      .badge-unprocessed {{ background: var(--neutral-bg); color: var(--neutral); }}
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
      @media (max-width: 600px) {{
        body {{ padding: 20px 14px 48px; }}
        .card {{ padding: 14px 16px; }}
      }}
    </style>
    </head>
    <body>
      <h1>Incident Agent &mdash; Approval Queue</h1>
      <p class="intro">Proposals are read-only suggestions from the agent. Nothing executes until you click Approve.</p>
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
