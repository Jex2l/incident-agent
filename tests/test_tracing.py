"""Tests for OpenTelemetry instrumentation: every LLM call and tool call
produces a span with truncated inputs/outputs, token counts, and latency.
Uses InMemorySpanExporter throughout - no live LLM or cluster needed."""
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent.llm import ChatResponse, OllamaProvider, ToolCall
from agent.mcp_client import ToolSet
from agent.tracing import JSONFileSpanExporter, get_tracer, reset_tracer_for_testing, truncate


@pytest.fixture
def span_exporter():
    exporter = InMemorySpanExporter()
    reset_tracer_for_testing(exporter)
    yield exporter


def test_truncate_short_value_unchanged():
    assert truncate("hello") == "hello"


def test_truncate_long_value_is_cut_with_marker():
    long_text = "x" * 1000
    result = truncate(long_text, max_chars=100)
    assert len(result) < 1000
    assert result.startswith("x" * 100)
    assert "truncated" in result
    assert "1000 chars total" in result


def test_truncate_serializes_non_string():
    result = truncate({"a": 1, "b": [1, 2, 3]})
    assert json.loads(result.split("...")[0]) if "..." in result else json.loads(result)


# --- llm.chat() span ---

@pytest.mark.asyncio
async def test_llm_chat_creates_span_with_token_counts_and_latency(span_exporter, monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "message": {"role": "assistant", "content": "the hypothesis is X"},
                "prompt_eval_count": 123,
                "eval_count": 45,
            }

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json):
            return FakeResponse()

    monkeypatch.setattr("agent.llm.httpx.AsyncClient", lambda **kw: FakeAsyncClient())

    provider = OllamaProvider(model="qwen2.5:7b")
    response = await provider.chat([{"role": "user", "content": "investigate this alert"}])

    assert response.content == "the hypothesis is X"
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "llm.chat"
    assert span.attributes["llm.model"] == "qwen2.5:7b"
    assert span.attributes["llm.prompt_tokens"] == 123
    assert span.attributes["llm.completion_tokens"] == 45
    assert span.end_time > span.start_time  # latency is captured via span duration


@pytest.mark.asyncio
async def test_llm_chat_span_records_error_on_failure(span_exporter, monkeypatch):
    class FailingAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json):
            raise ConnectionError("ollama unreachable")

    monkeypatch.setattr("agent.llm.httpx.AsyncClient", lambda **kw: FailingAsyncClient())

    provider = OllamaProvider()
    with pytest.raises(ConnectionError):
        await provider.chat([{"role": "user", "content": "hi"}])

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code.name == "ERROR"


@pytest.mark.asyncio
async def test_llm_chat_input_output_previews_are_truncated(span_exporter, monkeypatch):
    long_message = "a" * 2000

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "b" * 2000}, "prompt_eval_count": 1, "eval_count": 1}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json):
            return FakeResponse()

    monkeypatch.setattr("agent.llm.httpx.AsyncClient", lambda **kw: FakeAsyncClient())

    provider = OllamaProvider()
    await provider.chat([{"role": "user", "content": long_message}])

    span = span_exporter.get_finished_spans()[0]
    assert len(span.attributes["llm.input_preview"]) < 2000
    assert len(span.attributes["llm.output_preview"]) < 2000


# --- tool.call() span ---

@pytest.mark.asyncio
async def test_tool_call_creates_span_with_name_args_and_result(span_exporter):
    toolset = ToolSet.__new__(ToolSet)  # bypass __init__ (no subprocess spawn)
    toolset._tool_to_server = {"kube_list_pods": "kube_read"}

    fake_content = MagicMock()
    fake_content.text = json.dumps({"pods": [{"name": "cart-abc"}]})
    fake_result = MagicMock(isError=False, content=[fake_content])

    fake_session = MagicMock()
    fake_session.call_tool = AsyncMock(return_value=fake_result)
    toolset._sessions = {"kube_read": fake_session}

    output = await toolset.call("kube_list_pods", {"namespace": "incident-agent"})

    assert output == {"pods": [{"name": "cart-abc"}]}
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "tool.call"
    assert span.attributes["tool.name"] == "kube_list_pods"
    assert "incident-agent" in span.attributes["tool.arguments"]
    assert "cart-abc" in span.attributes["tool.result_preview"]


@pytest.mark.asyncio
async def test_tool_call_span_marks_error_status_on_tool_error(span_exporter):
    toolset = ToolSet.__new__(ToolSet)
    toolset._tool_to_server = {}  # unknown tool -> error path, no session needed

    output = await toolset.call("nonexistent_tool", {})

    assert "error" in output
    span = span_exporter.get_finished_spans()[0]
    assert span.status.status_code.name == "ERROR"


# --- JSONFileSpanExporter ---

def test_json_file_exporter_writes_valid_jsonl(tmp_path, monkeypatch):
    import agent.tracing as tracing_module
    monkeypatch.setattr(tracing_module, "SPANS_PATH", tmp_path / "spans.jsonl")

    exporter = JSONFileSpanExporter()
    reset_tracer_for_testing(exporter)
    # reset_tracer_for_testing builds its own provider but we want OUR
    # exporter instance wired to the patched SPANS_PATH, so use it directly.
    tracer = get_tracer()
    with tracer.start_as_current_span("investigate") as span:
        span.set_attribute("incident.id", "incident-test-1")
        time.sleep(0.01)

    lines = (tmp_path / "spans.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["name"] == "investigate"
    assert record["attributes"]["incident.id"] == "incident-test-1"
    assert record["duration_ms"] > 0
    assert len(record["trace_id"]) == 32
    assert len(record["span_id"]) == 16


def test_json_file_exporter_parent_child_share_trace_id(tmp_path, monkeypatch):
    import agent.tracing as tracing_module
    monkeypatch.setattr(tracing_module, "SPANS_PATH", tmp_path / "spans.jsonl")

    exporter = JSONFileSpanExporter()
    reset_tracer_for_testing(exporter)
    tracer = get_tracer()
    with tracer.start_as_current_span("investigate"):
        with tracer.start_as_current_span("tool.call") as child:
            child.set_attribute("tool.name", "kube_list_pods")

    lines = (tmp_path / "spans.jsonl").read_text().strip().splitlines()
    records = [json.loads(l) for l in lines]
    assert len(records) == 2
    trace_ids = {r["trace_id"] for r in records}
    assert len(trace_ids) == 1  # parent and child share one trace_id

    child_record = next(r for r in records if r["name"] == "tool.call")
    parent_record = next(r for r in records if r["name"] == "investigate")
    assert child_record["parent_span_id"] == parent_record["span_id"]
