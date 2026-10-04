# Demo script (2 minutes)

A shot-by-shot script for a short screen-recorded demo. Every number and screen here
is real - run the setup steps below before recording so what's on screen matches
what's described.

## Before recording

```bash
make up                               # cluster + observability + sample app
ollama pull qwen2.5:7b                # if not already pulled
make agent &                          # webhook intake, port 8010
make executor &                       # approval service, port 8020
make ui &                             # approval UI, port 8080
```

Have these ready in browser tabs, unfocused until their moment:
- Grafana (`localhost:3000`, admin/admin) on a dashboard showing the sample app
- The approval UI (`localhost:8080`)
- `evals/report.md` and `docs/failure-modes.md` open in an editor, scrolled to the
  accuracy table and the before/after section respectively

## Shot list

**0:00–0:15 — The problem, stated plainly.**
> "On-call engineers spend the first few minutes of every incident doing the same
> mechanical investigation - check the alert, look at a dashboard, grep some logs,
> check recent deploys. That's what drives MTTR up. This agent automates that first
> pass."

Screen: the architecture diagram from `README.md` (render it, don't just show raw
markdown).

**0:15–0:35 — Trigger a real incident.**
Run on screen:
```bash
python3 faults/oom_kill.py inject
```
> "This injects a real fault into a live Kubernetes cluster - no mocking. A real
> Prometheus alert will fire in about a minute."

Cut to Grafana briefly showing the pod's memory climbing / restart count ticking up.

**0:35–1:05 — The investigation, live.**
Switch to a terminal tailing the agent's logs, or the trace viewer
(`make trace-viewer`) refreshed once the alert fires.
> "Alertmanager sends a webhook, the agent pulls in metrics, logs, cluster events, and
> runbook guidance through real MCP tool calls - not a single hardcoded query - and
> produces a hypothesis with linked evidence."

Show the resulting JSON output (hypothesis, root_cause_label: oom_kill, component:
cart, confidence, evidence list) - this is `evals/results.json`'s real shape, or pull
a live one from `evals/runs/incident-*.json`.

**1:05–1:25 — The honest number, and the fix.**
> "The eval harness runs this against ground truth automatically. The first real run
> scored zero percent exact-match - and that's the point: it told us exactly why. The
> model was inventing root-cause labels instead of picking from a real taxonomy. One
> schema fix later - same model, same budget - three out of three re-verified
> scenarios went from wrong to exactly correct."

Screen: `docs/failure-modes.md`'s before/after table.

**1:25–1:45 — The approval gate.**
Switch to the approval UI, showing a pending proposal.
> "Nothing executes without a human. The agent can only propose - a separate executor
> service, with its own allowlist and audit log, is the only thing that can actually
> touch the cluster."

Click Approve on screen; show the deployment actually change (`kubectl get
deployment` before/after, or the Grafana panel reacting).

**1:45–2:00 — Close.**
> "Everything here - the fault injection, the investigation, the eval scoring, the
> approval flow - ran against a real local cluster and a real local LLM. No paid
> APIs, no cloud. The repo, the eval report, and the full decision log are linked
> below."

Screen: README.md's "What's actually been verified" section, then fade to the repo
URL.

## Notes for whoever records this

- If the agent is still investigating when you want to cut to 1:05, that's fine -
  qwen2.5:7b on consumer hardware can take 1-5 minutes per investigation depending on
  load (documented in `docs/DECISIONS.md`); either speed up that segment in editing or
  pre-record the investigation and cut to a pre-captured result.
- Don't claim a specific accuracy percentage on camera beyond what's in the committed
  `evals/report.md` at recording time - if you've re-run `make eval` since the last
  commit, update the report first so the live numbers and the spoken numbers match.
