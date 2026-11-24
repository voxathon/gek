"""The TUI control plane, headless: new swarm form, roles editor, config table, detached run,
spawn form, pause, mode switch, halt, push form, narrow (phone) layout, command palette.
Runs real `gek run` processes in the background; no model is reachable, which is fine:
the harness keeps running and every control path is exercised.

    uv run python tests/tui_control.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="gek-tuictl-"))
os.environ["GEK_HOME"] = str(HOME)
os.environ.pop("GEK_API_KEY", None)
os.environ["GEK_API_BASE"] = "http://127.0.0.1:9"  # nothing listens: model calls fail fast

from textual.widgets import Checkbox, DataTable, Input, TextArea  # noqa: E402

from gek.config import load_swarm  # noqa: E402
from gek.tui import GekTUI  # noqa: E402

SHOTS = Path(os.environ.get("GEK_SHOTS", tempfile.gettempdir()))


async def type_into(pilot, text: str) -> None:
    for ch in text:
        await pilot.press({" ": "space", "/": "slash", ".": "full_stop", "-": "minus", ":": "colon",
                           "_": "underscore"}.get(ch, ch))


async def wait(pred, timeout=20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.2)
    return False


async def main() -> int:
    r: dict = {}
    app = GekTUI(None)
    async with app.run_test(size=(170, 48)) as pilot:
        await pilot.pause(0.5)
        # 1. new swarm form (don't start yet)
        await pilot.press("n")
        await pilot.pause(0.3)
        await type_into(pilot, "tuitest")
        app.screen.query_one("#goal", TextArea).text = "Exercise the control plane."
        app.screen.query_one("#start", Checkbox).value = False
        app.save_screenshot(str(SHOTS / "tui_new.svg"))
        await pilot.click("#ok")
        r["created"] = await wait(lambda: (HOME / "swarms/tuitest/repo.git").exists())
        await pilot.pause(1.0)
        cfg = load_swarm("tuitest")
        # fast settings for the test, written before the run starts
        for k, v in {"orchestrator.inbox_poll_s": 0.3, "orchestrator.debounce_s": 0.2,
                     "orchestrator.min_sleep_s": 0.5, "llm.num_retries": 0, "llm.timeout_s": 2,
                     "orchestrator.default_sleep_s": 600}.items():
            subprocess.run([sys.executable, "-m", "gek", "-s", "tuitest", "config", "set", k, json.dumps(v)],
                           check=True, capture_output=True)

        # 2. roles editor: write a role (offline, swarm not running yet)
        await pilot.press("o")
        await pilot.pause(0.8)
        await pilot.press("ctrl+n")
        await pilot.pause(0.3)
        app.screen.query_one("#r-json", TextArea).text = json.dumps(
            {"name": "watcher", "mandate": "watch the log", "writes": []}, indent=2)
        await pilot.pause(0.3)
        app.save_screenshot(str(SHOTS / "tui_roles.svg"))
        await pilot.press("ctrl+s")
        await pilot.pause(1.0)
        roles = subprocess.run(["git", "--git-dir", str(cfg.repo_dir), "ls-tree", "-r", "--name-only",
                                "swarm/roles"], capture_output=True, text=True).stdout
        r["role_offline"] = "roles/watcher.json" in roles
        # an invalid role is not saved
        app.screen.query_one("#r-json", TextArea).text = json.dumps(
            {"name": "bad", "mandate": "x", "powers": {"mcp": {"nope": ["*"]}}})
        await pilot.pause(0.3)
        r["invalid_flagged"] = "above the ceiling" in str(app.screen.query_one("#r-status").render())
        await pilot.press("escape")
        await pilot.pause(0.3)

        # 3. config table: change agents.max_alive offline
        await pilot.press("c")
        await pilot.pause(0.8)
        t = app.screen.query_one("#c-table", DataTable)
        keys = [str(k.value) for k in t.rows]
        t.move_cursor(row=keys.index("agents.max_alive"))
        await pilot.press("enter")
        await pilot.pause(0.3)
        inp = app.screen.query_one("#text", Input)
        inp.value = "2"
        app.save_screenshot(str(SHOTS / "tui_config.svg"))
        await pilot.press("enter")
        await pilot.pause(1.0)
        r["config_offline"] = json.loads(cfg.path.read_text())["agents"]["max_alive"] == 2
        await pilot.press("escape")
        await pilot.pause(0.3)

        # 4. start it in the background (R), then steer it
        await pilot.press("R")
        r["running"] = await wait(lambda: (cfg.swarm_dir / "run.lock").exists())
        await pilot.pause(2.5)
        await pilot.press("s")
        await pilot.pause(0.8)
        from textual.widgets import Select
        app.screen.query_one("#role", Select).value = "watcher"
        app.screen.query_one("#note", Input).value = "look around"
        await pilot.click("#ok")
        ev_file = lambda: next(iter(sorted(cfg.logs_dir.glob("*/events.jsonl"))), None)  # noqa: E731

        def events():
            f = ev_file()
            return [json.loads(l) for l in open(f)] if f else []

        r["spawned"] = await wait(lambda: any(e["kind"] == "agent_spawn" and e["actor"] == "human"
                                              for e in events()))
        await pilot.press("p")  # pause
        r["paused"] = await wait(lambda: any(e["kind"] == "pause" and e["data"]["paused"] for e in events()))
        await pilot.pause(1.0)
        await pilot.press("p")  # resume
        r["resumed"] = await wait(lambda: any(e["kind"] == "pause" and not e["data"]["paused"] for e in events()))
        await pilot.press("m")  # llm -> human
        r["mode_human"] = await wait(lambda: any(e["kind"] == "mode" and e["data"]["mode"] == "human"
                                                 for e in events()))
        await pilot.pause(1.5)
        app.save_screenshot(str(SHOTS / "tui_live.svg"))
        # live config while running
        await pilot.press("c")
        await pilot.pause(0.8)
        t = app.screen.query_one("#c-table", DataTable)
        keys = [str(k.value) for k in t.rows]
        t.move_cursor(row=keys.index("agents.max_alive"))
        await pilot.press("enter")
        await pilot.pause(0.3)
        app.screen.query_one("#text", Input).value = "3"
        await pilot.press("enter")
        r["config_live"] = await wait(lambda: any(e["kind"] == "config" and e["data"].get("key") == "agents.max_alive"
                                                  and e["data"].get("new") == 3 for e in events()))
        await pilot.press("escape")
        await pilot.pause(0.3)
        # halt
        await pilot.press("H")
        await pilot.pause(0.3)
        await pilot.press("enter")
        r["halted"] = await wait(lambda: not (cfg.swarm_dir / "run.lock").exists(), timeout=30)

        # 5. push form
        dest = HOME / "dest.git"
        subprocess.run(["git", "init", "-q", "--bare", str(dest)], check=True)
        await pilot.press("P")
        await pilot.pause(0.3)
        app.screen.query_one("#remote", Input).value = str(dest)
        await pilot.click("#ok")
        await pilot.pause(1.5)
        r["pushed"] = "gek/tuitest/main" in subprocess.run(["git", "--git-dir", str(dest), "branch"],
                                                           capture_output=True, text=True).stdout

        # 6. command palette lists the control plane
        await pilot.press("ctrl+p")
        await pilot.pause(0.5)
        await type_into(pilot, "spawn")
        await pilot.pause(0.8)
        app.save_screenshot(str(SHOTS / "tui_palette.svg"))
        import re as _re
        shot = _re.sub(r"<[^>]+>", "", app.export_screenshot()).replace("&#160;", " ")
        r["palette"] = "spawn" in shot.lower() and "into" in shot and "role" in shot
        if not r["palette"]:
            (SHOTS / "palette_text.txt").write_text(shot)
        await pilot.press("escape")
        await pilot.pause(0.3)

    # 7. phone layout
    app2 = GekTUI(load_swarm("tuitest"))
    async with app2.run_test(size=(60, 50)) as pilot:
        await pilot.pause(1.2)
        app2.save_screenshot(str(SHOTS / "tui_phone.svg"))
        r["narrow"] = app2.screen.has_class("-narrow") or app2.has_class("-narrow")
        r["side_hidden"] = not app2.query_one("#side").display

    checks = {
        "new swarm form creates it": r.get("created"),
        "roles editor writes a role (offline)": r.get("role_offline"),
        "roles editor flags invalid roles": r.get("invalid_flagged"),
        "config table saves (offline)": r.get("config_offline"),
        "R starts a detached run": r.get("running"),
        "spawn form spawns (as human)": r.get("spawned"),
        "p pauses": r.get("paused"),
        "p resumes": r.get("resumed"),
        "m switches to human mode": r.get("mode_human"),
        "config table applies live": r.get("config_live"),
        "H halts, the run ends": r.get("halted"),
        "push form pushes": r.get("pushed"),
        "palette lists control actions": r.get("palette"),
        "narrow screens get the phone layout": r.get("narrow") and r.get("side_hidden"),
    }
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    print(f"screenshots in {SHOTS}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
