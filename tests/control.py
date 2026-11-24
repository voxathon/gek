"""Steering a running swarm from outside: `gek kill`, `gek halt`, plain inbox messages,
and `gek push`. Scripted fake model, no API key.

    uv run python tests/control.py
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
from gek.gitrepo import Repo
from gek.logs import RunLog
from gek.swarm import Swarm

_ids = itertools.count()


def call(_tool: str, **args) -> dict:
    return {"id": f"c{next(_ids)}", "type": "function",
            "function": {"name": _tool, "arguments": json.dumps(args)}}


def reply(*calls):
    msg = {"role": "assistant", "content": ""}
    if calls:
        msg["tool_calls"] = list(calls)
    return msg, {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}


def gek(cfg_path: Path, *args: str) -> str:
    r = subprocess.run([sys.executable, "-m", "gek", "-c", str(cfg_path), *args],
                       capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gek-control-"))
    cfg_path = tmp / "config.json"
    write_default_config(cfg_path, "Test steering.")
    raw = json.loads(cfg_path.read_text())
    raw["orchestrator"].update(min_sleep_s=1, debounce_s=0.2, inbox_poll_s=0.3)
    raw["agents"].update(max_steps=500)
    cfg_path.write_text(json.dumps(raw))
    cfg = load_config(cfg_path)
    await Repo.init(cfg.repo_dir, cfg.raw["goal"], None, None)
    log = RunLog(cfg.logs_dir, echo=False)
    swarm = Swarm(cfg, log)
    briefings: list[str] = []

    async def fake(model, messages, tools, actor):
        await asyncio.sleep(0.05)
        if actor == "orchestrator":
            if len(messages) == 2:
                briefings.append(messages[1]["content"])
                if len(briefings) == 1:
                    return reply(call("write_role", name="dawdler", mandate="read the log forever"),
                                 call("spawn", role="dawdler", count=2),
                                 call("sleep", seconds=600, reason="watch"))
            return reply(call("sleep", seconds=600, reason="keep watching"))
        return reply(call("read_log", refs=["swarm/chatter"], limit=1))

    swarm.llm.fake = fake
    lock = cfg.swarm_dir / "run.lock"

    async def outside() -> dict:
        res: dict = {}
        while len(swarm.alive()) < 2:
            await asyncio.sleep(0.1)
        lock.write_text(json.dumps({"pid": 0, "run": log.run_id}))  # what `gek run` writes
        victim = swarm.alive()[0].id
        res["victim"] = victim
        res["inbox"] = await asyncio.to_thread(gek, cfg_path, "inbox", "how's it going?", "--as", "moxxie")
        res["kill"] = await asyncio.to_thread(gek, cfg_path, "kill", victim, "too slow", "--as", "moxxie")
        for _ in range(100):
            if swarm.agents[victim].status == "killed":
                break
            await asyncio.sleep(0.1)
        res["halt"] = await asyncio.to_thread(gek, cfg_path, "halt", "testing halt", "--as", "moxxie")
        return res

    ext = asyncio.create_task(outside())
    reason = await asyncio.wait_for(swarm.run(), timeout=90)
    res = await ext
    log.close()
    lock.unlink(missing_ok=True)

    bare = tmp / "dest.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    pushed = gek(cfg_path, "push", str(bare))
    pushed_all = gek(cfg_path, "push", str(bare), "--everything", "--to", "all")
    branches = subprocess.run(["git", "--git-dir", str(bare), "branch"], capture_output=True,
                              text=True).stdout

    ev = [json.loads(l) for l in open(log.dir / "events.jsonl")]
    exits = {e["actor"]: e["data"] for e in ev if e["kind"] == "agent_exit"}
    controls = [e for e in ev if e["kind"] == "control"]
    victim = res["victim"]
    checks = {
        "kill lands": exits.get(victim, {}).get("how") == "killed",
        "kill is on the record as moxxie": any(e["kind"] == "agent_kill" and e["actor"] == "moxxie"
                                              and e["data"]["target"] == victim for e in ev),
        "control events logged": {c["data"]["verb"] for c in controls} == {"kill", "halt"},
        "plain inbox still wakes": any(e["kind"] == "inbox" and e["data"].get("external") for e in ev),
        "halt stops the swarm": reason.startswith("halted by moxxie"),
        "halt kills the rest": all(d.get("how") == "killed" for d in exits.values()) and len(exits) == 2,
        "push main": "gek/" in pushed and "/main" in branches,
        "push everything": "all/swarm/chatter" in branches and "all/main" in branches,
    }
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    if not all(checks.values()):
        print(res, reason, pushed, pushed_all, branches, sep="\n")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
