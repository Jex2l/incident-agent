"""LLM provider interface. Everything else in agent/ talks to this, not to
Ollama directly, so a different free-tier provider (Gemini, Groq) can be
added later without touching the orchestrator.
"""
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx
from opentelemetry.trace import Status, StatusCode

from agent.tracing import get_tracer, truncate

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen2.5:7b")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ChatResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMProvider(ABC):
    @abstractmethod
    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ChatResponse:
        """Send a chat turn. `tools` is a list of OpenAI-style tool schemas:
        {"type": "function", "function": {"name", "description", "parameters"}}.
        """
        ...


class OllamaProvider(LLMProvider):
    def __init__(self, model: str = LLM_MODEL, host: str = OLLAMA_HOST, timeout: float = 240.0):
        self.model = model
        self.host = host
        self.timeout = timeout

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ChatResponse:
        tracer = get_tracer()
        with tracer.start_as_current_span("llm.chat") as span:
            span.set_attribute("llm.model", self.model)
            span.set_attribute("llm.message_count", len(messages))
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
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise

            message = body.get("message", {})
            raw_tool_calls = message.get("tool_calls") or []
            tool_calls = [
                ToolCall(
                    id=str(i),
                    name=tc["function"]["name"],
                    arguments=tc["function"].get("arguments") or {},
                )
                for i, tc in enumerate(raw_tool_calls)
            ]
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
    if LLM_PROVIDER == "ollama":
        return OllamaProvider()
    raise ValueError(f"unknown LLM_PROVIDER: {LLM_PROVIDER}")
