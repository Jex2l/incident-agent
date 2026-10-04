"""The investigation loop: webhook intake hands this an alert summary, it
calls tools (via MCP) under hard budgets, and returns schema-validated
output. Every run's full message/tool-call history is saved to traces/.
"""
import json
import re
import time
from pathlib import Path

import jsonschema

from agent.budgets import Budget, BudgetTracker
from agent.llm import LLMProvider, get_default_provider
from agent.mcp_client import ToolSet
from agent.prompts import SYSTEM_PROMPT
from agent.schema import inconclusive_result, validate_output
from agent.tracing import get_tracer

ROOT = Path(__file__).resolve().parent.parent
TRACES_DIR = ROOT / "traces"
TRACES_DIR.mkdir(exist_ok=True)

MAX_FORMAT_RETRIES = 2
MAX_LLM_ERROR_RETRIES = 2
TOOL_RESULT_PREVIEW_CHARS = 800

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _try_parse_json(text: str) -> dict | None:
    text = text.strip()
    m = _JSON_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _tool_result_summary(result: dict) -> str:
    if "error" in result:
        return f"error: {result['error']}"
    for key in ("series", "patterns", "pods", "events", "deployments", "results", "lines"):
        if key in result and isinstance(result[key], list):
            return f"returned {len(result[key])} {key}"
    return "tool call succeeded"


def _validate_tool_args(tool_name: str, arguments, toolset: ToolSet) -> str | None:
    """Pre-validate tool-call arguments against the tool's own JSON schema
    before invoking it, so a malformed call produces a precise error
    message instead of a raw Python exception. Returns an error string, or
    None if arguments are valid."""
    if not isinstance(arguments, dict):
        return "tool arguments must be a JSON object"
    schema = next(
        (t["function"]["parameters"] for t in toolset.schemas() if t["function"]["name"] == tool_name),
        None,
    )
    if schema is None:
        return None
    try:
        jsonschema.validate(arguments, schema)
    except jsonschema.ValidationError as e:
        return f"invalid arguments: {e.message}"
    return None


async def investigate(
    alert_summary: dict,
    toolset: ToolSet,
    llm: LLMProvider | None = None,
    budget: Budget | None = None,
    incident_id: str | None = None,
) -> dict:
    llm = llm or get_default_provider()
    budget = budget or Budget()
    incident_id = incident_id or f"incident-{int(time.time())}"
    tracker = BudgetTracker(budget)
    fallback_evidence: list[dict] = []
    trace = {"incident_id": incident_id, "alert": alert_summary, "turns": [], "tool_calls": []}

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"New alert to investigate:\n{json.dumps(alert_summary, indent=2)}"},
    ]

    tracer = get_tracer()
    with tracer.start_as_current_span("investigate") as root_span:
        root_span.set_attribute("incident.id", incident_id)
        root_span.set_attribute("alert.name", str(alert_summary.get("alertname")))
        root_span.set_attribute("alert.namespace", str(alert_summary.get("namespace")))

        def finish(result: dict) -> dict:
            trace["result"] = result
            trace["budget"] = tracker.summary()
            _save_trace(incident_id, trace)
            root_span.set_attribute("result.status", result.get("status", "unknown"))
            root_span.set_attribute("result.root_cause_label", str(result.get("root_cause_label")))
            root_span.set_attribute("result.confidence", result.get("confidence", 0.0))
            root_span.set_attribute("budget.steps_used", tracker.steps_used)
            root_span.set_attribute("budget.tokens_used", tracker.tokens_used)
            return result

        format_retries = 0
        llm_error_retries = 0
        while True:
            reason = tracker.exceeded()
            if reason:
                return finish(inconclusive_result(reason, fallback_evidence))

            try:
                response = await llm.chat(messages, tools=toolset.schemas())
            except Exception as e:
                llm_error_retries += 1
                if llm_error_retries > MAX_LLM_ERROR_RETRIES:
                    return finish(inconclusive_result(f"LLM provider error after retries: {type(e).__name__}: {e}", fallback_evidence))
                continue
            tracker.record_tokens(response.prompt_tokens, response.completion_tokens)
            trace["turns"].append({
                "content": response.content,
                "tool_calls": [{"name": tc.name, "arguments": tc.arguments} for tc in response.tool_calls],
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
            })

            if response.tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": [{"function": {"name": tc.name, "arguments": tc.arguments}} for tc in response.tool_calls],
                })
                for tc in response.tool_calls:
                    error = None
                    if tc.name not in toolset.tool_names():
                        error = f"unknown tool '{tc.name}'. Available tools: {toolset.tool_names()}"
                    else:
                        error = _validate_tool_args(tc.name, tc.arguments, toolset)

                    tool_result = {"error": error} if error else await toolset.call(tc.name, tc.arguments)
                    trace["tool_calls"].append({
                        "name": tc.name,
                        "arguments": tc.arguments,
                        "result_preview": json.dumps(tool_result)[:TOOL_RESULT_PREVIEW_CHARS],
                    })
                    fallback_evidence.append({"tool": tc.name, "query": tc.arguments, "summary": _tool_result_summary(tool_result)})
                    messages.append({"role": "tool", "content": json.dumps(tool_result)})
                tracker.record_step()
                continue

            # No tool calls this turn -> treat content as a final-answer attempt.
            parsed = _try_parse_json(response.content or "")
            if parsed is None:
                format_retries += 1
                if format_retries > MAX_FORMAT_RETRIES:
                    return finish(inconclusive_result("model could not produce valid JSON output after retries", fallback_evidence))
                messages.append({"role": "user", "content": "Your last response was not valid JSON. Respond with ONLY the JSON object described in the system prompt - no markdown fences, no other text."})
                continue

            valid, errors = validate_output(parsed)
            if not valid:
                format_retries += 1
                if format_retries > MAX_FORMAT_RETRIES:
                    return finish(inconclusive_result(f"model's output failed schema validation: {errors}", fallback_evidence))
                messages.append({"role": "user", "content": f"Your JSON output failed schema validation: {errors}. Fix it and respond with ONLY the corrected JSON object."})
                continue

            return finish(parsed)


def _save_trace(incident_id: str, trace: dict):
    path = TRACES_DIR / f"{incident_id}.json"
    path.write_text(json.dumps(trace, indent=2, default=str))
