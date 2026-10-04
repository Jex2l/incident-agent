"""The executor package: the only part of incident-agent with write access
to the cluster.

Everything in agent/ investigates and can only *propose* a remediation
action - it never touches kubectl. This package runs as its own FastAPI
process (see app.py), on its own port (8020), specifically so the
investigation loop and the execution path are different processes with
different privileges: a bug or prompt-injection inside the LLM-driven agent
cannot reach a kubectl binary, because the agent process has no code path
that invokes one.

A write only ever happens here, and only after two independent checks pass:
1. A human approves the proposal through this service's API (app.py).
2. allowlist.execute_action() re-validates the action/target/params against
   a fixed allowlist immediately before running kubectl (allowlist.py).

store.py holds the proposal records and the append-only audit log that ties
every approve/reject/execute decision back to the incident that produced it.
"""
