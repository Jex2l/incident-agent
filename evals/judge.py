"""Optional LLM-judge for explanation quality, using a DIFFERENT local
model than the one under test (so the model isn't grading its own work).
Disabled by default (EVAL_LLM_JUDGE=false) - see evals/report.md for which
runs, if any, actually used it.
"""
import json
import os

from agent.llm import OllamaProvider
from agent.orchestrator import _try_parse_json

JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "qwen2.5:3b")

# Deliberately scores explanation *quality* (grounded/clarity/actionability),
# not whether the label is correct - exact-match correctness is scorer.py's
# job and is already an objective comparison against scenarios.yaml. This
# prompt exists to catch a model that gets the right label for the wrong
# (or no) reason.
JUDGE_PROMPT = """\
You are grading an on-call incident triage agent's explanation, not its final \
label accuracy (that's scored separately by exact match against ground truth). \
Rate the hypothesis text on a 1-5 scale for each of:

- grounded: is the explanation actually supported by the evidence listed, or does \
  it assert things the evidence doesn't show?
- clarity: would an on-call engineer understand the claimed root cause from this \
  text alone?
- actionability: does it point toward what to check or do next, even if the \
  proposed_action field is null?

Respond with ONLY a JSON object: {"grounded": <1-5>, "clarity": <1-5>, \
"actionability": <1-5>, "notes": "<one sentence justification>"}
"""


async def judge_result(alert_summary: dict, result: dict, model_under_test: str) -> dict | None:
    """Ask a second, smaller local model to grade the agent's hypothesis
    text for explanation quality. Optional and off by default
    (EVAL_LLM_JUDGE=false in runner.py) - fully implemented but not
    exercised in the eval runs actually documented in evals/report.md, for
    time/cost reasons (an extra LLM call per scored run on top of an
    already ~5-hour matrix).

    Args:
        alert_summary: the normalized alert dict passed to the pipeline
            under test, included so the judge has the same context.
        result: the pipeline's schema-shaped output dict (hypothesis,
            evidence, status, etc.) to be graded.
        model_under_test: name of the model that produced `result`, used
            only to decide whether judging should be skipped below.

    Returns:
        None (not an error) if judging is skipped - e.g. the judge
        model is the same as the model under test, which would defeat the
        point of an independent check. Otherwise a dict with
        "grounded"/"clarity"/"actionability" (1-5) and "notes", or an
        {"error": ...} dict if the judge model's own output wasn't valid
        JSON.
    """
    if JUDGE_MODEL == model_under_test:
        return None

    judge = OllamaProvider(model=JUDGE_MODEL)
    prompt = (
        f"Alert: {json.dumps(alert_summary)}\n\n"
        f"Agent's hypothesis: {result.get('hypothesis')}\n"
        f"Evidence cited: {json.dumps(result.get('evidence', []))}\n"
        f"Status: {result.get('status')}"
    )
    response = await judge.chat([
        {"role": "system", "content": JUDGE_PROMPT},
        {"role": "user", "content": prompt},
    ])
    parsed = _try_parse_json(response.content or "")
    if parsed is None:
        return {"error": "judge did not return valid JSON", "judge_model": JUDGE_MODEL}
    parsed["judge_model"] = JUDGE_MODEL
    return parsed
