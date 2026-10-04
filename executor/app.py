"""Executor service: the only thing in this project allowed to perform a
write action against the cluster, and only after explicit human approval.
Runs as a separate process/service from the agent on purpose - the agent
can only ever create a pending proposal (via propose_action in its output,
synced by executor/store.py), never execute one.
"""
import time

from fastapi import FastAPI, HTTPException

from executor.allowlist import ActionError, execute_action
from executor.store import append_audit, get_proposal, list_proposals, update_proposal

app = FastAPI(title="incident-agent executor")


@app.get("/healthz")
def healthz():
    """Liveness probe only - carries no information about proposal state."""
    return {"status": "ok"}


@app.get("/proposals")
def get_proposals():
    """List all known proposals (pending, approved, or rejected).

    list_proposals() first calls store.sync_from_incidents() to pick up any
    newly-finished investigations, so this endpoint is also what makes a
    fresh proposal show up for a human to act on in the first place.
    """
    return {"proposals": list_proposals()}


@app.get("/proposals/{proposal_id}")
def get_one_proposal(proposal_id: str):
    """Fetch a single proposal by id, 404 if it doesn't exist."""
    proposal = get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="no such proposal")
    return proposal


@app.post("/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: str):
    """The only endpoint in the whole system that can lead to a write
    against the cluster, and even here it isn't automatic: a proposal must
    already exist and be "pending" (never approved or rejected before - the
    status check below prevents double-execution via a replayed or
    concurrent request), and approving it only *attempts* execution.

    The actual write happens inside execute_action(), which independently
    re-validates the action/target/params against the allowlist regardless
    of the fact that a human just approved this proposal - approval and
    allowlist-validity are two separate gates, and both must pass.
    """
    proposal = get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="no such proposal")
    if proposal["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"proposal is already '{proposal['status']}', not pending")

    try:
        output = execute_action(proposal["action"], proposal["target"], proposal.get("params"))
        updated = update_proposal(
            proposal_id,
            status="approved", decided_at=time.time(),
            executed=True, execution_result=output, execution_error=None,
        )
        append_audit({
            "proposal_id": proposal_id, "incident_id": proposal["incident_id"], "event": "approved_and_executed",
            "action": proposal["action"], "target": proposal["target"], "params": proposal.get("params"),
            "result": output,
        })
    except ActionError as e:
        # Approval was granted but the allowlist/bounds check at execution
        # time rejected it anyway - still never executes, and this is
        # recorded distinctly from a plain reject.
        updated = update_proposal(
            proposal_id,
            status="approved", decided_at=time.time(),
            executed=False, execution_result=None, execution_error=str(e),
        )
        append_audit({
            "proposal_id": proposal_id, "incident_id": proposal["incident_id"], "event": "approved_but_refused",
            "action": proposal["action"], "target": proposal["target"], "params": proposal.get("params"),
            "error": str(e),
        })
    return updated


@app.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: str):
    """Mark a proposal rejected. This path never calls execute_action() or
    touches allowlist.py at all - rejecting a proposal is purely a status
    update plus an audit entry, so there is no way for a reject to have a
    side effect on the cluster.
    """
    proposal = get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="no such proposal")
    if proposal["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"proposal is already '{proposal['status']}', not pending")

    updated = update_proposal(proposal_id, status="rejected", decided_at=time.time(), executed=False)
    append_audit({
        "proposal_id": proposal_id, "incident_id": proposal["incident_id"], "event": "rejected",
        "action": proposal["action"], "target": proposal["target"],
    })
    return updated


@app.get("/audit")
def audit_log(limit: int = 50):
    """Read-only view of the append-only audit log (executor/audit.jsonl).
    Every proposal/approve/reject/execute event that has ever happened is in
    there in order; this just tails the most recent `limit` entries, newest
    first, for display. Nothing here can mutate the log.
    """
    from executor.store import read_audit
    entries = read_audit()
    return {"entries": entries[-limit:][::-1]}
