"""MCP tool servers available to the incident-agent investigation loop.

Each sibling module (prometheus_query, log_search, kube_read,
runbook_search) is a standalone MCP server exposing one or more read-only
tools the LLM can call during an investigation; common.py holds the
truncation/capping helpers shared by all of them so tool output stays small
enough for a local model's context window. This package itself has no
behavior of its own - it exists so these modules can be imported as
`tools.<module>` (see the `sys.path.insert` + `from tools.common import ...`
pattern at the top of each module) both when run standalone and when
imported from tests.
"""
