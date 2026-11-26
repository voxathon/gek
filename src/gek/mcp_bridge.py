"""MCP as a grantable power.

A role's powers.mcp is {server_name: [tool globs]}. When an agent of that
role spawns, it connects to each granted server (as configured in
config.json mcp_servers), lists tools, keeps only those matching the globs,
and exposes them as function tools named mcp__<server>__<tool>.

A server is either local (stdio: we spawn `command`) or remote (a url over
streamable-http or sse, with optional headers whose "${ENV:NAME}" values are
expanded at connect time and never logged). Tools a role wasn't granted are
never shown to it.
"""

from __future__ import annotations

import anyio
import json
import os
from contextlib import AsyncExitStack
from typing import Any
from urllib.parse import urlparse

from .config import (mcp_endpoint, mcp_header_env_names, mcp_kind, mcp_resolve_headers,
                     mcp_transport)
from .logs import RunLog
from .tools import Tool, granted


def _host(url: str) -> str:
    """Just the host[:port] of a url, for logs: no path, no query, no userinfo."""
    try:
        return urlparse(str(url)).netloc.rsplit("@", 1)[-1] or "?"
    except (ValueError, TypeError):
        return "?"


class MCPBridge:
    def __init__(self, actor: str, log: RunLog):
        self.actor = actor
        self.log = log
        self.stack = AsyncExitStack()
        self.sessions: dict[str, Any] = {}
        self.tools: dict[str, tuple[str, str]] = {}  # exposed name -> (server, tool)
        self.listed: list[Tool] = []

    async def _open(self, server: str, server_stack: AsyncExitStack, spec: dict[str, Any],
                    base_env: dict[str, str] | None):
        """Enter a transport (stdio or remote) on server_stack and return (read, write).

        server_stack is pushed onto self.stack only once the session is ready, so a server
        that fails to connect closes its own transport and leaves no session behind."""
        if mcp_kind(spec) == "remote":
            url = str(spec["url"])
            transport = mcp_transport(spec)
            # raises ValueError (naming server + variable, never a value) if a var is missing
            headers = mcp_resolve_headers(spec, base_env, name=server)
            timeout = float(spec.get("timeout_s") or 60)
            if transport == "sse":
                from mcp.client.sse import sse_client  # noqa: PLC0415

                streams = await server_stack.enter_async_context(
                    sse_client(url, headers=headers, timeout=timeout))
            else:
                import httpx2  # noqa: PLC0415 -- the SDK's own http lib, same version
                from mcp.client.streamable_http import (create_mcp_http_client,  # noqa: PLC0415
                                                        streamable_http_client)

                # the httpx client must stay open for the whole session: nest it on the same
                # stack scope as the transport (both closed by server_stack, in one task)
                http_client = await server_stack.enter_async_context(
                    create_mcp_http_client(headers=headers, timeout=httpx2.Timeout(timeout)))
                streams = await server_stack.enter_async_context(
                    streamable_http_client(url, http_client=http_client))
            return streams[0], streams[1]

        from mcp import StdioServerParameters  # noqa: PLC0415
        from mcp.client.stdio import stdio_client  # noqa: PLC0415

        params = StdioServerParameters(
            command=spec["command"], args=spec.get("args", []),
            env={**(os.environ if base_env is None else base_env), **(spec.get("env") or {})})
        read, write = await server_stack.enter_async_context(
            stdio_client(params, errlog=self.log.stderr_file(f"mcp-{server}-{self.actor}")))
        return read, write

    async def connect(self, grants: dict[str, list[str]], servers: dict[str, Any],
                      base_env: dict[str, str] | None = None) -> None:
        """base_env: the env MCP servers start from (the agent-safe allowlist, no credentials);
        each server's own `env` from config is added on top, so a server that needs a key gets
        exactly the one you configured for it. Remote header placeholders (${ENV:NAME}) are
        resolved from it too. A server that can't be reached is logged and skipped: connect()
        does not raise for a bad entry, so one broken server leaves the rest usable."""
        if not grants:
            return
        from mcp import ClientSession

        for server, globs in grants.items():
            spec = servers.get(server)
            if not spec:
                self.log.event("mcp", self.actor, server=server, ok=False, error="not configured")
                continue
            kind = mcp_kind(spec)
            server_stack = AsyncExitStack()
            try:
                read, write = await self._open(server, server_stack, spec, base_env)
                session = await server_stack.enter_async_context(ClientSession(read, write))
                with anyio.fail_after(spec.get("timeout_s", 60)):
                    await session.initialize()
                listed = await session.list_tools()
                kept, tools, made = [], {}, []
                for t in listed.tools:
                    if granted(globs, t.name):
                        name = f"mcp__{server}__{t.name}"[:64]
                        params = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
                        tools[name] = (server, t.name)
                        made.append(Tool(name, (t.description or "")[:1000],
                                         params or {"type": "object", "properties": {}},
                                         self._handler(name), source=server))
                        kept.append(t.name)
                self.stack.push_async_callback(server_stack.aclose)
                self.sessions[server] = session
                self.tools.update(tools)
                self.listed.extend(made)
                event: dict[str, Any] = {"server": server, "ok": True, "kind": kind}
                if kind == "remote":
                    event["transport"] = mcp_transport(spec)
                    event["host"] = _host(mcp_endpoint(spec))
                    event["auth_names"] = mcp_header_env_names(spec)
                self.log.event("mcp", self.actor, listed=[t.name for t in listed.tools],
                               granted=kept, **event)
            except BaseException as e:
                event = {"server": server, "ok": False, "kind": kind,
                         "error": f"{type(e).__name__}: {self._safe(e, spec)}"}
                if kind == "remote":
                    event["transport"] = mcp_transport(spec)
                    event["host"] = _host(mcp_endpoint(spec))
                self.log.event("mcp", self.actor, **event)
                try:
                    await server_stack.aclose()
                except BaseException:
                    pass
                if not isinstance(e, Exception):
                    raise  # cancellation (agent killed mid-connect) must propagate

    def _safe(self, e: BaseException, spec: dict[str, Any]) -> str:
        """The exception text with any configured header/env value scrubbed out of it: an error
        line must never echo a secret (a library quoting a request is the risk)."""
        text = str(e)
        values = [str(v) for v in (spec.get("headers") or {}).values()]
        values += [str(v) for v in (spec.get("env") or {}).values()]
        for value in values:
            if len(value) >= 4:
                text = text.replace(value, "***")
        return text

    def _handler(self, name: str):
        async def h(**args: Any) -> str:
            return await self.call(name, args)
        return h

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
        finally:
            # nothing survives a close: the next generation starts from a clean bridge
            self.sessions.clear()
            self.tools.clear()
            self.listed.clear()
