"""Manages MCP client sessions to the 4 tool servers (tools/*.py), each run
as a subprocess over stdio. This is the agent's only way to call tools -
it never imports tools/*.py directly - so the agent exercises the same MCP
protocol path a real external MCP client would use.
"""
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from opentelemetry.trace import Status, StatusCode

from agent.tracing import get_tracer, truncate

ROOT = Path(__file__).resolve().parent.parent

# Maps a logical server name to the Python module that implements it
# (tools/<name>.py, run as `python -m tools.<name>` in its own subprocess).
# Adding a 5th tool server means adding one entry here - ToolSet below
# handles spawning, schema discovery, and dispatch generically.
TOOL_SERVERS = {
    "prometheus_query": "tools.prometheus_query",
    "log_search": "tools.log_search",
    "kube_read": "tools.kube_read",
    "runbook_search": "tools.runbook_search",
}


class ToolSet:
    """Aggregates tools across all 4 MCP servers behind one call(name, args).

    Used as an async context manager (`async with ToolSet() as tools:`) so
    all 4 subprocesses are spawned on entry and cleanly torn down on exit via
    a single AsyncExitStack, regardless of how the `with` block exits.
    """

    def __init__(self):
        # One live MCP ClientSession per server, keyed by server name.
        self._sessions: dict[str, ClientSession] = {}
        # Reverse index: which server owns a given tool name, so call()
        # can route to the right subprocess without the caller needing to
        # know the tool/server mapping.
        self._tool_to_server: dict[str, str] = {}
        # OpenAI-style tool schemas collected from every server, handed
        # straight to agent.llm.LLMProvider.chat()'s `tools` parameter so
        # the model knows what it can call and with what arguments.
        self._tool_schemas: list[dict] = []
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> "ToolSet":
        for server_name, module in TOOL_SERVERS.items():
            # Launch `python -m tools.<module>` as a subprocess and speak
            # MCP to it over its stdin/stdout (stdio transport) - this is
            # the same protocol path any external MCP client would use,
            # which is the whole point: the agent never imports tools/*.py
            # directly, it always goes through the real MCP wire protocol.
            params = StdioServerParameters(command=sys.executable, args=["-m", module], cwd=str(ROOT))
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._sessions[server_name] = session

            # Ask the server what tools it exposes (FastMCP auto-generates
            # this from each @mcp.tool()-decorated function's signature and
            # docstring) and fold them into the aggregate schema/index.
            tools_result = await session.list_tools()
            for tool in tools_result.tools:
                self._tool_to_server[tool.name] = server_name
                self._tool_schemas.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.inputSchema,
                    },
                })
        return self

    async def __aexit__(self, *exc_info):
        # Closes all 4 stdio transports and subprocess handles in reverse
        # order of acquisition, regardless of whether the block raised.
        await self._stack.aclose()

    def schemas(self) -> list[dict]:
        """OpenAI-style tool schemas for every tool across all servers -
        passed to the LLM so it knows what's callable."""
        return self._tool_schemas

    def tool_names(self) -> list[str]:
        return list(self._tool_to_server.keys())

    async def call(self, tool_name: str, arguments: dict) -> dict:
        """Public entry point: routes a tool call to the right MCP server
        and wraps it in an OpenTelemetry span recording the tool name,
        (truncated) arguments, and (truncated) result - this is what shows
        up as a "tool.call" node in the trace viewer.
        """
        tracer = get_tracer()
        with tracer.start_as_current_span("tool.call") as span:
            span.set_attribute("tool.name", tool_name)
            span.set_attribute("tool.arguments", truncate(arguments))

            output = await self._call_uninstrumented(tool_name, arguments)

            span.set_attribute("tool.result_preview", truncate(output))
            # A tool error is surfaced as {"error": ...} data (not an
            # exception) so the orchestrator loop can feed it back to the
            # model as a normal tool result and let it try something else -
            # but it's still flagged on the span so a trace reviewer can see
            # at a glance which tool calls failed.
            if isinstance(output, dict) and "error" in output:
                span.set_status(Status(StatusCode.ERROR, str(output["error"])))
            return output

    async def _call_uninstrumented(self, tool_name: str, arguments: dict) -> dict:
        """The actual MCP call, without tracing - split out from call() so
        the span always wraps exactly the work being measured."""
        if tool_name not in self._tool_to_server:
            return {"error": f"unknown tool '{tool_name}'. Available: {self.tool_names()}"}
        session = self._sessions[self._tool_to_server[tool_name]]
        try:
            result = await session.call_tool(tool_name, arguments)
        except Exception as e:
            # A transport-level failure (subprocess died, malformed
            # response, etc.) becomes a tool error, not a crash of the
            # whole investigation.
            return {"error": f"tool call failed: {e}"}

        if result.isError:
            # MCP tool-level errors come back as content blocks with
            # isError=True rather than raising - concatenate any text
            # blocks to get a readable message.
            text = "".join(c.text for c in result.content if hasattr(c, "text"))
            return {"error": text or "tool returned an error"}

        texts = [c.text for c in result.content if hasattr(c, "text")]
        if not texts:
            return {"error": "tool returned no content"}
        try:
            # Our tool servers always return a single JSON-encoded text
            # block; parse it back into a dict for the orchestrator.
            return json.loads(texts[0])
        except json.JSONDecodeError:
            # Defensive fallback if a tool ever returns plain text instead
            # of JSON - don't lose the content, just pass it through raw.
            return {"result": texts[0]}
