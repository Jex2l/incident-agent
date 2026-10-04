#!/usr/bin/env python3
"""Eval harness: for each scenario, inject the real fault, wait for the
real alert, run the full agent (and optionally the baseline pipeline) on
it, score against ground truth, reset. Writes evals/results.json
(incrementally, after every run) and evals/report.md from those real
results - never fabricated or hand-edited.
"""
import asyncio
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "faults"))

from agent.budgets import Budget  # noqa: E402
from agent.mcp_client import ToolSet  # noqa: E402
from agent.orchestrator import investigate  # noqa: E402
from agent.webhook import _alert_summary  # noqa: E402
from evals.baseline import investigate_baseline  # noqa: E402
from evals.judge import judge_result  # noqa: E402
from evals.scorer import aggregate, score_run  # noqa: E402

import lib as fault_lib  # noqa: E402  (faults/lib.py, via the sys.path insert above)

SCENARIOS_PATH = ROOT / "evals" / "scenarios.yaml"
RESULTS_PATH = ROOT / "evals" / "results.json"
REPORT_PATH = ROOT / "evals" / "report.md"

RUNS_PER_SCENARIO = int(os.environ.get("EVAL_RUNS_PER_SCENARIO", "5"))
RUN_BASELINE = os.environ.get("EVAL_RUN_BASELINE", "true").lower() == "true"
RUN_JUDGE = os.environ.get("EVAL_LLM_JUDGE", "false").lower() == "true"
MODEL_UNDER_TEST = os.environ.get("LLM_MODEL", "qwen2.5:7b")
ALERT_WAIT_TIMEOUT_S = int(os.environ.get("EVAL_ALERT_WAIT_TIMEOUT_S", "300"))

# Reference pricing only - illustrative of what this token volume would cost
# against a small hosted-model tier. The model under test runs locally via
# Ollama; actual spend for every run in this report is $0.
REFERENCE_PRICE_PER_1K_PROMPT_TOKENS = 0.0005
REFERENCE_PRICE_PER_1K_COMPLETION_TOKENS = 0.0015


def load_fault_module(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "faults" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _reset_with_retry(fault_mod, retries: int = 4, delay_s: float = 20.0):
    """kubectl calls transiently fail under the CPU contention this project
    is known to hit when the cluster and local LLM inference compete for
    cores (documented in docs/DECISIONS.md, Phase 4/6/7) - a bare
    fault_mod.reset() crashing the whole harness on one flaky TLS handshake
    would throw away every result collected so far. Retry with backoff; if
    all retries fail, log loudly and move on rather than losing the run."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            fault_mod.reset()
            return True
        except Exception as e:
            last_err = e
            print(f"  reset attempt {attempt}/{retries} failed ({type(e).__name__}: {e}); retrying in {delay_s}s...")
            time.sleep(delay_s)
    print(f"  WARNING: reset failed after {retries} attempts ({last_err}) - fault may still be active, "
          f"continuing to the next run anyway rather than losing all accumulated results.")
    return False


async def run_one(scenario: dict, toolset: ToolSet, run_index: int) -> dict:
    fault_mod = load_fault_module(scenario["fault_module"])
    since = fault_lib.now_iso()
    print(f"  injecting {scenario['fault_module']}...")
    fault_mod.inject()

    alert = fault_lib.wait_for_alert(scenario["expected_alerts"], since, timeout_s=ALERT_WAIT_TIMEOUT_S)
    record = {"scenario": scenario["name"], "run_index": run_index, "alert_fired": alert is not None}

    if alert is None:
        print("  alert never fired within timeout - resetting and recording as a miss")
        _reset_with_retry(fault_mod)
        record["agent"] = None
        record["baseline"] = None
        record["error"] = "alert never fired within timeout"
        return record

    alert_summary = _alert_summary(alert)
    print(f"  alert fired: {alert_summary['alertname']}, running full agent...")

    try:
        incident_id = f"eval-agent-{scenario['name']}-{run_index}-{int(time.time())}"
        start = time.monotonic()
        agent_result = await investigate(alert_summary, toolset, budget=Budget(), incident_id=incident_id)
        agent_elapsed = time.monotonic() - start

        trace = json.loads((ROOT / "traces" / f"{incident_id}.json").read_text())
        agent_score = score_run(
            scenario, agent_result,
            tool_call_count=len(trace["tool_calls"]),
            prompt_tokens=sum(t["prompt_tokens"] for t in trace["turns"]),
            completion_tokens=sum(t["completion_tokens"] for t in trace["turns"]),
            elapsed_s=agent_elapsed,
        )
        record["agent"] = agent_score
        print(f"  agent: status={agent_result.get('status')} root_cause={agent_result.get('root_cause_label')} "
              f"component={agent_result.get('component')} exact_match={agent_score['exact_match']} ({agent_elapsed:.1f}s)")

        if RUN_JUDGE:
            print("  running LLM judge...")
            record["agent_judge"] = await judge_result(alert_summary, agent_result, MODEL_UNDER_TEST)

        if RUN_BASELINE:
            print("  running baseline pipeline...")
            baseline_incident_id = f"eval-baseline-{scenario['name']}-{run_index}-{int(time.time())}"
            baseline_out = await investigate_baseline(alert_summary, toolset, incident_id=baseline_incident_id)
            baseline_score = score_run(
                scenario, baseline_out["result"],
                tool_call_count=baseline_out["tool_call_count"],
                prompt_tokens=baseline_out["prompt_tokens"],
                completion_tokens=baseline_out["completion_tokens"],
                elapsed_s=baseline_out["elapsed_s"],
            )
            record["baseline"] = baseline_score
            print(f"  baseline: status={baseline_out['result'].get('status')} "
                  f"exact_match={baseline_score['exact_match']} ({baseline_out['elapsed_s']:.1f}s)")
    except Exception as e:
        # A transient infra failure (cluster API timeout, etc.) mid-investigation
        # must not crash the whole harness and lose every prior result - record
        # what we have and still attempt to reset the fault below.
        print(f"  ERROR during investigation: {type(e).__name__}: {e}")
        record.setdefault("agent", None)
        record.setdefault("baseline", None)
        record["error"] = f"{type(e).__name__}: {e}"

    print("  resetting fault...")
    record["reset_ok"] = _reset_with_retry(fault_mod)
    return record


async def main_async() -> dict:
    scenarios = yaml.safe_load(SCENARIOS_PATH.read_text())["scenarios"]
    results = {
        "model_under_test": MODEL_UNDER_TEST,
        "runs_per_scenario": RUNS_PER_SCENARIO,
        "run_baseline": RUN_BASELINE,
        "run_judge": RUN_JUDGE,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "runs": [],
    }

    async with ToolSet() as toolset:
        for scenario in scenarios:
            for i in range(RUNS_PER_SCENARIO):
                print(f"\n=== {scenario['name']} run {i + 1}/{RUNS_PER_SCENARIO} ===")
                try:
                    record = await run_one(scenario, toolset, i)
                except Exception as e:
                    # Belt and suspenders beyond run_one()'s own try/except:
                    # nothing should be able to take down the whole harness
                    # and lose every previously-collected result.
                    print(f"  FATAL in run_one, recording as a miss and continuing: {type(e).__name__}: {e}")
                    record = {"scenario": scenario["name"], "run_index": i, "alert_fired": False,
                              "agent": None, "baseline": None, "error": f"harness error: {type(e).__name__}: {e}"}
                results["runs"].append(record)
                RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str))

    results["completed_at"] = datetime.now(timezone.utc).isoformat()
    RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str))
    return results


def build_report(results: dict) -> str:
    agent_runs = [r["agent"] for r in results["runs"] if r.get("agent")]
    baseline_runs = [r["baseline"] for r in results["runs"] if r.get("baseline")]
    missed = [r for r in results["runs"] if not r.get("alert_fired")]
    agent_agg = aggregate(agent_runs)
    baseline_agg = aggregate(baseline_runs) if baseline_runs else None
    scenario_count = len({r["scenario"] for r in results["runs"]})

    lines = [
        "# incident-agent eval report",
        "",
        f"Generated from **{len(results['runs'])} real runs** "
        f"({results['runs_per_scenario']} per scenario x {scenario_count} scenarios). "
        f"Model under test: `{results['model_under_test']}`.",
        "",
        f"Started: {results['started_at']}  ",
        f"Completed: {results.get('completed_at', 'IN PROGRESS')}",
        "",
    ]

    if missed:
        lines += [f"**{len(missed)} run(s) never saw their alert fire within {ALERT_WAIT_TIMEOUT_S}s and are excluded from scoring below** "
                   f"(counted as a harness-level miss, not a model failure): "
                   + ", ".join(f"{r['scenario']}#{r['run_index']}" for r in missed), ""]

    lines += [
        "## Overall accuracy (exact match: root_cause_label AND component)",
        "",
        "| Pipeline | Accuracy | Inconclusive rate | Avg tool calls | Avg tokens | Avg time to diagnosis |",
        "|---|---|---|---|---|---|",
        f"| Full agent | {agent_agg.get('accuracy', 0) * 100:.0f}% | {agent_agg.get('inconclusive_rate', 0) * 100:.0f}% | "
        f"{agent_agg.get('avg_tool_call_count', 0)} | {agent_agg.get('avg_total_tokens', 0):.0f} | {agent_agg.get('avg_elapsed_s', 0):.1f}s |",
    ]
    if baseline_agg:
        lines.append(
            f"| Baseline (fixed pipeline, 1 LLM call) | {baseline_agg.get('accuracy', 0) * 100:.0f}% | "
            f"{baseline_agg.get('inconclusive_rate', 0) * 100:.0f}% | {baseline_agg.get('avg_tool_call_count', 0)} | "
            f"{baseline_agg.get('avg_total_tokens', 0):.0f} | {baseline_agg.get('avg_elapsed_s', 0):.1f}s |"
        )
    lines.append("")

    lines += ["## Accuracy by scenario (full agent)", "", "| Scenario | Runs | Accuracy |", "|---|---|---|"]
    for name, s in agent_agg.get("accuracy_by_scenario", {}).items():
        lines.append(f"| {name} | {s['run_count']} | {s['accuracy'] * 100:.0f}% |")
    lines.append("")

    lines += ["## Failure mode tally (full agent)", "", "| Failure mode | Count |", "|---|---|"]
    for mode, count in sorted(agent_agg.get("failure_mode_tally", {}).items(), key=lambda kv: -kv[1]):
        lines.append(f"| {mode} | {count} |")
    lines.append("")

    if agent_runs:
        total_prompt = sum(r["prompt_tokens"] for r in agent_runs)
        total_completion = sum(r["completion_tokens"] for r in agent_runs)
        total_tokens = total_prompt + total_completion
        ref_cost = total_prompt / 1000 * REFERENCE_PRICE_PER_1K_PROMPT_TOKENS + total_completion / 1000 * REFERENCE_PRICE_PER_1K_COMPLETION_TOKENS
        lines += [
            "## Token usage and reference cost",
            "",
            "**Actual cost: $0** - the model under test runs locally via Ollama; no API calls, no spend.",
            "",
            f"Total tokens across all {len(agent_runs)} agent runs: {total_tokens:,} "
            f"({total_prompt:,} prompt + {total_completion:,} completion). "
            f"Average per incident: {agent_agg['avg_total_tokens']:.0f} tokens.",
            "",
            f"**Reference figure only, not actual spend**: at illustrative pricing of "
            f"${REFERENCE_PRICE_PER_1K_PROMPT_TOKENS}/1K prompt tokens and ${REFERENCE_PRICE_PER_1K_COMPLETION_TOKENS}/1K "
            f"completion tokens (roughly a small hosted-model tier), this token volume would cost "
            f"**${ref_cost:.4f}** across all runs, or **${ref_cost / len(agent_runs):.5f} per incident**.",
            "",
        ]

    if RUN_JUDGE:
        judged = [r.get("agent_judge") for r in results["runs"] if r.get("agent_judge") and "error" not in r["agent_judge"]]
        if judged:
            avg_grounded = sum(j["grounded"] for j in judged) / len(judged)
            avg_clarity = sum(j["clarity"] for j in judged) / len(judged)
            avg_action = sum(j["actionability"] for j in judged) / len(judged)
            lines += [
                "## LLM-judge explanation quality (1-5, judged by a different local model)",
                "",
                f"Judge model: `{judged[0]['judge_model']}` (distinct from `{results['model_under_test']}` under test).",
                "",
                f"- grounded: {avg_grounded:.2f}/5",
                f"- clarity: {avg_clarity:.2f}/5",
                f"- actionability: {avg_action:.2f}/5",
                "",
            ]
    else:
        lines += ["## LLM-judge explanation quality", "", "Not run for this report (`EVAL_LLM_JUDGE=false`) - see docs/DECISIONS.md for why.", ""]

    return "\n".join(lines)


def main():
    results = asyncio.run(main_async())
    report = build_report(results)
    REPORT_PATH.write_text(report)
    print(f"\nWrote {RESULTS_PATH} and {REPORT_PATH}")


if __name__ == "__main__":
    main()
