"""Limits, budgets, pricing and briefing checks with a scripted fake model (no API key).

    uv run python tests/limits.py
"""

from __future__ import annotations

import asyncio
import itertools
import json
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


def reply(*calls, prompt=1000, cached=600, out=100):
    msg = {"role": "assistant", "content": ""}
    if calls:
        msg["tool_calls"] = list(calls)
    return msg, {"prompt_tokens": prompt, "completion_tokens": out, "total_tokens": prompt + out,
                 "cached_prompt_tokens": cached}


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gek-limits-"))
    cfg_path = tmp / "config.json"
    write_default_config(cfg_path, "Test limits.")
    raw = json.loads(cfg_path.read_text())
    raw["orchestrator"].update(min_sleep_s=1, debounce_s=0.2, inbox_poll_s=0.3)
    raw["agents"].update(max_steps=12, status_every_steps=3,
                         env_notes=["uv is installed: use `uv run`", "pytest is installed"])
    raw["models"]["pricing"] = {"openai/auto": {"input": 1.0, "cached_input": 0.1, "output": 2.0}}
    cfg_path.write_text(json.dumps(raw))
    cfg = load_config(cfg_path)
    await Repo.init(cfg.repo_dir, cfg.raw["goal"], None, None)
    log = RunLog(cfg.logs_dir, echo=False)
    swarm = Swarm(cfg, log)
    seen: dict[str, list] = {"briefings": [], "systems": {}, "status_lines": []}

    async def fake(model, messages, tools, actor):
        await asyncio.sleep(0.01)
        if actor == "orchestrator":
            if len(messages) == 2:
                seen["briefings"].append(messages[1]["content"])
                n = len(seen["briefings"])
                if n == 1:
                    return reply(
                        call("write_role", name="capped", mandate="burn tokens", max_tokens=3000),
                        call("write_role", name="stepper", mandate="burn steps"),
                        call("spawn", role="capped"), call("spawn", role="stepper"),
                        call("sleep", seconds=60, reason="watch caps"))
                return reply(call("halt", reason="done"))
        seen["systems"].setdefault(actor, messages[0]["content"])
        for m in messages:
            if m.get("role") == "tool" and "[gek: step" in m.get("content", ""):
                line = m["content"][m["content"].index("[gek: step"):]
                if line not in seen["status_lines"]:
                    seen["status_lines"].append(line)
        return reply(call("read_log", refs=["swarm/chatter"], limit=1))

    swarm.llm.fake = fake

    reason = await asyncio.wait_for(swarm.run(), timeout=90)
    log.close()
    ev = [json.loads(l) for l in open(log.dir / "events.jsonl")]
    exits = {e["actor"]: e["data"] for e in ev if e["kind"] == "agent_exit"}
    capped = next((d for a, d in exits.items() if a.startswith("capped/")), {})
    stepper = next((d for a, d in exits.items() if a.startswith("stepper/")), {})
    b1 = seen["briefings"][0] if seen["briefings"] else ""
    b2 = seen["briefings"][1] if len(seen["briefings"]) > 1 else ""
    sys_capped = next((v for k, v in seen["systems"].items() if k.startswith("capped/")), "")
    costs = [e["data"]["cost"] for e in ev if e["kind"] == "llm_response"]
    expected = (400 * 1.0 + 600 * 0.1 + 100 * 2.0) / 1e6
    checks = {
        "token cap stops agent": capped.get("how") == "token_cap",
        "step cap stops agent with a summary": stepper.get("how") == "max_steps"
                                               and "step cap" in (stepper.get("summary") or ""),
        "status lines shown": any("tokens used" in l for l in seen["status_lines"]),
        "near-limit warning shown": any("Close to a limit" in l for l in seen["status_lines"]),
        "agent sees its limits": "12 steps" in sys_capped and "3,000 tokens" in sys_capped,
        "agent sees env notes": "uv is installed" in sys_capped,
        "briefing has limits": "each agent: 12 steps" in b1 and "steps per wake" in b1,
        "briefing has alias mapping": '"cheap": "openai/auto"' in b1,
        "briefing has env notes": "pytest is installed" in b1,
        "briefing has per-agent tokens": "tokens=" in b2 and "/3k" in b2,
        "briefing has own spend": "you alone:" in b2,
        "custom pricing applied": bool(costs) and abs(costs[0] - expected) < 1e-12,
        "halted": reason == "done",
    }
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    if not all(checks.values()):
        print("\n--- briefing 2 ---\n" + b2[:3000])
        print("status lines:", seen["status_lines"][:5])
        print("exits:", exits)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
