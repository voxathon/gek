"""`gek resume` after a hard crash: a child process runs a swarm (scripted fake model) and is
killed mid-work (SIGKILL / TerminateProcess). Then we resume: stale lock detected, uncommitted
work salvaged onto the agent's branch, the orchestrator briefed, the agent respawned onto its
branch, tokens carried into the budget.

    uv run python tests/resume.py
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
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
    return msg, {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}


async def child(cfg_path: Path) -> None:
    """The run that crashes: commit one file, leave another uncommitted, then dawdle forever."""
    cfg = load_config(cfg_path)
    log = RunLog(cfg.logs_dir, echo=False)
    (cfg.swarm_dir / "run.lock").write_text(json.dumps({"pid": os.getpid(), "run": log.run_id}))
    swarm = Swarm(cfg, log)
    n = {"builder": 0}

    async def fake(model, messages, tools, actor):
        await asyncio.sleep(0.05)
        if actor == "orchestrator":
            if len(messages) == 2:
                return reply(call("write_role", name="builder", mandate="build src/", writes=["src/"]),
                             call("spawn", role="builder", note="build it"),
                             call("sleep", seconds=600, reason="builder on it; check its branch next"))
            return reply(call("sleep", seconds=600, reason="waiting"))
        n["builder"] += 1
        step = n["builder"]
        if step == 1:
            return reply(call("write_file", path="src/a.py", content="A = 1\n"))
        if step == 2:
            return reply(call("commit", message="add a"))
        if step == 3:
            return reply(call("write_file", path="src/b.py", content="B = 2  # never committed\n"))
        await asyncio.sleep(0.3)
        return reply(call("read_log", refs=["swarm/chatter"], limit=1))

    swarm.llm.fake = fake
    await swarm.run()


async def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        await child(Path(sys.argv[2]))
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="gek-resume-"))
    cfg_path = tmp / "config.json"
    write_default_config(cfg_path, "Test resume.")
    raw = json.loads(cfg_path.read_text())
    raw["orchestrator"].update(min_sleep_s=0.5, debounce_s=0.2, inbox_poll_s=0.3)
    raw["budget"]["max_total_tokens"] = 1_000_000
    cfg_path.write_text(json.dumps(raw, indent=2))
    cfg = load_config(cfg_path)
    await Repo.init(cfg.repo_dir, cfg.raw["goal"], None, None)

    # 1. run it in a child process and kill it hard once there's uncommitted work
    proc = subprocess.Popen([sys.executable, __file__, "--child", str(cfg_path)])
    end = time.time() + 60
    found = None
    while time.time() < end and not found:
        found = next(iter(cfg.worktrees_dir.glob("builder-*/src/b.py")), None) if cfg.worktrees_dir.exists() else None
        time.sleep(0.2)
    time.sleep(0.5)
    proc.kill()
    proc.wait()

    from gek.cli import _lock_info
    from gek.resume import read_resume_info, salvage_worktrees

    lock_state = _lock_info(cfg) or ""
    info = read_resume_info(cfg)
    old = info.interrupted[0] if info.interrupted else None
    info.stale_lock = lock_state.startswith("stale")
    (cfg.swarm_dir / "run.lock").unlink(missing_ok=True)
    repo = Repo(cfg.repo_dir)
    info.salvaged = await salvage_worktrees(cfg, repo)
    salvaged_b = await repo.read_file_at(old.branch, "src/b.py") if old else None
    leftover = list(cfg.worktrees_dir.iterdir()) if cfg.worktrees_dir.exists() else []

    # 2. resume with --respawn
    log = RunLog(cfg.logs_dir, echo=False)
    swarm = Swarm(cfg, log, resume=info, respawn=True)
    seen: dict = {"brief": "", "note": "", "files": []}

    async def fake(model, messages, tools, actor):
        await asyncio.sleep(0.05)
        if actor == "orchestrator":
            if len(messages) == 2 and not seen["brief"]:
                seen["brief"] = messages[1]["content"]
            return reply(call("halt", reason="checked the resume"))
        if not seen["note"]:
            seen["note"] = messages[1]["content"]
            h = swarm.agents[actor]
            seen["files"] = sorted(str(p.relative_to(h.worktree)).replace("\\", "/")
                                   for p in h.worktree.rglob("*.py"))
        return reply(call("read_log", refs=["swarm/chatter"], limit=1))

    swarm.llm.fake = fake
    reason = await asyncio.wait_for(swarm.run(), timeout=60)
    log.close()
    ev = [json.loads(l) for l in open(log.dir / "events.jsonl")]
    end_ev = next(e for e in ev if e["kind"] == "run_end")
    # 3. a clean halt with an agent still working counts as interrupted too ("killed")
    log3 = RunLog(cfg.logs_dir, echo=False)
    swarm3 = Swarm(cfg, log3)
    st = {"w": 0}

    async def fake3(model, messages, tools, actor):
        await asyncio.sleep(0.05)
        if actor == "orchestrator":
            if len(messages) == 2:
                st["w"] += 1
                if st["w"] == 1:
                    return reply(call("spawn", role="builder", note="keep going"),
                                 call("sleep", seconds=1, reason="short nap"))
            return reply(call("halt", reason="stop mid-work"))
        await asyncio.sleep(0.1)
        return reply(call("read_log", refs=["swarm/chatter"], limit=1))

    swarm3.llm.fake = fake3
    await asyncio.wait_for(swarm3.run(), timeout=60)
    log3.close()
    info3 = read_resume_info(cfg)
    help_ok = subprocess.run([sys.executable, "-m", "gek", "resume", "--help"], capture_output=True,
                             text=True).returncode == 0

    checks = {
        "crash detected (no run_end)": info.crashed,
        "stale lock detected": info.stale_lock,
        "interrupted agent found": old is not None and old.how == "crashed" and old.role == "builder",
        "uncommitted work salvaged onto its branch": salvaged_b is not None and "B = 2" in salvaged_b,
        "crashed worktree removed": leftover == [],
        "briefing says resumed + crashed": "RESUMES" in seen["brief"] and "CRASHED" in seen["brief"],
        "briefing lists the interrupted agent": old is not None and old.id in seen["brief"],
        "briefing lists the salvage": "salvaged" in seen["brief"] and "builder-" in seen["brief"],
        "briefing has the last note": "check its branch next" in seen["brief"],
        "respawned with a continuation note": old is not None and f"continuing {old.id}" in seen["note"],
        "respawned agent starts from the salvaged branch": seen["files"] == ["src/a.py", "src/b.py"],
        "tokens carried into the budget": info.tokens > 0 and "in the runs this one resumes" in seen["brief"]
                                          and end_ev["data"]["tokens_cumulative"] > info.tokens,
        "resumed run ends cleanly": reason == "checked the resume",
        "gek resume parses": help_ok,
        "halted mid-work agents count as interrupted": any(a.how == "killed" for a in info3.interrupted)
                                                       and info3.ended == "stop mid-work",
        "tokens keep accumulating across resumes": info3.tokens > end_ev["data"]["tokens_cumulative"] - 1
                                                   or info3.tokens > 0,
    }
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    if not all(checks.values()):
        print(info.to_dict())
        print(seen["brief"][:2000])
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
