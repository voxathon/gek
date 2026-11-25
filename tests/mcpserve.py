"""`gek mcp-serve`: a running swarm (scripted fake model) spawns agents; an outside MCP client
launches `gek mcp-serve --agent ...` for one of them and works through it: sees only that
role's tools, writes and commits as the agent, posts, uses an MCP tool and a plugin tool
through gek, and kills another agent via the inbox.

    uv run python tests/mcpserve.py
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
import tempfile
from pathlib import Path

from gek.config import load_config, write_default_config
from gek.gitrepo import CHATTER, Repo
from gek.logs import RunLog
from gek.swarm import Swarm

_ids = itertools.count()
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")


def call(_tool: str, **args) -> dict:
    return {"id": f"c{next(_ids)}", "type": "function",
            "function": {"name": _tool, "arguments": json.dumps(args)}}


def reply(*calls):
    return ({"role": "assistant", "content": "", "tool_calls": list(calls)},
            {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})


PLUG = '''
from gek.plugin import Plugin, PluginTool
class Shout(Plugin):
    def tools(self):
        async def shout(caller, text):
            return f"{caller.id} SHOUTS {text.upper()}"
        return [PluginTool("shout", "Shout.", {"type": "object", "properties": {"text": {"type": "string"}},
                           "required": ["text"]}, shout)]
gek_plugin = Shout
'''


def text_of(res) -> str:
    return "\n".join(getattr(c, "text", "") for c in res.content)


async def client_session(cfg_path: Path, agent_id: str, work):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "gek", "--config", str(cfg_path), "mcp-serve", "--agent", agent_id],
                                   env={**os.environ})
    with open(cfg_path.parent / "serve-stderr.txt", "w") as err:
        async with stdio_client(params, errlog=err) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await work(s)


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gek-mcpserve-"))
    os.environ["GEK_HOME"] = str(tmp / "gekhome")
    cfg_path = tmp / "config.json"
    write_default_config(cfg_path, "Test mcp-serve.")
    raw = json.loads(cfg_path.read_text())
    raw["orchestrator"].update(min_sleep_s=0.5, debounce_s=0.2, inbox_poll_s=0.3)
    srv = tmp / "calc_server.py"
    srv.write_text("from mcp.server.mcpserver import MCPServer\nm = MCPServer('calc')\n"
                   "@m.tool()\ndef add(a: int, b: int) -> int:\n    return a + b\n"
                   "@m.tool()\ndef secret() -> str:\n    return 'hidden'\nm.run()\n")
    raw["mcp_servers"] = {"calc": {"command": sys.executable, "args": [str(srv)]}}
    raw["plugins"] = {"shout": {}}
    raw["ceiling"]["mcp"] = {"calc": ["*"], "shout": ["*"]}
    (tmp / "plugins").mkdir()
    (tmp / "plugins" / "shout.py").write_text(PLUG)
    cfg_path.write_text(json.dumps(raw, indent=2))
    cfg = load_config(cfg_path)
    await Repo.init(cfg.repo_dir, cfg.raw["goal"], None, None)

    log = RunLog(cfg.logs_dir, echo=False)
    swarm = Swarm(cfg, log)
    st: dict = {"spawned": False, "done": asyncio.Event()}

    async def fake(model, messages, tools, actor):
        await asyncio.sleep(0.05)
        if actor == "orchestrator":
            if not st["spawned"]:
                st["spawned"] = True
                return reply(call("write_role", name="outsider", mandate="work over MCP", writes=["src/"],
                                  powers={"kill": ["victim"], "mcp": {"calc": ["add"], "shout": ["shout"]}}),
                             call("write_role", name="victim", mandate="wait around", writes=["*"]),
                             call("spawn", role="outsider", note="driven from outside"),
                             call("spawn", role="victim", note="be killed"),
                             call("sleep", seconds=600, reason="wait"))
            if st["done"].is_set():
                return reply(call("halt", reason="done"))
            return reply(call("sleep", seconds=600, reason="wait"))
        await asyncio.sleep(0.3)  # in-harness agents just idle
        return reply(call("read_log", refs=["swarm/chatter"], limit=1))

    swarm.llm.fake = fake
    run = asyncio.create_task(swarm.run())
    for _ in range(200):
        if len(swarm.alive()) == 2:
            break
        await asyncio.sleep(0.1)
    outsider = next(a.id for a in swarm.agents.values() if a.role.name == "outsider")
    victim = next(a.id for a in swarm.agents.values() if a.role.name == "victim")
    # the harness's own copy of the outsider would race us; stop it, keep its worktree
    out: dict = {}

    async def work(s):
        listed = await s.list_tools()
        out["tools"] = sorted(t.name for t in listed.tools)
        out["write"] = text_of(await s.call_tool("write_file", {"path": "src/x.py", "content": "X = 1\n"}))
        out["commit"] = text_of(await s.call_tool("commit", {"message": "from outside"}))
        out["post"] = text_of(await s.call_tool("post", {"message": "hello over mcp"}))
        out["add"] = text_of(await s.call_tool("mcp__calc__add", {"a": 2, "b": 40}))
        out["shout"] = text_of(await s.call_tool("shout__shout", {"text": "hi"}))
        out["agents"] = text_of(await s.call_tool("list_agents", {}))
        out["kill"] = text_of(await s.call_tool("kill", {"agent_id": victim, "reason": "test"}))
        res = await s.call_tool("merge", {"branch": "main"})
        out["merge_err"] = (res.is_error, text_of(res))
        res = await s.call_tool("read_file", {"path": "../../config.json"})
        out["escape"] = (res.is_error, text_of(res))
        return out

    await client_session(cfg_path, outsider, work)
    for _ in range(100):
        if swarm.agents[victim].status == "killed":
            break
        await asyncio.sleep(0.1)
    st["done"].set()
    swarm.wake.set()
    await asyncio.wait_for(run, timeout=60)
    log.close()

    t = out.get("tools", [])
    check("write_file" in t and "commit" in t and "kill" in t and "list_agents" in t,
          "the client sees the role's core tools")
    check("merge" not in t and "run" not in t and "interrupt" not in t, "...and not the powers it lacks")
    check("mcp__calc__add" in t and "mcp__calc__secret" not in t, "granted MCP tools only")
    check("shout__shout" in t, "plugin tools too")
    repo = Repo(cfg.repo_dir)
    branch = f"role/{outsider.replace('/', '/')}"
    logline = await repo.git("log", "-1", "--format=%an|%s", f"refs/heads/{branch}")
    check("committed" in out.get("commit", "") and logline.strip().startswith(f"{outsider}|from outside"),
          "commits land on the agent's branch, as the agent")
    chatter = await repo.log_text([CHATTER], limit=20)
    check("hello over mcp" in chatter, "posts reach chatter")
    check(out.get("add", "").strip() == "42", "MCP tools work through gek")
    check(f"{outsider} SHOUTS HI" in out.get("shout", ""), "plugin tools know the caller")
    check(victim in out.get("agents", ""), "list_agents reads the running swarm")
    check(swarm.agents[victim].status == "killed", "kill goes through the inbox and the harness carries it out")
    check(out.get("merge_err", (False,))[0], "unknown tools come back as MCP errors")
    check(out.get("escape", (False,))[0] and "outside your worktree" in out["escape"][1],
          "paths outside the worktree are refused")
    ev_dirs = [d for d in (cfg.logs_dir / "mcp-serve").iterdir() if d.is_dir()]
    sev = [json.loads(l) for d in ev_dirs for l in open(d / "events.jsonl")]
    check(any(e["kind"] == "tool_call" and e["data"]["name"] == "commit" for e in sev),
          "serve logs every call under logs/mcp-serve/")
    from gek.logs import list_runs

    check(all("mcp-serve" not in str(p) for p in list_runs(cfg.logs_dir)), "...which isn't mistaken for a run")
    import subprocess

    r = subprocess.run([sys.executable, "-m", "gek", "--config", str(cfg_path), "mcp-serve", "--agent",
                        outsider, "--print-config"], capture_output=True, text=True)
    conf = json.loads(r.stdout or "{}")
    check("mcp-serve" in json.dumps(conf) and outsider in json.dumps(conf), "--print-config gives a client snippet")
    r = subprocess.run([sys.executable, "-m", "gek", "--config", str(cfg_path), "mcp-serve", "--agent", "ghost/0000"],
                       capture_output=True, text=True)
    check(r.returncode == 2 and "no worktree" in r.stderr, "an agent with no worktree is a clear error")

    bad = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    if bad:
        print(json.dumps(out, indent=1, default=str))
        print((tmp / "serve-stderr.txt").read_text()[-3000:])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
