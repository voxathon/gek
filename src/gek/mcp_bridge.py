"""MCP as a grantable power.

A role's powers.mcp is {server_name: [tool globs]}. When an agent of that
role spawns, it connects to each granted server over stdio (as configured
in config.json mcp_servers), lists tools, keeps only those matching the
globs, and exposes them as function tools named mcp__<server>__<tool>.
Tools a role wasn't granted are never shown to it.
"""

from __future__ import annotations

import anyio
import fnmatch
import json
import os
from contextlib import AsyncExitStack
from typing import Any

from .logs import RunLog


class MCPBridge:
    def __init__(self, actor: str, log: RunLog):
        self.actor = actor
        self.log = log
        self.stack = AsyncExitStack()
        self.sessions: dict[str, Any] = {}
        self.tools: dict[str, tuple[str, str]] = {}  # exposed name -> (server, tool)
        self.schemas: list[dict[str, Any]] = []

    async def connect(self, grants: dict[str, list[str]], servers: dict[str, Any],
                      base_env: dict[str, str] | None = None) -> None:
        """base_env: the env MCP servers start from (the agent-safe allowlist, no credentials);
        each server's own `env` from config is added on top, so a server that needs a key gets
        exactly the one you configured for it."""
        if not grants:
            return
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        for server, globs in grants.items():
            spec = servers.get(server)
            if not spec:
                self.log.event("mcp", self.actor, server=server, ok=False, error="not configured")
                continue
            server_stack = AsyncExitStack()
            try:
                params = StdioServerParameters(
                    command=spec["command"], args=spec.get("args", []),
                    env={**(os.environ if base_env is None else base_env), **(spec.get("env") or {})})
                read, write = await server_stack.enter_async_context(stdio_client(params, errlog=self.log.stderr_file(f"mcp-{server}-{self.actor}")))
                session = await server_stack.enter_async_context(ClientSession(read, write))
                with anyio.fail_after(spec.get("timeout_s", 60)):
                    await session.initialize()
                listed = await session.list_tools()
                kept, tools, schemas = [], {}, []
                for t in listed.tools:
                    if any(fnmatch.fnmatch(t.name, g) for g in globs):
                        name = f"mcp__{server}__{t.name}"[:64]
                        params = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
                        tools[name] = (server, t.name)
                        schemas.append({"type": "function", "function": {
                            "name": name,
                            "description": (t.description or "")[:1000],
                            "parameters": params or {"type": "object", "properties": {}},
                        }})
                        kept.append(t.name)
                self.stack.push_async_callback(server_stack.aclose)
                self.sessions[server] = session
                self.tools.update(tools)
                self.schemas.extend(schemas)
                self.log.event("mcp", self.actor, server=server, ok=True,
                               listed=[t.name for t in listed.tools], granted=kept)
            except BaseException as e:
                self.log.event("mcp", self.actor, server=server, ok=False,
                               error=f"{type(e).__name__}: {e}")
                try:
                    await server_stack.aclose()
                except BaseException:
                    pass
                if not isinstance(e, Exception):
                    raise  # cancellation (agent killed mid-connect) must propagate

    async def call(self, name: str, args: dict[str, Any]) -> str:
        server, tool = self.tools[name]
        res = await self.sessions[server].call_tool(tool, args)
        parts = []
        for c in getattr(res, "content", None) or []:
            text = getattr(c, "text", None)
            if text is None and hasattr(c, "model_dump"):
                text = json.dumps(c.model_dump(), default=str)
            parts.append(text if text is not None else str(c))
        out = "\n".join(parts)
        err = getattr(res, "is_error", None)
        if err is None:
            err = getattr(res, "isError", False)
        return ("ERROR: " + out) if err else out

    async def close(self) -> None:
        try:
            await self.stack.aclose()
        except BaseException as e:  # anyio cancel scopes can be fussy on teardown
            self.log.event("mcp", self.actor, closing=True, error=f"{type(e).__name__}: {e}")
