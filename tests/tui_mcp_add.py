"""Adding an MCP server from the TUI, headless, the way a human does it: Shift+M opens the MCP
servers screen, `a` asks for the name of the new server, the edit form takes it, and Save sends
`mcp set NAME` through the control plane to swarm/inbox (`Control: mcp set NAME`).

The swarm is not started and no model is reachable: a run.lock naming a live pid (this process)
makes do_control take its "a swarm is running" branch, so the commit it makes is the real one,
on a throwaway GEK_HOME. Nothing outside the temp dir is read or written.

    uv run python tests/tui_mcp_add.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="gek-tuimcpadd-"))
os.environ["GEK_HOME"] = str(HOME)
os.environ.pop("GEK_API_KEY", None)
os.environ["GEK_API_BASE"] = "http://127.0.0.1:9"  # nothing listens: a model call would fail fast

from textual.widgets import Input  # noqa: E402

from gek.cli import init_home_swarm  # noqa: E402
from gek.config import load_swarm  # noqa: E402
from gek.tui import GekTUI  # noqa: E402

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")


def screen_text(app) -> str:
    return re.sub(r"<[^>]+>", "", app.export_screenshot()).replace("&#160;", " ")


async def type_into(pilot, text: str) -> None:
    for ch in text:
        await pilot.press({" ": "space", "-": "minus", ".": "full_stop"}.get(ch, ch))


async def wait(pred, timeout=15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.1)
    return False


def has_input(screen, ident: str) -> bool:
    try:
        screen.query_one(f"#{ident}", Input)
        return True
    except Exception:  # noqa: BLE001 - no such input on this screen
        return False


def inbox_message(cfg) -> str:
    """The message of the tip of swarm/inbox, or "" when the ref does not exist."""
    r = subprocess.run(["git", "--git-dir", str(cfg.repo_dir), "log", "-1", "--format=%B",
                        "swarm/inbox"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


async def flow(cfg) -> dict:
    r: dict = {}
    app = GekTUI(cfg)
    try:
        async with app.run_test(size=(170, 48)) as pilot:
            await pilot.pause(0.5)

            # Shift+M: the MCP servers screen
            await pilot.press("M")
            await pilot.pause(0.6)
            r["opened"] = has_input(app.screen, "text") is False and "MCP servers" in screen_text(app)

            # a: ask for the name of the new server (this is where main crashed)
            await pilot.press("a")
            await pilot.pause(0.8)
            r["no_crash"] = app._exception is None
            r["prompt"] = has_input(app.screen, "text")
            r["prompt_text"] = "Name for the new MCP server" in screen_text(app)
            if r["prompt"]:
                app.screen.query_one("#text", Input).value = ""
                await pilot.pause(0.2)
                await type_into(pilot, "calc")
                await pilot.press("enter")
                await pilot.pause(0.8)

            # the edit form for the new name, filled in and saved
            r["edit"] = has_input(app.screen, "cmd")
            r["edit_text"] = "mcp_servers.calc" in screen_text(app)
            if r["edit"]:
                cmd = app.screen.query_one("#cmd", Input)
                await type_into(pilot, "uvx")  # #cmd has focus on mount: typing lands there
                await pilot.pause(0.2)
                r["typed"] = cmd.value == "uvx"
                await pilot.click("#ok")
                r["committed"] = await wait(lambda: "Control: mcp set calc" in inbox_message(cfg), timeout=20)
            r["clean"] = app._exception is None
    except Exception as e:  # noqa: BLE001 - the app's panic comes back out of run_test
        r["raised"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    return r


async def main() -> int:
    cfg, _notes = await init_home_swarm("mcpadd", "Add an MCP server from the TUI.", None)
    # this process is the run: do_control's "a swarm is running" branch, i.e. a control commit
    (cfg.swarm_dir / "run.lock").write_text(json.dumps({"pid": os.getpid(), "run": "tuimcpadd"}))

    r = await flow(cfg)
    msg = inbox_message(cfg)
    if msg:
        print("\nswarm/inbox tip:\n" + msg)

    check(r.get("opened") is True, "M: the MCP servers screen opens")
    check(r.get("no_crash") is True,
          "a does not crash the app (no AttributeError on McpScreen)" if r.get("no_crash")
          else f"a does not crash the app (raised: {r.get('raised') or r.get('crash')})")
    check(r.get("prompt") is True, "a asks for the new server's name")
    check(r.get("prompt_text") is True, "the name prompt is the MCP one")
    check(r.get("edit") is True, "Enter on the name opens the edit form for it")
    check(r.get("edit_text") is True, "the form is for mcp_servers.calc")
    check(r.get("typed") is True, "typing lands in the command field")
    check(r.get("committed") is True, "Save sends `Control: mcp set calc` to swarm/inbox")
    check("\"command\": \"uvx\"" in msg or '"command":"uvx"' in msg, "...with the entry as its payload")
    check(r.get("clean") is True, "the whole add flow ran without an exception")
    check("AttributeError" not in (r.get("raised") or ""), "no AttributeError anywhere in the flow")

    bad = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    if bad:
        print("failed: " + json.dumps(bad))
    print(f"temp home: {HOME}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
