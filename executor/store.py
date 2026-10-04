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
    if not PROPOSALS_PATH.exists():
        return {}
    return json.loads(PROPOSALS_PATH.read_text())


def _save_proposals(proposals: dict):
    PROPOSALS_PATH.write_text(json.dumps(proposals, indent=2, default=str))


def append_audit(entry: dict):
    entry = {"timestamp": time.time(), **entry}
    with AUDIT_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def read_audit() -> list[dict]:
    if not AUDIT_LOG_PATH.exists():
        return []
    return [json.loads(line) for line in AUDIT_LOG_PATH.read_text().splitlines() if line.strip()]


def sync_from_incidents() -> int:
    """Scan evals/runs/incident-*.json for diagnosed incidents with a
    proposed_action that aren't already tracked, and record them as new
    pending proposals. This is the only place a proposal is created -
    nothing here executes anything. Returns the number of new proposals."""
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
    sync_from_incidents()
    return sorted(_load_proposals().values(), key=lambda p: p["created_at"], reverse=True)


def get_proposal(proposal_id: str) -> dict | None:
    return _load_proposals().get(proposal_id)


def update_proposal(proposal_id: str, **fields) -> dict:
    proposals = _load_proposals()
    if proposal_id not in proposals:
        raise KeyError(f"no such proposal: {proposal_id}")
    proposals[proposal_id].update(fields)
    _save_proposals(proposals)
    return proposals[proposal_id]
