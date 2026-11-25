"""The bwrap sandbox for agents' `run` commands, and the cross-platform fallbacks.

Without a working bwrap (Windows, macOS, a container, a minimal distro) only the fallback checks
run: one warning per run, commands still work. With bwrap, a real swarm (scripted fake model)
runs shell commands and we check what they could and couldn't touch.

    uv run python tests/sandbox.py
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

from gek import sandbox as sbmod
from gek.config import load_config, write_default_config
from gek.gitrepo import Repo
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
    msg = {"role": "assistant", "content": ""}
    if calls:
        msg["tool_calls"] = list(calls)
    return msg, {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}


def make_swarm_cfg(tmp: Path, **agents):
    tmp.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp / "config.json"
    write_default_config(cfg_path, "Test the sandbox.")
    raw = json.loads(cfg_path.read_text())
    raw["orchestrator"].update(min_sleep_s=0.5, debounce_s=0.2, inbox_poll_s=0.3)
    raw["agents"].update(agents)
    cfg_path.write_text(json.dumps(raw, indent=2))
    return load_config(cfg_path)


COMMANDS = [
    ("write_wt", "echo hi > inside.txt && cat inside.txt"),
    ("write_root", "touch /gek-sandbox-escape 2>&1; echo rc=$?"),
    ("read_config", "cat {config} 2>&1; echo rc=$?"),
    ("read_env", "cat {env} 2>&1; echo rc=$?"),
    ("read_ssh", "ls ~/.ssh 2>&1; cat ~/.ssh/id_test 2>&1; echo rc=$?"),
    ("other_wt", "ls {wt_root} 2>&1"),
    ("tmp_write", "echo x > /tmp/gek-sb && cat /tmp/gek-sb"),
    ("tmp_again", "cat /tmp/gek-sb 2>&1; echo rc=$?"),
    ("cache_write", "mkdir -p \"$UV_CACHE_DIR\" && echo cached > \"$UV_CACHE_DIR/x\" && echo ok"),
    ("cache_again", "cat \"$UV_CACHE_DIR/x\""),
    ("git_commit", "git add inside.txt && git commit -qm 'from the sandbox' && git log --format=%an%x09%s -1"),
    ("api_key", "echo key=[$GEK_API_KEY]"),
]


async def sandboxed_run(tmp: Path, home: Path) -> dict[str, str]:
    cfg = make_swarm_cfg(tmp / "swarm")
    (cfg.root / ".env").write_text("GEK_API_KEY=sk-super-secret\n")
    await Repo.init(cfg.repo_dir, cfg.raw["goal"], None, None)
    log = RunLog(cfg.logs_dir, echo=False)
    swarm = Swarm(cfg, log)
    out: dict[str, str] = {}
    fmt = {"config": cfg.path, "env": cfg.root / ".env", "wt_root": cfg.worktrees_dir}
    todo = list(COMMANDS)

    async def fake(model, messages, tools, actor):
        await asyncio.sleep(0.02)
        if actor == "orchestrator":
            if len(messages) == 2:
                return reply(call("write_role", name="sh", mandate="poke the sandbox", writes=["*"],
                                  powers={"shell": True}),
                             call("spawn", role="sh", note="go"),
                             call("spawn", role="sh", note="second agent, just so another worktree exists"),
                             call("sleep", seconds=600, reason="wait"))
            if len(out) == len(COMMANDS):
                return reply(call("halt", reason="done"))
            return reply(call("sleep", seconds=600, reason="wait"))
        last = messages[-1]
        if last.get("role") == "tool" and swarm.agents[actor].note == "go":
            name = getattr(swarm, "_sb_pending", None)
            if name:
                out[name] = last["content"]
        if swarm.agents[actor].note != "go":
            await asyncio.sleep(0.2)
            if len(out) == len(COMMANDS):
                return reply(call("finish", summary="ok"))
            return reply(call("read_log", refs=["swarm/chatter"], limit=1))
        if not todo:
            return reply(call("finish", summary="poked"))
        name, cmd = todo.pop(0)
        swarm._sb_pending = name
        return reply(call("run", command=cmd.format(**fmt)))

    swarm.llm.fake = fake
    await asyncio.wait_for(swarm.run(), timeout=120)
    log.close()
    ev = [json.loads(l) for l in open(log.dir / "events.jsonl")]
    out["_event"] = json.dumps(next(e for e in ev if e["kind"] == "sandbox")["data"])
    return out


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gek-sandbox-"))

    # --- fallbacks: these run everywhere ---------------------------------------------------
    cfg = make_swarm_cfg(tmp / "a")
    off = sbmod.detect(make_swarm_cfg(tmp / "off", sandbox="off"))
    check(off.kind == "none" and off.warning() is None, 'sandbox "off": no sandbox, no warning')
    with mock.patch.object(sys, "platform", "win32"):
        w = sbmod.detect(cfg)
    check(w.kind == "none" and "win32" in (w.warning() or ""), "Windows: unsandboxed, with a warning")
    with mock.patch.object(sys, "platform", "linux"), mock.patch("shutil.which", return_value=None):
        m = sbmod.detect(cfg)
    check(m.kind == "none" and "apt install bubblewrap" in (m.warning() or ""),
          "Linux without bwrap: warning says how to install it")
    try:
        with mock.patch.object(sys, "platform", "darwin"):
            sbmod.detect(make_swarm_cfg(tmp / "strict", sandbox="bwrap"))
        check(False, 'sandbox "bwrap" without bwrap refuses to start')
    except sbmod.SandboxError as e:
        check("bwrap" in str(e), 'sandbox "bwrap" without bwrap refuses to start')
    try:
        sbmod.detect(make_swarm_cfg(tmp / "typo", sandbox="bwarp"))
        check(False, "a typo in agents.sandbox is an error")
    except sbmod.SandboxError:
        check(True, "a typo in agents.sandbox is an error")
    check(not w.active and "not sandboxed" in w.describe(), "agents are told when they're not sandboxed")

    from gek.logs import feed_line
    fl = feed_line("sandbox", "harness", {"using": "none", "warning": w.warning(), "shell_allowed": True})
    check(bool(fl) and "WARNING" in fl, "the warning shows in the console feed")

    # --- the real thing ----------------------------------------------------------------------
    real = sbmod.detect(cfg)
    if not real.active:
        print(f"  SKIP  bwrap checks: {real.reason}")
    else:
        home = tmp / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh" / "id_test").write_text("PRIVATE KEY\n")
        with mock.patch.dict(os.environ, {"HOME": str(home), "GEK_API_KEY": "sk-super-secret"}):
            o = await sandboxed_run(tmp, home)
        g = lambda k: o.get(k, "<missing>")  # noqa: E731
        if os.environ.get("GEK_SB_DEBUG"):
            print(json.dumps(o, indent=1))
        check(json.loads(o["_event"]).get("using") == "bwrap", "run logs a sandbox event (bwrap)")
        check("hi" in g("write_wt"), "the agent can write its own worktree")
        check("rc=0" not in g("write_root") and not Path("/gek-sandbox-escape").exists(),
              "...but not the rest of the filesystem")
        check("rc=0" not in g("read_config") and "max_total_tokens" not in g("read_config"),
              "the swarm's config.json is hidden")
        check("sk-super-secret" not in g("read_env") and "rc=0" not in g("read_env"), "the .env is hidden")
        check("PRIVATE KEY" not in g("read_ssh"), "~/.ssh is hidden")
        check(g("other_wt").strip().count("sh-") <= 1, "other agents' worktrees are hidden")
        check("x" in g("tmp_write") and "rc=0" not in g("tmp_again"), "/tmp is fresh per command")
        check("ok" in g("cache_write") and "cached" in g("cache_again"), "the swarm cache persists between commands")
        check("from the sandbox" in g("git_commit") and "sh/" in g("git_commit"),
              "git commit works inside, as the agent")
        check("sk-super-secret" not in g("api_key"), "no API key in the sandboxed env")

    bad = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
