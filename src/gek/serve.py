"""`gek mcp-serve --agent ROLE/HEX`: one agent's tools as a stdio MCP server.

For agent runtimes that aren't gek's own loop (another CLI agent, a framework, a human with an
MCP client). The server hands out exactly the tools that agent's role is granted (gek's core
tools by power, MCP servers and plugin tools by glob) through the same registry and logging
as gek's own agents. Only tools/list and tools/call: the part of MCP every client implements.

It runs as its own process, beside or without a running harness. Git is the medium, so most
tools work from here unchanged: read_log, read_file, write_file, commit, post, interrupt,
merge, diff, run (sandboxed the same way). Two need the harness and go through the inbox
like any outside control: kill (a `kill` control commit, which a running swarm carries out)
and list_agents (read from the current run's events).

The agent's worktree must exist (spawn it first). Events go to logs/mcp-serve/<id-and-time>/,
which gek explain and gek resume ignore.

Stdout is the MCP channel: nothing else may print there.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .agent import Agent, AgentHandle, mcp_grants, safe_env
from .config import Config
from .gitrepo import INBOX, Identity
from .logs import RunLog
from .plugin import PluginHost


class ServeError(RuntimeError):
    pass


def live_agents(cfg: Config) -> list[dict[str, Any]]:
    """Agents spawned in the latest run with no exit yet."""
    from .logs import list_runs

    runs = list_runs(cfg.logs_dir)
    if not runs:
        return []
    live: dict[str, dict[str, Any]] = {}
    with open(runs[-1] / "events.jsonl", encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("kind") == "agent_spawn":
                d = e.get("data") or {}
                live[d.get("id", "")] = {"id": d.get("id"), "role": d.get("role"), "branch": d.get("branch"),
                                         "since": e.get("ts")}
            elif e.get("kind") == "agent_exit":
                live.pop(e.get("actor", ""), None)
            elif e.get("kind") == "run_end":
                live.clear()
    return list(live.values())


class RemoteAgent(Agent):
    """An Agent whose harness-only tools go through the inbox / the event log."""

    async def t_list_agents(self) -> str:
        rows = [f"{a['id']:<24} live since {a['since']} branch={a['branch']}" for a in live_agents(self.swarm.cfg)]
        return "\n".join(rows) or "(no live agents in the current run, or no run)"

    async def t_kill(self, agent_id: str, reason: str) -> str:
        role = agent_id.split("/")[0]
        if not self.h.role.may_kill(role):
            return f"ERROR: you may not kill role {role}"
        if agent_id == self.h.id:
            return "ERROR: use finish to end yourself"
        if agent_id not in {a["id"] for a in live_agents(self.swarm.cfg)}:
            return f"ERROR: no live agent {agent_id}"
        from .cli import control_message

        msg = control_message(self.h.id, f"kill {agent_id}", reason)
        sha = await self.swarm.repo.empty_commit(INBOX, msg, Identity.for_actor(self.h.id), actor=self.h.id)
        return f"asked the harness to kill {agent_id} (inbox {sha[:10]}); a running swarm carries it out"

    async def t_finish(self, summary: str) -> str:
        self.finished = summary
        await self.t_post(f"finished: {summary}")
        return "ok, noted on chatter. (Over MCP, your runtime decides when you stop.)"


async def build(cfg: Config, agent_id: str) -> tuple[Any, RemoteAgent]:
    from .swarm import Swarm

    if "/" not in agent_id:
        raise ServeError(f"agent id looks like ROLE/HEX (e.g. coder/ab12), not {agent_id!r}")
    role_name, hexid = agent_id.split("/", 1)
    wt = cfg.worktrees_dir / f"{role_name}-{hexid}"
    if not wt.is_dir():
        raise ServeError(f"no worktree for {agent_id} at {wt}; spawn the agent first (gek spawn {role_name})")
    log = RunLog(cfg.logs_dir / "mcp-serve", run_id=time.strftime("%Y%m%d-%H%M%S-") + f"{role_name}-{hexid}",
                 echo="none")
    swarm = Swarm(cfg, log)
    await swarm.load_roles()
    role = swarm.roles.get(role_name)
    if role is None:
        raise ServeError(f"no role {role_name!r} on swarm/roles (it may have been retired)")
    h = AgentHandle(id=agent_id, role=role, branch=f"role/{role_name}/{hexid}", worktree=wt,
                    note="served over MCP")
    h.status = "running"
    agent = RemoteAgent(swarm, h)
    swarm.plugins = PluginHost(swarm, serve_only=True)
    await swarm.plugins.start()
    await agent.mcp.connect(mcp_grants(role.powers.mcp, cfg), cfg.raw.get("mcp_servers", {}),
                            base_env=safe_env(cfg))
    agent._build_tools()
    log.event("note", agent_id, serving=sorted(agent.box.tools), pid=__import__("os").getpid())
    return swarm, agent


async def serve(cfg: Config, agent_id: str) -> None:
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    swarm, agent = await build(cfg, agent_id)
    box = agent.box

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[
            types.Tool(name=t.name, description=t.description, inputSchema=t.parameters)
            for t in box.tools.values()])

    async def on_call_tool(ctx, params) -> types.CallToolResult:
        out = await box.run(params.name, json.dumps(params.arguments or {}))
        return types.CallToolResult(content=[types.TextContent(type="text", text=out)],
                                    isError=out.startswith("ERROR"))

    server = Server("gek", version=__import__("gek").__version__,
                    instructions=f"Tools for gek agent {agent_id} (role {agent.h.role.name}) "
                                 f"in swarm {cfg.name}: {agent.h.role.mandate}",
                    on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        try:
            await agent.mcp.close()
        except BaseException:
            pass
        await swarm.plugins.stop()
        swarm.log.event("note", agent_id, serving_stopped=True)
        swarm.log.close()


def mcp_config(cfg: Config, agent_id: str) -> dict[str, Any]:
    """A client config snippet that launches this server."""
    import sys

    where = ["--config", str(cfg.path)] if cfg.portable else ["--swarm", cfg.name]
    args = ["-m", "gek"] + where + ["mcp-serve", "--agent", agent_id]
    return {"mcpServers": {f"gek-{agent_id.replace('/', '-')}": {"command": sys.executable, "args": args}}}


__all__ = ["serve", "build", "mcp_config", "ServeError", "live_agents", "Path"]
