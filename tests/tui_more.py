"""The TUI's views of 0.4-0.7 features, headless: sandbox badge, plugin status, the context
column, compaction markers and dividers, per-agent tools, the plugins screen (enable, edit),
and the resume review dialog for a crashed run. A scripted run (fake model) makes the logs.

    uv run python tests/tui_more.py
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="gek-tuimore-"))
os.environ["GEK_HOME"] = str(HOME)

from textual.widgets import DataTable, RichLog, TextArea  # noqa: E402

from gek.cli import init_home_swarm  # noqa: E402
from gek.config import load_swarm, persist_key  # noqa: E402
from gek.gitrepo import Repo  # noqa: E402
from gek.logs import RunLog  # noqa: E402
from gek.swarm import Swarm  # noqa: E402
from gek.tui import GekTUI  # noqa: E402

SHOTS = Path(os.environ.get("GEK_SHOTS", tempfile.gettempdir()))
_ids = itertools.count()
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")


def call(_tool: str, **args) -> dict:
    return {"id": f"c{next(_ids)}", "type": "function",
            "function": {"name": _tool, "arguments": json.dumps(args)}}


def screen_text(app) -> str:
    return re.sub(r"<[^>]+>", "", app.export_screenshot()).replace("&#160;", " ")


def log_text(log: RichLog) -> str:
    return "\n".join("".join(seg.text for seg in strip) for strip in log.lines)


PROBE = '''
from gek.plugin import Plugin, PluginTool
class Probe(Plugin):
    async def on_event(self, ev):
        if ev["kind"] == "agent_spawn":
            self.ctx.log(saw=ev["data"]["id"])
    def tools(self):
        async def ping(caller):
            return "pong"
        return [PluginTool("ping", "Ping.", {"type": "object", "properties": {}}, ping)]
gek_plugin = Probe
'''
BOOM = '''
from gek.plugin import Plugin
class Boom(Plugin):
    async def on_event(self, ev):
        raise RuntimeError("kaboom")
gek_plugin = Boom
'''


def size(messages) -> int:
    return sum(len(json.dumps(m)) for m in messages) // 4


async def make_runs():
    cfg, _ = await init_home_swarm("more", "Show off the new TUI bits.", None)
    root = cfg.root
    (root / "plugins").mkdir(exist_ok=True)
    (root / "plugins" / "probe.py").write_text(PROBE)
    (root / "plugins" / "boom.py").write_text(BOOM)
    for k, v in {"orchestrator.min_sleep_s": 0.5, "orchestrator.debounce_s": 0.2,
                 "orchestrator.inbox_poll_s": 0.3, "agents.compact_at_tokens": 3000,
                 "agents.compact_keep_recent": 6, "plugins": {"probe": {}, "boom": {}},
                 "ceiling.mcp": {"probe": ["*"]}}.items():
        persist_key(cfg.path, k, v)
    cfg = load_swarm("more")
    if not (cfg.repo_dir / "HEAD").exists():
        await Repo.init(cfg.repo_dir, cfg.raw["goal"], None, None)

    # run 1: a grinder that gets compacted, with plugin tools and a shell
    log = RunLog(cfg.logs_dir, echo=False)
    swarm = Swarm(cfg, log)
    st = {"spawned": False, "n": 0}

    async def fake(model, messages, tools, actor):
        await asyncio.sleep(0.01)
        usage = {"prompt_tokens": size(messages), "completion_tokens": 40}
        usage["total_tokens"] = usage["prompt_tokens"] + 40
        if actor == "orchestrator":
            if not st["spawned"]:
                st["spawned"] = True
                return {"role": "assistant", "content": "", "tool_calls": [
                    call("write_role", name="grinder", mandate="grind", writes=["*"],
                         powers={"shell": True, "mcp": {"probe": ["ping"]}}),
                    call("spawn", role="grinder", note="grind"),
                    call("sleep", seconds=600, reason="wait")]}, usage
            alive = [a for a in swarm.agents.values() if a.status in ("starting", "running")]
            return {"role": "assistant", "content": "", "tool_calls": [
                call("halt", reason="done") if not alive else call("sleep", seconds=600, reason="w")]}, usage
        if tools is None:
            return {"role": "assistant", "content": "NOTES: wrote g1..g8. Next: finish up."}, usage
        st["n"] += 1
        if st["n"] == 1:
            return {"role": "assistant", "content": "", "tool_calls": [call("probe__ping")]}, usage
        if st["n"] <= 12:
            return {"role": "assistant", "content": "", "tool_calls": [
                call("write_file", path=f"g{st['n']}.txt", content="y" * 300),
                call("read_file", path=f"g{st['n']}.txt")]}, usage
        return {"role": "assistant", "content": "", "tool_calls": [call("finish", summary="ground")]}, usage

    swarm.llm.fake = fake
    await asyncio.wait_for(swarm.run(), timeout=90)
    log.close()
    gid = next(a for a in swarm.agents if a.startswith("grinder"))

    # run 2: "crashed": an agent spawned, no exit, no run_end, and a dirty worktree left behind
    wt = cfg.worktrees_dir / "fixer-0001"
    await Repo(cfg.repo_dir).add_worktree(wt, "role/fixer/0001", base="main")
    (wt / "half_done.py").write_text("x = 1\n")
    log2 = RunLog(cfg.logs_dir, echo=False)
    log2.event("run_start", "harness", goal=cfg.raw["goal"])
    log2.event("agent_spawn", "orchestrator", id="fixer/0001", role="fixer", branch="role/fixer/0001")
    log2.event("orch_sleep", "orchestrator", seconds=300, reason="fixer is on the flaky test")
    log2.close()
    return cfg, gid


async def main() -> int:
    cfg, gid = await make_runs()
    import gek.cli as cli

    started: list = []
    cli.start_detached = lambda c, goal=None, extra=None: started.append(extra) or 4242

    app = GekTUI(cfg)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.pause(1.0)
        txt = screen_text(app)
        check("U: review and resume" in txt and "no run_end" in txt, "a crashed latest run offers U: resume")

        # the resume review
        await pilot.press("U")
        await pilot.pause(0.8)
        txt = screen_text(app)
        app.save_screenshot(str(SHOTS / "tui_resume.svg"))
        check("CRASHED" in txt and "fixer/0001" in txt, "resume review: how it ended, who was mid-work")
        check("fixer-0001: 1 files" in txt, "resume review: uncommitted work that will be salvaged")
        check("fixer is on the flaky test" in txt, "resume review: the orchestrator's last note")
        await pilot.click("#ok")
        await pilot.pause(0.5)
        check(started and started[-1] == ["--resume", "--respawn"], "Resume starts gek run --resume --respawn")

        # the older, real run
        await pilot.press("[")
        await pilot.pause(1.0)
        txt = screen_text(app)
        app.save_screenshot(str(SHOTS / "tui_main_more.svg"))
        sb = app.cur.sandbox if app.cur else {}
        if sb.get("using") == "bwrap":
            check("sandbox: bwrap" in txt, "header: sandbox badge")
        else:
            check("NOT SANDBOXED" in txt, "header: NOT SANDBOXED badge")
        check("plugins 2" in txt and "1 failed" in txt, "header: plugin count, with failures")
        check("cyan | compaction" in txt, "token graph marks compactions")
        t = app.query_one("#agents", DataTable)
        row = t.get_row(gid)
        ctx = str(row[6])
        check("/3k" in ctx and "x" in ctx, f"agents table: context vs threshold, compaction count ({ctx})")
        check(len(app.cur.compaction_list) >= 1, "compactions are parsed from the log")
        check("MCP probe failed" not in txt, "a plugin grant isn't mistaken for an MCP server")

        # the transcript
        t.focus()
        t.move_cursor(row=t.get_row_index(gid))
        await pilot.press("enter")
        await pilot.pause(1.0)
        log = app.screen.query_one("#t-log", RichLog)
        body = log_text(log)
        head = screen_text(app)
        check("compacted at step" in body and "notes the agent wrote" in body and "NOTES: wrote g1..g8" in body,
              "transcript: compaction divider with the agent's notes")
        check(re.search(r"compacted at step \d+: [\d,]+ -> ~[\d,]+ tokens", body) is not None,
              "transcript: divider shows before -> after")
        check("tools" in head and "probe: probe__ping" in head and "gek:" in head and "core" in head,
              "transcript: tools by source")
        await pilot.press("t")
        await pilot.pause(0.3)
        head = screen_text(app)
        app.save_screenshot(str(SHOTS / "tui_transcript_more.svg"))
        check("read_log" in head and "write_file" in head, "transcript: t shows core tool names")
        await pilot.press("escape")
        await pilot.pause(0.5)

        # the plugins screen
        await pilot.press("L")
        await pilot.pause(0.8)
        pt = app.screen.query_one("#pl-table", DataTable)
        rows = {str(k.value): [str(c) for c in pt.get_row(k)] for k in pt.rows}
        txt = screen_text(app)
        app.save_screenshot(str(SHOTS / "tui_plugins.svg"))
        check(rows.get("probe", [""])[3] == "loaded" and rows["probe"][4] == "ping" and rows["probe"][5] != "-",
              "plugins: loaded, its tools and hash")
        check(rows.get("boom", ["", "", "", ""])[3] == "disabled", "plugins: the crashing one shows disabled")
        check(rows.get("notify", ["", ""])[1] == "not configured" and rows["notify"][2] == "bundled",
              "plugins: bundled notify shows, not configured")
        check("saw=" in rows.get("probe", [""] * 7)[6], "plugins: last action")
        pt.move_cursor(row=list(rows).index("notify"))
        await pilot.press("space")
        await pilot.pause(0.5)
        check("notify" in (load_swarm("more").raw.get("plugins") or {}), "space enables a plugin in config.json")
        await pilot.press("e")
        await pilot.pause(0.5)
        app.screen.query_one("#p-json", TextArea).text = json.dumps({"url": "https://ntfy.sh/secret-topic"})
        await pilot.click("#ok")
        await pilot.pause(0.5)
        check(load_swarm("more").raw["plugins"]["notify"] == {"url": "https://ntfy.sh/secret-topic"},
              "e edits its config")
        pt = app.screen.query_one("#pl-table", DataTable)
        pt.move_cursor(row=[str(k.value) for k in pt.rows].index("boom"))
        await pilot.press("space")
        await pilot.pause(0.5)
        check(load_swarm("more").raw["plugins"]["boom"] == {"enabled": False}, "space again disables")
        txt = screen_text(app)
        check("disabled" in txt, "...and the table shows it")

    r = subprocess.run([sys.executable, "-m", "gek", "-s", "more", "plugins"], capture_output=True, text=True)
    check("disabled  boom" in r.stdout and "enabled   notify" in r.stdout, "gek plugins agrees")

    bad = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
