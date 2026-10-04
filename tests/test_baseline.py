"""Tests for the baseline pipeline: fixed tool calls (no iterative
reasoning), exactly one LLM call. Mocked toolset/LLM - no live cluster."""
import json
from unittest.mock import AsyncMock

import pytest

from agent.llm import ChatResponse
from evals.baseline import investigate_baseline


class FakeToolSet:
    def __init__(self):
        self.calls = []

    async def call(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        return {"fake": "result", "tool": tool_name}


ALERT = {"alertname": "PodOOMKilled", "namespace": "incident-agent"}


@pytest.mark.asyncio
async def test_baseline_always_runs_the_same_fixed_tool_sequence():
    toolset = FakeToolSet()
    llm = AsyncMock()
    llm.chat.return_value = ChatResponse(content=json.dumps({
        "hypothesis": "x", "root_cause_label": "oom_kill", "component": "cart",
        "confidence": 0.8, "evidence": [], "proposed_action": None, "status": "diagnosed",
    }))

    await investigate_baseline(ALERT, toolset, llm=llm)

    called_tools = [name for name, _ in toolset.calls]
    assert called_tools == ["kube_list_pods", "kube_list_events", "search_pod_logs", "kube_list_deployments", "search_runbooks_tool"]


@pytest.mark.asyncio
async def test_baseline_makes_exactly_one_llm_call():
    toolset = FakeToolSet()
    llm = AsyncMock()
    llm.chat.return_value = ChatResponse(content=json.dumps({
        "hypothesis": "x", "root_cause_label": "oom_kill", "component": "cart",
        "confidence": 0.8, "evidence": [], "proposed_action": None, "status": "diagnosed",
    }))

    await investigate_baseline(ALERT, toolset, llm=llm)

    assert llm.chat.call_count == 1
    # no tools param -> baseline cannot make follow-up tool-calling turns
    call_args = llm.chat.call_args
    assert call_args.args[1:] == () or call_args.kwargs.get("tools") is None


@pytest.mark.asyncio
async def test_baseline_returns_tool_call_count_and_tokens():
    toolset = FakeToolSet()
    llm = AsyncMock()
    llm.chat.return_value = ChatResponse(
        content=json.dumps({"hypothesis": "x", "root_cause_label": "oom_kill", "component": "cart",
                             "confidence": 0.8, "evidence": [], "proposed_action": None, "status": "diagnosed"}),
        prompt_tokens=500, completion_tokens=80,
    )

    out = await investigate_baseline(ALERT, toolset, llm=llm)

    assert out["tool_call_count"] == 5
    assert out["prompt_tokens"] == 500
    assert out["completion_tokens"] == 80
    assert out["elapsed_s"] >= 0


@pytest.mark.asyncio
async def test_baseline_does_not_retry_on_invalid_json():
    """Baseline has no bounded-retry loop by design - one shot only."""
    toolset = FakeToolSet()
    llm = AsyncMock()
    llm.chat.return_value = ChatResponse(content="not json at all")

    out = await investigate_baseline(ALERT, toolset, llm=llm)

    assert llm.chat.call_count == 1  # no retry
    assert out["result"]["status"] == "inconclusive"


@pytest.mark.asyncio
async def test_baseline_does_not_retry_on_schema_violation():
    toolset = FakeToolSet()
    llm = AsyncMock()
    llm.chat.return_value = ChatResponse(content=json.dumps({"status": "not_a_valid_enum_value"}))

    out = await investigate_baseline(ALERT, toolset, llm=llm)

    assert llm.chat.call_count == 1
    assert out["result"]["status"] == "inconclusive"
