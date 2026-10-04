"""Proposal store (JSON file) and append-only audit log (JSONL).

propose_action never executes anything - it only ever records a proposal
(sync_from_incidents, below, is literally the only thing that creates one,
and all it does is read the agent's already-saved JSON output and write a
pending row). Execution only ever happens inside approve(), after the
allowlist/bounds check in executor/allowlist.py.
"""
import json
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCIDENTS_DIR = ROOT / "evals" / "runs"
PROPOSALS_PATH = Path(__file__).resolve().parent / "proposals.json"
AUDIT_LOG_PATH = Path(__file__).resolve().parent / "audit.jsonl"


def _load_proposals() -> dict:
    """Read the full proposal table (id -> proposal dict) from disk. A plain
    JSON file is enough here - this is a single-operator tool, not a
    multi-writer database, so there's no locking; see update_proposal()'s
    read-modify-write for the implied assumption of non-concurrent writers.
    """
    if not PROPOSALS_PATH.exists():
        return {}
    return json.loads(PROPOSALS_PATH.read_text())


def _save_proposals(proposals: dict):
    """Overwrite proposals.json wholesale with the current in-memory table.
    `default=str` covers values like timestamps that may not already be
    plain JSON types.
    """
    PROPOSALS_PATH.write_text(json.dumps(proposals, indent=2, default=str))


def append_audit(entry: dict):
    """Add one line to the audit log and never anything else - there is no
    corresponding delete/update function in this module. Every proposed,
    approved_and_executed, approved_but_refused, and rejected event goes
    through here, each gets its own server-side timestamp (not trusted from
    the caller), and the log is opened in append mode so an existing line is
    never rewritten or removed. This is what makes the audit trail a
    reliable record of what the executor actually did, independent of
    whatever the proposals.json snapshot currently says.
    """
    entry = {"timestamp": time.time(), **entry}
    with AUDIT_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def read_audit() -> list[dict]:
    """Parse the JSONL audit log back into a list of dicts, in the order
    they were written (oldest first) - callers that want newest-first, like
    app.py's /audit endpoint, reverse it themselves."""
    if not AUDIT_LOG_PATH.exists():
        return []
    return [json.loads(line) for line in AUDIT_LOG_PATH.read_text().splitlines() if line.strip()]


def sync_from_incidents() -> int:
    """Scan evals/runs/incident-*.json for diagnosed incidents with a
    proposed_action that aren't already tracked, and record them as new
    pending proposals.

    This function is the only place in the entire codebase that constructs a
    Proposal - the agent process never calls it and has no handle on
    proposals.json at all; it only ever writes its investigation result
    (including the proposed_action it would like a human to consider) to
    evals/runs/. Turning that finished investigation into something a human
    can actually approve/reject happens here, on the executor side, strictly
    after the investigation is done. Every new proposal is also immediately
    logged to the audit trail with event "proposed", so the audit log has a
    record of every action ever suggested, not just the ones later approved.

    Nothing in this function executes an action - it only ever sets
    status="pending" and leaves execution to approve_proposal() in app.py.
    Returns the number of new proposals created.
    """
    proposals = _load_proposals()
    known_incident_ids = {p["incident_id"] for p in proposals.values()}
    created = 0
    for path in INCIDENTS_DIR.glob("incident-*.json"):
        record = json.loads(path.read_text())
        incident_id = record.get("incident_id")
        if incident_id in known_incident_ids:
            continue
        result = record.get("result", {})
        action = result.get("proposed_action")
        if not action:
            continue
        proposal_id = str(uuid.uuid4())
        proposals[proposal_id] = {
            "id": proposal_id,
            "incident_id": incident_id,
            "alert": record.get("alert", {}),
            "hypothesis": result.get("hypothesis"),
            "root_cause_label": result.get("root_cause_label"),
            "confidence": result.get("confidence"),
            "action": action.get("action"),
            "target": action.get("target"),
            "params": action.get("params", {}),
            "status": "pending",
            "created_at": record.get("completed_at", time.time()),
            "decided_at": None,
            "executed": False,
            "execution_result": None,
            "execution_error": None,
        }
        append_audit({"proposal_id": proposal_id, "incident_id": incident_id, "event": "proposed", "action": action.get("action"), "target": action.get("target")})
        created += 1
    if created:
        _save_proposals(proposals)
    return created


def list_proposals() -> list[dict]:
    """Proposal listing used by GET /proposals. Syncing first means a newly
    finished investigation becomes visible to a human reviewer the next time
    anyone lists proposals, without needing a separate poller process."""
    sync_from_incidents()
    return sorted(_load_proposals().values(), key=lambda p: p["created_at"], reverse=True)


def get_proposal(proposal_id: str) -> dict | None:
    """Look up a single proposal by id, or None if it doesn't exist (callers
    in app.py turn that into a 404)."""
    return _load_proposals().get(proposal_id)


def update_proposal(proposal_id: str, **fields) -> dict:
    """Read-modify-write a proposal's fields (used to record status, decision
    time, and execution outcome after an approve/reject). Raises KeyError
    for an unknown id rather than silently creating one, since every
    proposal must have originated from sync_from_incidents()."""
    proposals = _load_proposals()
    if proposal_id not in proposals:
        raise KeyError(f"no such proposal: {proposal_id}")
    proposals[proposal_id].update(fields)
    _save_proposals(proposals)
    return proposals[proposal_id]
