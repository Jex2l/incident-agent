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

ROOT = Path(__file__).resolve().parent.parent

TOOL_SERVERS = {
    "prometheus_query": "tools.prometheus_query",
    "log_search": "tools.log_search",
    "kube_read": "tools.kube_read",
    "runbook_search": "tools.runbook_search",
}


class ToolSet:
    """Aggregates tools across all 4 MCP servers behind one call(name, args)."""

    def __init__(self):
        self._sessions: dict[str, ClientSession] = {}
        self._tool_to_server: dict[str, str] = {}
        self._tool_schemas: list[dict] = []
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> "ToolSet":
        for server_name, module in TOOL_SERVERS.items():
            params = StdioServerParameters(command=sys.executable, args=["-m", module], cwd=str(ROOT))
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._sessions[server_name] = session

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
        await self._stack.aclose()

    def schemas(self) -> list[dict]:
        return self._tool_schemas

    def tool_names(self) -> list[str]:
        return list(self._tool_to_server.keys())

    async def call(self, tool_name: str, arguments: dict) -> dict:
        if tool_name not in self._tool_to_server:
            return {"error": f"unknown tool '{tool_name}'. Available: {self.tool_names()}"}
        session = self._sessions[self._tool_to_server[tool_name]]
        try:
            result = await session.call_tool(tool_name, arguments)
        except Exception as e:
            return {"error": f"tool call failed: {e}"}

        if result.isError:
            text = "".join(c.text for c in result.content if hasattr(c, "text"))
            return {"error": text or "tool returned an error"}

        texts = [c.text for c in result.content if hasattr(c, "text")]
        if not texts:
            return {"error": "tool returned no content"}
        try:
            return json.loads(texts[0])
        except json.JSONDecodeError:
            return {"result": texts[0]}
