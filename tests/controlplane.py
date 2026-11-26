"""The control plane end to end (docs/CONTROL.md), with a scripted fake model and the real CLI:
role set, spawn, live config (incl. refusals and the ceiling), human mode, wake, pause/resume,
hand edits to config.json, failures reported, outside actions in the briefing, offline paths.

    uv run python tests/controlplane.py
"""

from __future__ import annotations

import asyncio
import itertools
import json
import subprocess
import sys
import tempfile
import time
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


def gek(cfg_path: Path, *args: str, stdin: str | None = None) -> tuple[int, str]:
    r = subprocess.run([sys.executable, "-m", "gek", "-c", str(cfg_path), *args],
                       capture_output=True, text=True, input=stdin)
    return r.returncode, (r.stdout + r.stderr).strip()


async def until(pred, timeout=15.0, step=0.1) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gek-cp-"))
    cfg_path = tmp / "config.json"
    write_default_config(cfg_path, "Test the control plane.")
    raw = json.loads(cfg_path.read_text())
    raw["orchestrator"].update(min_sleep_s=0.5, debounce_s=0.2, inbox_poll_s=0.2, default_sleep_s=600)
    raw["agents"].update(max_steps=10_000)
    cfg_path.write_text(json.dumps(raw, indent=2))
    cfg = load_config(cfg_path)
    await Repo.init(cfg.repo_dir, cfg.raw["goal"], None, None)
    log = RunLog(cfg.logs_dir, echo=False)
    swarm = Swarm(cfg, log)
    briefings: list[str] = []
    orch_calls = {"n": 0}

    async def fake(model, messages, tools, actor):
        await asyncio.sleep(0.02)
        if actor == "orchestrator":
            orch_calls["n"] += 1
            if len(messages) == 2:
                briefings.append(messages[1]["content"])
            return reply(call("sleep", seconds=600, reason="idle"))
        await asyncio.sleep(0.08)
        return reply(call("read_log", refs=["swarm/chatter"], limit=1))

    swarm.llm.fake = fake
    lock = cfg.swarm_dir / "run.lock"
    r: dict = {}

    async def outside() -> None:
        await until(lambda: len(briefings) >= 1)
        lock.write_text(json.dumps({"pid": 0, "run": log.run_id}))
        G = lambda *a, **k: asyncio.to_thread(gek, cfg_path, *a, **k)  # noqa: E731

        worker = {"name": "worker", "mandate": "read the log", "powers": {"merge": True}}
        r["role_set"] = await G("role", "set", "-", "--as", "moxxie", stdin=json.dumps(worker))
        r["role_seen"] = await until(lambda: "worker" in swarm.roles)
        r["spawn"] = await G("spawn", "worker", "look around", "--as", "moxxie")
        r["spawned"] = await until(lambda: len(swarm.alive()) == 1)

        r["cfg"] = await G("config", "set", "agents.max_alive", "1", "--as", "moxxie")
        r["cfg_applied"] = await until(lambda: swarm.cfg.section("agents").get("max_alive") == 1)
        r["cfg_persisted"] = json.loads(cfg_path.read_text())["agents"]["max_alive"] == 1

        # MCP servers change live: set, disable, enable, rm (docs/CONTROL.md)
        from gek.cli import send_control
        calc = {"command": sys.executable, "args": ["-c", "print(1)"]}
        gen0 = getattr(swarm, "mcp_generation", 0)
        await send_control(cfg, "moxxie", "mcp set calc", "add a server", json.dumps(calc))
        r["mcp_set"] = await until(lambda: (swarm.cfg.raw.get("mcp_servers") or {}).get("calc", {})
                                   .get("command") == sys.executable)
        r["mcp_gen"] = getattr(swarm, "mcp_generation", 0) > gen0
        r["mcp_persisted"] = ((json.loads(cfg_path.read_text()).get("mcp_servers") or {}).get("calc") or {}).get(
            "args") == ["-c", "print(1)"]
        await send_control(cfg, "moxxie", "mcp disable calc", "not needed yet")
        r["mcp_disabled"] = await until(lambda: (swarm.cfg.raw.get("mcp_servers") or {}).get("calc", {})
                                        .get("enabled") is False)
        await send_control(cfg, "moxxie", "mcp enable calc", "needed again")
        r["mcp_enabled"] = await until(lambda: (swarm.cfg.raw.get("mcp_servers") or {}).get("calc", {})
                                       .get("enabled") is not False)
        await send_control(cfg, "moxxie", "mcp rm calc", "gone for good")
        r["mcp_rm"] = await until(lambda: "calc" not in (swarm.cfg.raw.get("mcp_servers") or {}))
        r["mcp_rm_persisted"] = "calc" not in (json.loads(cfg_path.read_text()).get("mcp_servers") or {})
        r["spawn2"] = await G("spawn", "worker", "--as", "moxxie")  # over max_alive: must fail
        r["cold_cli"] = await G("config", "set", "swarm_dir", '"elsewhere"')

        await send_control(cfg, "moxxie", "config set api.api_key_env", "sneaky", '"X"')
        r["ceiling_no_yes"] = await G("config", "set", "ceiling.merge", "false")
        r["ceiling"] = await G("config", "set", "ceiling.merge", "false", "--yes", "--as", "moxxie")
        await until(lambda: swarm.cfg.raw["ceiling"]["merge"] is False)

        r["human"] = await G("mode", "human", "my turn", "--as", "moxxie")
        await until(lambda: swarm.mode == "human")
        before = orch_calls["n"]
        r["wake"] = await G("wake", "check this", "--as", "moxxie")
        await asyncio.sleep(1.5)
        r["human_no_model"] = orch_calls["n"] == before

        r["pause"] = await G("pause", "hold on", "--as", "moxxie")
        await until(lambda: swarm.paused)
        await asyncio.sleep(0.5)
        a = swarm.alive()[0]
        s0 = a.steps
        await asyncio.sleep(1.0)
        r["paused_still"] = a.steps == s0
        r["resume"] = await G("unpause", "go", "--as", "moxxie")
        await until(lambda: not swarm.paused)
        await asyncio.sleep(1.0)
        r["resumed_moving"] = a.steps > s0

        # a hand edit to config.json is picked up
        data = json.loads(cfg_path.read_text())
        data["budget"] = {"max_total_tokens": 4_242_424}
        time.sleep(0.05)
        cfg_path.write_text(json.dumps(data, indent=2))
        r["hand_edit"] = await until(lambda: swarm.cfg.raw["budget"]["max_total_tokens"] == 4_242_424)

        n_brief = len(briefings)
        r["llm"] = await G("mode", "llm", "your turn", "--as", "moxxie")
        r["llm_wakes"] = await until(lambda: len(briefings) > n_brief)
        r["halt"] = await G("halt", "done testing", "--as", "moxxie")

    ext = asyncio.create_task(outside())
    reason = await asyncio.wait_for(swarm.run(), timeout=120)
    await ext
    log.close()
    lock.unlink(missing_ok=True)

    # offline paths
    off_role = gek(cfg_path, "role", "set", "-", stdin=json.dumps({"name": "scout", "mandate": "look"}))
    off_cfg = gek(cfg_path, "config", "set", "agents.max_steps", "77")
    off_mode = gek(cfg_path, "mode", "human")
    off_spawn = gek(cfg_path, "spawn", "scout")
    off_list = gek(cfg_path, "role", "list")
    from gek.cli import do_control
    off_mcp = await do_control(load_config(cfg_path), "moxxie", "mcp set calc", "set while stopped",
                               json.dumps({"command": "uvx", "args": ["mcp-server-fetch"]}))
    mid = json.loads(cfg_path.read_text())
    off_mcp_rm = await do_control(load_config(cfg_path), "moxxie", "mcp rm calc", "rm while stopped")
    saved = json.loads(cfg_path.read_text())

    ev = [json.loads(l) for l in open(log.dir / "events.jsonl")]
    controls = [e for e in ev if e["kind"] == "control"]
    by_verb = {}
    for c in controls:
        by_verb.setdefault((c["data"]["verb"], c["data"]["arg"].split(" ")[0]), []).append(c)
    last_brief = briefings[-1] if briefings else ""
    chatter = subprocess.run(["git", "--git-dir", str(cfg.repo_dir), "log", "--format=%B", "swarm/chatter"],
                             capture_output=True, text=True).stdout
    checks = {
        "role set over the inbox": r["role_set"][0] == 0 and r["role_seen"],
        "spawn over the inbox": r["spawn"][0] == 0 and r["spawned"],
        "spawn is on the record as moxxie": any(e["kind"] == "agent_spawn" and e["actor"] == "moxxie" for e in ev),
        "config set applied live": r["cfg_applied"],
        "config set persisted": r["cfg_persisted"],
        "max_alive enforced, failure reported": any(not c["data"]["ok"] for c in by_verb.get(("spawn", "worker"), []))
                                                and "Control-Failed" in chatter,
        "cold key refused by the cli": r["cold_cli"][0] != 0 and "restart" in r["cold_cli"][1],
        "cold key refused by the harness": any(c["data"]["verb"] == "config" and not c["data"]["ok"]
                                               and "api.api_key_env" in c["data"]["arg"] for c in controls),
        "ceiling change asks first": r["ceiling_no_yes"][0] != 0,
        "ceiling change flags roles above it": any(e["kind"] == "note" and e["data"].get("roles_above_ceiling")
                                                   for e in ev),
        "human mode: no model call on wake": r["human_no_model"]
                                             and any(e["kind"] == "human_wake" for e in ev),
        "pause holds agents": r["paused_still"],
        "resume releases them": r["resumed_moving"],
        "hand edit to config.json applied": r["hand_edit"]
                                            and any(e["kind"] == "config" and e["actor"] == "file" for e in ev),
        "back to llm wakes the model": r["llm_wakes"],
        "briefing lists outside actions": "Outside actions since your last wake" in last_brief
                                          and "moxxie: spawn worker" in last_brief
                                          and "config.json edited by hand" in last_brief
                                          and "moxxie: mcp set calc" in last_brief,
        "mcp set applied live": r["mcp_set"],
        "mcp set bumps the generation live agents sync on": r["mcp_gen"],
        "mcp set persisted": r["mcp_persisted"],
        "mcp disable applied live": r["mcp_disabled"],
        "mcp enable applied live": r["mcp_enabled"],
        "mcp rm applied live": r["mcp_rm"],
        "mcp rm persisted": r["mcp_rm_persisted"],
        "mcp commands are on the record": any(c["data"]["verb"] == "mcp" and c["data"]["ok"]
                                              and c["data"]["arg"].split(" ")[0] in ("set", "rm", "enable", "disable")
                                              for c in controls)
                                          and any(e["kind"] == "config" and e["data"]["key"].startswith("mcp_servers")
                                                  for e in ev),
        "offline mcp set saves for the next run": off_mcp[0] == 0 and "applies on the next run" in off_mcp[1]
                                                  and (mid.get("mcp_servers") or {}).get("calc", {}).get("command") == "uvx",
        "offline mcp rm saves for the next run": off_mcp_rm[0] == 0 and "applies on the next run" in off_mcp_rm[1]
                                                 and "calc" not in (saved.get("mcp_servers") or {}),
        "halt": reason.startswith("halted by moxxie"),
        "offline role set": off_role[0] == 0 and "scout" in off_list[1],
        "offline config set": off_cfg[0] == 0 and saved["agents"]["max_steps"] == 77,
        "offline mode": off_mode[0] == 0 and saved["orchestrator"]["mode"] == "human",
        "offline spawn refused": off_spawn[0] != 0 and "isn't running" in off_spawn[1],
    }
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    if not all(checks.values()):
        print(json.dumps({k: v for k, v in r.items()}, indent=1, default=str)[:3000])
        print(last_brief[:2500])
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
