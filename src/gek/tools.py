"""The one tool registry. Every tool an actor sees is a Tool here, whatever provides it:

  gek     the core tools (read_log, commit, merge, run, kill...), gated by role powers
  <name>  an MCP server from mcp_servers, or a plugin that provides tools; gated by the
          role's powers.mcp / powers.tools globs, exposed as mcp__<server>__<tool> or
          <plugin>__<tool>

One path for listing, granting, calling and logging, so a plugin's tool is granted exactly
like an MCP tool, and `gek mcp-serve` can hand the same filtered set to an outside runtime."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
import fnmatch
from typing import Any, Awaitable, Callable

from .logs import RunLog

Handler = Callable[..., Awaitable[str]]


def obj(required: list[str] | None = None, **props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}


def arr(items: dict[str, Any] = S) -> dict[str, Any]:
    return {"type": "array", "items": items}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    source: str = "gek"

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


class ToolBox:
    def __init__(self, actor: str, log: RunLog, max_output: int = 12000):
        self.actor = actor
        self.log = log
        self.max_output = max_output
        self.tools: dict[str, Tool] = {}

    def add(self, name: str, description: str, parameters: dict[str, Any], handler: Handler,
            source: str = "gek") -> None:
        self.tools[name] = Tool(name, description, parameters, handler, source)

    def add_tool(self, tool: Tool) -> None:
        if tool.name in self.tools:
            self.log.event("note", self.actor, error=f"tool {tool.name!r} from {tool.source} shadows one "
                           f"from {self.tools[tool.name].source}; keeping the first")
            return
        self.tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]

    def sources(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for t in self.tools.values():
            out.setdefault(t.source, []).append(t.name)
        return out

    async def run(self, name: str, raw_args: str) -> str:
        t0 = time.monotonic()
        try:
            args = json.loads(raw_args) if raw_args and raw_args.strip() else {}
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except Exception as e:
            out, ok = f"ERROR: could not parse arguments as JSON ({e}). Raw: {raw_args[:500]}", False
            self.log.event("tool_call", self.actor, name=name, args_raw=raw_args)
            self.log.event("tool_result", self.actor, name=name, ok=ok, output=out)
            return out
        self.log.event("tool_call", self.actor, name=name, args=args)
        try:
            if name in self.tools:
                out = await self.tools[name].handler(**args)
            else:
                out = f"ERROR: no tool named {name!r}. Available: {', '.join(self.tools)}"
            ok = not str(out).startswith("ERROR")
        except TypeError as e:
            out, ok = f"ERROR: bad arguments for {name}: {e}", False
        except Exception as e:
            out, ok = f"ERROR: {type(e).__name__}: {e}", False
        out = str(out)
        full_len = len(out)
        if full_len > self.max_output:
            out = out[: self.max_output] + f"\n...(output truncated; {full_len - self.max_output} more chars)"
        self.log.event("tool_result", self.actor, name=name, ok=ok, chars=full_len,
                       ms=round((time.monotonic() - t0) * 1000), output=out[:4000])
        return out


def granted(globs: list[str], tool: str) -> bool:
    return any(fnmatch.fnmatch(tool, g) for g in globs)
