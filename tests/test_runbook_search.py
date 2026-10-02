"""Tests for the runbook_search tool: index build, embedding search, and
the BM25 fallback path (forced by monkeypatching the embed call)."""
import pytest

from tools import runbook_search as rs


@pytest.fixture(autouse=True)
def fresh_index():
    rs.DB_PATH.unlink(missing_ok=True)
    yield
    rs.DB_PATH.unlink(missing_ok=True)


def test_build_index_indexes_all_runbooks():
    result = rs.build_index()
    assert result["status"] == "built"
    md_files = list(rs.RUNBOOKS_DIR.glob("*.md"))
    assert result["doc_count"] == len(md_files)
    assert result["doc_count"] >= 6


def test_build_index_is_idempotent_without_force():
    rs.build_index()
    result = rs.build_index()
    assert result["status"] == "exists"


def test_search_runbooks_returns_structured_results():
    result = rs.search_runbooks("pod restart count climbing, out of memory", top_k=3)
    assert result["mode"] in ("embedding", "bm25")
    assert len(result["results"]) <= 3
    top = result["results"][0]
    assert {"title", "path", "root_cause_label", "score", "excerpt"} <= top.keys()


def test_search_runbooks_oom_query_ranks_oom_runbook_first():
    result = rs.search_runbooks("pod keeps getting OOMKilled, memory limit too low", top_k=3)
    assert result["results"][0]["root_cause_label"] == "oom_kill"


def test_search_runbooks_kafka_query_ranks_kafka_runbook_first():
    result = rs.search_runbooks("kafka consumer group lag is growing", top_k=3)
    assert result["results"][0]["root_cause_label"] == "kafka_consumer_lag"


def test_search_runbooks_bm25_fallback_when_embeddings_unavailable(monkeypatch):
    monkeypatch.setattr(rs, "_embed", lambda text: None)
    result = rs.search_runbooks("crash loop bad environment variable config", top_k=3)
    assert result["mode"] == "bm25"
    assert len(result["results"]) >= 1


def test_search_runbooks_respects_max_results_cap():
    result = rs.search_runbooks("alert", top_k=rs.MAX_RESULTS + 50)
    assert len(result["results"]) <= rs.MAX_RESULTS
