"""LLM provider interface. Everything else in agent/ talks to this, not to
Ollama directly, so a different free-tier provider (Gemini, Groq) can be
added later without touching the orchestrator.

The abstraction point is deliberately narrow: one `chat()` method that takes
OpenAI-style messages/tools and returns a normalized ChatResponse. Whatever
provider-specific request/response shape exists underneath (Ollama's
`/api/chat` JSON shape here) is translated at the edge, inside the provider
class, so `orchestrator.py` never has to know which backend it's talking to.
"""
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx
from opentelemetry.trace import Status, StatusCode

from agent.tracing import get_tracer, truncate

# Which provider class to instantiate (see get_default_provider at the
# bottom). Only "ollama" is implemented; the env var exists so a future
# provider can be selected without a code change.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama")
# Default model: qwen2.5:7b, chosen because it's small enough to run on
# consumer hardware (see docs/DECISIONS.md) while still supporting tool
# calling. Override per-run with `LLM_MODEL=... make eval` etc.
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen2.5:7b")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


@dataclass
class ToolCall:
    """One tool invocation the model asked for in a single turn. `id` is a
    position-based string (Ollama doesn't issue real call IDs like OpenAI
    does), used only to correlate the call with its result message.
    """
    id: str
    name: str
    arguments: dict


@dataclass
class ChatResponse:
    """Normalized result of one LLM turn, regardless of provider.

    `content` is the model's free-text reply (None if it only emitted tool
    calls). `tool_calls` is empty when the model produced a final answer
    instead of requesting more tool use. Token counts feed directly into
    BudgetTracker.record_tokens() in the orchestrator loop.
    """
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMProvider(ABC):
    """Abstract interface every LLM backend must implement. The orchestrator
    depends only on this class, never on a concrete provider.
    """
    @abstractmethod
    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ChatResponse:
        """Send a chat turn. `tools` is a list of OpenAI-style tool schemas:
        {"type": "function", "function": {"name", "description", "parameters"}}.
        """
        ...


class OllamaProvider(LLMProvider):
    """Talks to a local Ollama server over its HTTP `/api/chat` endpoint."""

    def __init__(self, model: str = LLM_MODEL, host: str = OLLAMA_HOST, timeout: float = 240.0):
        self.model = model
        self.host = host
        # 240s (not the httpx default) because a single turn on this
        # hardware under load can legitimately take a few minutes - see
        # docs/DECISIONS.md for the real timeout crashes that led here.
        self.timeout = timeout

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ChatResponse:
        # Every LLM call gets its own OpenTelemetry span so a saved trace
        # shows exactly how long each turn took and what went in/out of it
        # (see agent/tracing.py and traces/viewer.html).
        tracer = get_tracer()
        with tracer.start_as_current_span("llm.chat") as span:
            span.set_attribute("llm.model", self.model)
            span.set_attribute("llm.message_count", len(messages))
            # Only a truncated preview of the last message is recorded -
            # full conversation history can be large and sensitive; see
            # tracing.truncate() and docs/customer-deployment.md's data
            # handling section.
            span.set_attribute("llm.input_preview", truncate(messages[-1].get("content", "") if messages else ""))

            payload = {"model": self.model, "messages": messages, "stream": False}
            if tools:
                payload["tools"] = tools
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(f"{self.host}/api/chat", json=payload)
                    resp.raise_for_status()
                    body = resp.json()
            except Exception as e:
                # Record the failure on the span before re-raising so a
                # trace viewer shows exactly where/why an investigation
                # died, rather than just "no final span".
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise

            message = body.get("message", {})
            # Ollama's tool_calls come back as a list of
            # {"function": {"name", "arguments"}} dicts with no call id -
            # we synthesize one from list position since nothing downstream
            # needs it to be globally unique, only locally distinguishable
            # within this one turn.
            raw_tool_calls = message.get("tool_calls") or []
            tool_calls = [
                ToolCall(
                    id=str(i),
                    name=tc["function"]["name"],
                    arguments=tc["function"].get("arguments") or {},
                )
                for i, tc in enumerate(raw_tool_calls)
            ]
            # Ollama reports token counts under these specific keys (not the
            # OpenAI-style "usage.prompt_tokens" naming).
            prompt_tokens = body.get("prompt_eval_count", 0)
            completion_tokens = body.get("eval_count", 0)

            span.set_attribute("llm.prompt_tokens", prompt_tokens)
            span.set_attribute("llm.completion_tokens", completion_tokens)
            span.set_attribute("llm.tool_call_count", len(tool_calls))
            span.set_attribute("llm.output_preview", truncate(message.get("content") or json.dumps([tc.name for tc in tool_calls])))

            return ChatResponse(
                content=message.get("content") or None,
                tool_calls=tool_calls,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )


def get_default_provider() -> LLMProvider:
    """Factory used by orchestrator.py / webhook.py to get whichever
    provider is configured via LLM_PROVIDER, without those callers needing
    to know about concrete provider classes.
    """
    if LLM_PROVIDER == "ollama":
        return OllamaProvider()
    raise ValueError(f"unknown LLM_PROVIDER: {LLM_PROVIDER}")
