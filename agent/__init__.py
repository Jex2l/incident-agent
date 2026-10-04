"""Agent package: the on-call investigation engine.

This package contains everything involved in turning an incoming Alertmanager
webhook into a root-cause hypothesis:

- `llm.py`        - provider-agnostic interface to the LLM (Ollama by default)
- `mcp_client.py` - spawns and talks to the 4 MCP tool server subprocesses
- `budgets.py`     - hard caps on steps/tokens/wallclock so a run can't loop forever
- `schema.py`      - the JSON schema the final output must satisfy
- `prompts.py`     - the system prompt that defines the investigation method
- `orchestrator.py`- the actual tool-calling loop that ties all of the above together
- `webhook.py`     - the FastAPI app that receives Alertmanager webhooks and
                      kicks off an investigation
- `tracing.py`     - OpenTelemetry instrumentation shared by the modules above

Nothing in this package is allowed to perform a write action against the
cluster - that capability lives only in the separate `executor` package,
reached through an explicit human-approval step.
"""
