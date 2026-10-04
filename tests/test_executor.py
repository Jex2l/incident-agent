"""Tests for the approval gate and executor. The three things that must be
true regardless of anything else: an unapproved action never executes, a
non-allowlisted action is refused even if "approved", and every decision
is written to the append-only audit log.
"""
import json
import time
import uuid

import pytest

from executor import store
from executor.allowlist import ActionError, execute_action
from executor.app import approve_proposal, reject_proposal


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROPOSALS_PATH", tmp_path / "proposals.json")
    monkeypatch.setattr(store, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(store, "INCIDENTS_DIR", tmp_path / "incidents")
    (tmp_path / "incidents").mkdir()
    yield


def _make_proposal(action="scale", target="cart", params=None) -> str:
    """Mirrors what sync_from_incidents() does for one incident: merge into
    the existing store (not overwrite it) and record the same 'proposed'
    audit entry real proposal creation does."""
    proposal_id = str(uuid.uuid4())
    proposals = store._load_proposals()
    proposals[proposal_id] = {
        "id": proposal_id, "incident_id": f"incident-test-{proposal_id[:8]}", "alert": {}, "hypothesis": "test",
        "root_cause_label": "test", "confidence": 0.5, "action": action, "target": target,
        "params": params or {}, "status": "pending", "created_at": time.time(),
        "decided_at": None, "executed": False, "execution_result": None, "execution_error": None,
    }
    store._save_proposals(proposals)
    store.append_audit({"proposal_id": proposal_id, "incident_id": proposals[proposal_id]["incident_id"], "event": "proposed", "action": action, "target": target})
    return proposal_id


# --- allowlist / bounds (no store involved) ---

def test_non_allowlisted_action_is_refused(monkeypatch):
    calls = []
    monkeypatch.setattr("executor.allowlist._run_kubectl", lambda *a: calls.append(a))
    with pytest.raises(ActionError, match="not in the allowlist"):
        execute_action("delete_everything", "cart", {})
    assert calls == [], "a non-allowlisted action must never reach kubectl"


def test_non_allowlisted_target_is_refused(monkeypatch):
    calls = []
    monkeypatch.setattr("executor.allowlist._run_kubectl", lambda *a: calls.append(a))
    with pytest.raises(ActionError, match="not in the allowed targets"):
        execute_action("rollout_undo", "kube-apiserver", {})
    assert calls == [], "a non-allowlisted target must never reach kubectl"


def test_scale_out_of_bounds_is_refused(monkeypatch):
    calls = []
    monkeypatch.setattr("executor.allowlist._run_kubectl", lambda *a: calls.append(a))
    with pytest.raises(ActionError, match="outside the allowed bounds"):
        execute_action("scale", "cart", {"replicas": 99})
    assert calls == [], "an out-of-bounds scale must never reach kubectl"


def test_scale_within_bounds_is_allowed(monkeypatch):
    calls = []
    monkeypatch.setattr("executor.allowlist._run_kubectl", lambda *a: calls.append(a) or "scaled")
    result = execute_action("scale", "cart", {"replicas": 2})
    assert result == "scaled"
    assert calls == [("-n", "incident-agent", "scale", "deployment/cart", "--replicas=2")]


# --- store / app: proposal never executes until approved ---

def test_creating_a_proposal_never_executes_anything(monkeypatch):
    calls = []
    monkeypatch.setattr("executor.allowlist._run_kubectl", lambda *a: calls.append(a))
    _make_proposal()
    assert calls == [], "propose_action must only ever record a proposal, never execute"


def test_reject_never_executes(monkeypatch):
    calls = []
    monkeypatch.setattr("executor.allowlist._run_kubectl", lambda *a: calls.append(a))
    proposal_id = _make_proposal()
    result = reject_proposal(proposal_id)
    assert result["status"] == "rejected"
    assert result["executed"] is False
    assert calls == [], "a rejected proposal must never execute"


def test_approve_executes_allowlisted_action(monkeypatch):
    calls = []
    monkeypatch.setattr("executor.allowlist._run_kubectl", lambda *a: calls.append(a) or "rolled back")
    proposal_id = _make_proposal(action="rollout_undo", target="cart")
    result = approve_proposal(proposal_id)
    assert result["status"] == "approved"
    assert result["executed"] is True
    assert result["execution_result"] == "rolled back"
    assert len(calls) == 1


def test_approve_of_non_allowlisted_action_still_refuses(monkeypatch):
    """A proposal could in principle contain anything (the model wrote it) -
    approval must re-validate against the allowlist at execution time, not
    trust that the proposal itself is safe."""
    calls = []
    monkeypatch.setattr("executor.allowlist._run_kubectl", lambda *a: calls.append(a))
    proposal_id = _make_proposal(action="delete_everything", target="cart")
    result = approve_proposal(proposal_id)
    assert result["status"] == "approved"
    assert result["executed"] is False
    assert result["execution_error"] is not None
    assert calls == [], "even an approved non-allowlisted action must never reach kubectl"


def test_cannot_approve_twice(monkeypatch):
    monkeypatch.setattr("executor.allowlist._run_kubectl", lambda *a: "ok")
    proposal_id = _make_proposal(action="rollout_undo", target="cart")
    approve_proposal(proposal_id)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        approve_proposal(proposal_id)
    assert exc_info.value.status_code == 409


# --- audit log ---

def test_every_decision_is_audited(monkeypatch):
    monkeypatch.setattr("executor.allowlist._run_kubectl", lambda *a: "ok")
    approved_id = _make_proposal(action="rollout_undo", target="cart")
    rejected_id = _make_proposal(action="rollout_undo", target="frontend")
    approve_proposal(approved_id)
    reject_proposal(rejected_id)

    entries = store.read_audit()
    events_by_proposal = {}
    for e in entries:
        events_by_proposal.setdefault(e["proposal_id"], []).append(e["event"])

    assert "proposed" in events_by_proposal[approved_id]
    assert "approved_and_executed" in events_by_proposal[approved_id]
    assert "proposed" in events_by_proposal[rejected_id]
    assert "rejected" in events_by_proposal[rejected_id]


def test_audit_log_is_append_only_jsonl(monkeypatch):
    monkeypatch.setattr("executor.allowlist._run_kubectl", lambda *a: "ok")
    proposal_id = _make_proposal(action="rollout_undo", target="cart")
    approve_proposal(proposal_id)

    raw = store.AUDIT_LOG_PATH.read_text()
    lines = [l for l in raw.splitlines() if l.strip()]
    for line in lines:
        json.loads(line)  # must be valid standalone JSON per line
    assert len(lines) >= 2  # "proposed" + "approved_and_executed"


def test_sync_from_incidents_creates_pending_proposals_only(tmp_path):
    incident = {
        "incident_id": "incident-HighErrorRate-123",
        "alert": {"alertname": "HighErrorRate"},
        "result": {
            "hypothesis": "bad deploy",
            "root_cause_label": "bad_deploy",
            "confidence": 0.8,
            "proposed_action": {"action": "rollout_undo", "target": "cart", "params": {}},
            "status": "diagnosed",
        },
        "completed_at": time.time(),
    }
    (store.INCIDENTS_DIR / "incident-HighErrorRate-123.json").write_text(json.dumps(incident))

    created = store.sync_from_incidents()
    assert created == 1
    proposals = store.list_proposals()
    assert len(proposals) == 1
    assert proposals[0]["status"] == "pending"
    assert proposals[0]["executed"] is False


def test_sync_skips_incidents_without_proposed_action(tmp_path):
    incident = {
        "incident_id": "incident-Inconclusive-456",
        "alert": {},
        "result": {"hypothesis": "unclear", "proposed_action": None, "status": "inconclusive"},
        "completed_at": time.time(),
    }
    (store.INCIDENTS_DIR / "incident-Inconclusive-456.json").write_text(json.dumps(incident))

    created = store.sync_from_incidents()
    assert created == 0
