"""End-to-end smoke test with a scripted fake model (no network, no API key).

    uv run python tests/smoke.py

Creates a throwaway swarm in a temp dir and plays a script: the orchestrator
writes roles (one rejected by the ceiling), spawns a coder; the coder commits,
gets refused for writing outside its scope, finishes; the orchestrator wakes on
the exit, spawns a reviewer and a second coder; the reviewer merges the first
coder's branch and kills the second; an external inbox message arrives; the
orchestrator halts. Then `gek explain` runs over the logs.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from gek.config import load_config, write_default_config
from gek.explain import explain
from gek.gitrepo import Repo
from gek.logs import RunLog
from gek.swarm import Swarm

_ids = itertools.count()


def call(_tool: str, **args) -> dict:
    return {"id": f"call_{next(_ids)}", "type": "function",
            "function": {"name": _tool, "arguments": json.dumps(args)}}


def reply(*calls, content: str = "") -> tuple[dict, dict]:
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = list(calls)
    return msg, {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


def make_fake(swarm: Swarm):
    state = {"orch_wake": 0, "per_agent": {}}

    async def fake(model, messages, tools, actor):
        await asyncio.sleep(0.05)
        last = messages[-1]
        if actor == "orchestrator":
            if len(messages) == 2:  # fresh wake: system prior + wake briefing
                state["orch_wake"] += 1
            w = state["orch_wake"]
            if last["role"] == "tool":
                if w == 1 and "rejected" in last["content"] and "bad" not in state:
                    state["bad"] = True
                    return reply(call("spawn", role="coder", note="write src/hello.py"))
                if w == 1:
                    return reply(call("sleep", seconds=30, reason="coder working"))
                if w == 2:
                    return reply(call("sleep", seconds=30, reason="reviewer reviewing"))
                return reply(call("halt", reason="hello merged; done"))
            if w == 1:
                return reply(
                    call("write_role", name="coder", mandate="write code under src/", writes=["src/"],
                         powers={"shell": True, "mcp": {"calc": ["add"]}}),
                    call("write_role", name="reviewer", mandate="review and merge coder branches",
                         powers={"merge": True, "kill": ["coder"]}),
                    call("write_role", name="bad", mandate="too powerful", powers={"mcp": {"nope": ["*"]}}),
                )
            if w == 2:
                coder1 = next(a for a in swarm.agents.values() if a.role.name == "coder")
                return reply(call("spawn", role="reviewer", note=f"review {coder1.branch}"),
                             call("spawn", role="coder", note="dawdle"))
            if last["role"] == "user" and "Use your tools" in last["content"]:
                return reply(call("halt", reason="hello merged; done"))
            return reply(content="thinking out loud with no tool call")  # exercise the nudge
        # agents
        n = state["per_agent"].get(actor, 0)
        state["per_agent"][actor] = n + 1
        h = swarm.agents[actor]
        if h.role.name == "coder" and "write src/hello.py" in h.note:
            script = [
                reply(call("write_file", path="src/hello.py", content="print('hello from the swarm')\n")),
                reply(call("run", command="python3 src/hello.py || python src/hello.py")),
                reply(call("commit", message="add hello")),
                reply(call("write_file", path="README.md", content="sneaky\n")),
                reply(call("commit", message="sneak a readme")),
                reply(call("run", command="git checkout -- . ; git clean -fd")),
                reply(call("post", message=f"hello.py ready on {h.branch}")),
                reply(call("finish", summary=f"hello.py committed on {h.branch}")),
            ]
            return script[min(n, len(script) - 1)]
        if h.role.name == "coder":  # the dawdler: reads the log forever until killed
            await asyncio.sleep(0.3)
            if n == 0:
                return reply(call("mcp__calc__add", a=2, b=40))
            return reply(call("read_log", refs=["swarm/chatter"], limit=3))
        if h.role.name == "reviewer":
            branch = h.note.split()[-1]
            if n == 3:  # let the dawdler get going (and use its MCP tool) before killing it
                while any("dawdle" in a.note and a.steps < 3 for a in swarm.alive()):
                    await asyncio.sleep(0.1)
            dawdler = next((a for a in swarm.alive() if "dawdle" in a.note), None)
            script = [
                reply(call("diff", branch=branch)),
                reply(call("merge", branch=branch)),
                reply(call("list_agents")),
                reply(call("kill", agent_id=dawdler.id if dawdler else "x", reason="dawdling")),
                reply(call("finish", summary="merged hello, killed the dawdler")),
            ]
            return script[min(n, len(script) - 1)]
        return reply(call("finish", summary="?"))

    return fake


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gek-smoke-"))
    cfg_path = tmp / "config.json"
    write_default_config(cfg_path, "Make a hello world program.")
    raw = json.loads(cfg_path.read_text())
    raw["orchestrator"].update(min_sleep_s=1, debounce_s=0.5, inbox_poll_s=0.5, default_sleep_s=5)
    srv = tmp / "calc_server.py"
    srv.write_text("from mcp.server.mcpserver import MCPServer\nm = MCPServer('calc')\n"
                   "@m.tool()\ndef add(a: int, b: int) -> int:\n    return a + b\n"
                   "@m.tool()\ndef secret() -> str:\n    return 'should be hidden'\nm.run()\n")
    raw["mcp_servers"] = {"calc": {"command": sys.executable, "args": [str(srv)]}}
    raw["ceiling"]["mcp"] = {"calc": ["add"]}
    cfg_path.write_text(json.dumps(raw, indent=2))
    cfg = load_config(cfg_path)
    await Repo.init(cfg.repo_dir, cfg.raw["goal"], None, None)

    log = RunLog(cfg.logs_dir, echo=True)
    swarm = Swarm(cfg, log)
    swarm.llm.fake = make_fake(swarm)

    async def outside_inbox():
        # an external sender, like `gek inbox`, in another process
        while not any(a.role.name == "reviewer" and a.status == "finished" for a in swarm.agents.values()):
            await asyncio.sleep(0.2)
        subprocess.run([sys.executable, "-m", "gek", "-c", str(cfg_path), "inbox", "status?", "--as", "moxxie"],
                       check=True)

    ext = asyncio.create_task(outside_inbox())
    reason = await asyncio.wait_for(swarm.run(), timeout=120)
    ext.cancel()
    log.close()
    print("\nSTOPPED:", reason)

    repo = Repo(cfg.repo_dir)
    main_files = await repo.list_files_at("main")
    hello = await repo.read_file_at("main", "src/hello.py")
    ev = [json.loads(l) for l in open(log.dir / "events.jsonl")]
    kinds = [e["kind"] for e in ev]
    exits = {e["actor"]: e["data"]["how"] for e in ev if e["kind"] == "agent_exit"}
    checks = {
        "hello merged to main": hello is not None and "hello" in hello,
        "readme kept off main": "README.md" not in main_files,
        "bad role rejected": "role_rejected" in kinds,
        "commit refused outside scope": any(e["kind"] == "tool_result" and "refusing to commit" in e["data"]["output"]
                                            for e in ev),
        "a coder finished": "finish" in exits.values(),
        "a coder was killed": "killed" in exits.values(),
        "external inbox seen": any(e["kind"] == "inbox" and e["data"].get("external") for e in ev),
        "halted": reason == "hello merged; done",
        "mcp tool worked": any(e["kind"] == "tool_result" and e["data"]["name"] == "mcp__calc__add"
                               and e["data"]["output"].strip() == "42" for e in ev),
        "mcp secret hidden": all("secret" not in (e["data"].get("granted") or []) for e in ev if e["kind"] == "mcp"),
        "transcripts written": len(list((log.dir / "agents").glob("*.jsonl"))) >= 4,
    }
    print(await explain(cfg))
    print("\nCHECKS")
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
