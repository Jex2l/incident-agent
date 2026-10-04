"""Baseline pipeline: no agent loop, no iterative tool selection. Always
runs the same fixed sequence of tool calls regardless of the alert, then
makes exactly one LLM call to synthesize them into the same output schema
the real agent uses. This is the comparison point for "does the
investigation loop actually help, or would a dumb fixed pipeline do
just as well" in evals/report.md.
"""
import json
import time

from agent.llm import LLMProvider, get_default_provider
from agent.mcp_client import ToolSet
from agent.orchestrator import _try_parse_json
from agent.schema import inconclusive_result, validate_output
from agent.tracing import get_tracer

BASELINE_SYSTEM_PROMPT = """\
You are an on-call incident triage assistant. You have been given an alert \
and a fixed set of evidence gathered automatically (pod list, recent events, \
logs, deployments, and relevant runbook excerpts). You get exactly one \
response - there are no tools available to you and no follow-up turns.

Based solely on the evidence provided, respond with ONLY a single JSON object \
(no markdown fences, no prose) matching exactly this shape:

{
  "hypothesis": "<one or two sentence explanation of the likely root cause>",
  "root_cause_label": "<short snake_case label, or null if inconclusive>",
  "component": "<the affected component/deployment name, or null if inconclusive>",
  "confidence": <float 0.0-1.0>,
  "evidence": [{"tool": "<tool name>", "query": <arguments used>, "summary": "<takeaway>"}, ...],
  "proposed_action": null,
  "status": "diagnosed" or "inconclusive"
}

If the evidence doesn't clearly support a root cause, say status "inconclusive" rather \
than guessing.
"""


async def investigate_baseline(
    alert_summary: dict,
    toolset: ToolSet,
    llm: LLMProvider | None = None,
    incident_id: str | None = None,
) -> dict:
    """Run the "dumb" comparison pipeline for one alert: always the same 5
    tool calls regardless of what the alert actually is, then exactly one
    LLM call with no retries and no follow-up turns to turn that evidence
    into a hypothesis. Contrast with agent/orchestrator.py's investigate(),
    which adaptively chooses tools and can take multiple turns - this
    baseline exists purely so the eval report can show whether that
    adaptiveness is earning its larger token/time budget.

    Args:
        alert_summary: the normalized alert dict (same shape the real agent
            receives from agent/webhook.py's _alert_summary()).
        toolset: shared MCP ToolSet used to make the fixed tool calls.
        llm: provider override for testing; defaults to the configured
            production provider.
        incident_id: optional id for tracing/logging; a baseline-prefixed
            timestamp id is generated if not given.

    Returns:
        dict with keys: "result" (the schema-validated or inconclusive
        output dict), "tool_call_count" (always 5), "prompt_tokens",
        "completion_tokens", and "elapsed_s".
    """
    llm = llm or get_default_provider()
    incident_id = incident_id or f"baseline-{int(time.time())}"
    namespace = alert_summary.get("namespace") or "incident-agent"
    tracer = get_tracer()

    with tracer.start_as_current_span("investigate_baseline") as root_span:
        root_span.set_attribute("incident.id", incident_id)
        root_span.set_attribute("alert.name", str(alert_summary.get("alertname")))

        start = time.monotonic()
        evidence = []
        tool_calls_made = 0

        # The fixed set: no branching on alertname or namespace contents,
        # unlike the real agent's adaptive tool selection - this is the
        # whole point of the baseline, so don't "improve" it by making it
        # smarter about which tools to call.
        fixed_calls = [
            ("kube_list_pods", {"namespace": namespace}),
            ("kube_list_events", {"namespace": namespace}),
            ("search_pod_logs", {"namespace": namespace, "since_minutes": 15}),
            ("kube_list_deployments", {"namespace": namespace}),
            ("search_runbooks_tool", {"query": str(alert_summary.get("alertname", ""))}),
        ]
        for tool_name, args in fixed_calls:
            result = await toolset.call(tool_name, args)
            tool_calls_made += 1
            evidence.append({"tool": tool_name, "query": args, "result": result})

        evidence_text = json.dumps(evidence, default=str)
        messages = [
            {"role": "system", "content": BASELINE_SYSTEM_PROMPT},
            {"role": "user", "content": f"Alert:\n{json.dumps(alert_summary, indent=2)}\n\nEvidence:\n{evidence_text}"},
        ]

        response = await llm.chat(messages)  # no tools - exactly one call, no loop
        parsed = _try_parse_json(response.content or "")

        # Unlike the real agent, there is no retry-on-bad-output here: a
        # malformed or schema-invalid response is recorded as inconclusive
        # immediately. The reason string is embedded in the hypothesis text
        # so scorer.classify_failure_mode() can later tell this failure mode
        # apart from a genuinely wrong diagnosis.
        if parsed is None:
            result = inconclusive_result("baseline: model did not produce valid JSON (no retry in baseline)", [
                {"tool": e["tool"], "query": e["query"], "summary": "fixed evidence gathered"} for e in evidence
            ])
        else:
            valid, errors = validate_output(parsed)
            result = parsed if valid else inconclusive_result(
                f"baseline: model output failed schema validation (no retry in baseline): {errors}",
                [{"tool": e["tool"], "query": e["query"], "summary": "fixed evidence gathered"} for e in evidence],
            )

        elapsed = time.monotonic() - start
        root_span.set_attribute("result.status", result.get("status", "unknown"))
        root_span.set_attribute("result.root_cause_label", str(result.get("root_cause_label")))

        return {
            "result": result,
            "tool_call_count": tool_calls_made,
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "elapsed_s": elapsed,
        }
