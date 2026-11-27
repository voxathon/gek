"""The swarm's config editor (c), headless: the same controls as the global one (G). Every
setting with where it comes from (swarm / global / default), edit, add, delete an override back
to the inherited value, and restart-only keys saved for the next run instead of refused.

    uv run python tests/tui_config_swarm.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="gek-tuiconf-"))
os.environ["GEK_HOME"] = str(HOME)

from textual.widgets import DataTable, Input, Static  # noqa: E402

from gek.cli import init_home_swarm  # noqa: E402
from gek.config import load_swarm, persist_key  # noqa: E402
from gek.tui import GekTUI  # noqa: E402

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")


def own(cfg) -> dict:
    return json.loads(cfg.path.read_text(encoding="utf-8"))


async def main() -> int:
    cfg, _ = await init_home_swarm("conf", "Configure me.", None)
    glob = HOME / "config.json"
    persist_key(glob, "models.default", "global-model")   # shared by every swarm
    g = json.loads(glob.read_text())                       # `gek init` wrote the defaults into the
    g["llm"].pop("timeout_s", None)                        # global file; drop one so it's default-only
    glob.write_text(json.dumps(g, indent=2))
    persist_key(cfg.path, "agents.max_alive", 2)          # this swarm's own override
    persist_key(cfg.path, "orchestrator.max_sleep_s", 99)  # and another, alone in its section
    cfg = load_swarm("conf")

    app = GekTUI(cfg)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.pause(1.0)
        await pilot.press("c")
        await pilot.pause(0.8)
        t = app.screen.query_one("#c-table", DataTable)
        keys = [str(k.value) for k in t.rows]

        def src(key: str) -> str:
            return str(t.get_row(key)[2])

        def goto(key: str) -> None:
            t.focus()
            t.move_cursor(row=[str(k.value) for k in t.rows].index(key))

        check("agents.max_alive" in keys and "models.default" in keys and "llm.timeout_s" in keys,
              "rows are every setting the swarm runs on (the merged view)")
        check(src("agents.max_alive") == "swarm" and src("models.default") == "global"
              and src("llm.timeout_s") == "default", "the from column says swarm / global / default")
        note = str(app.screen.query_one("#c-note", Static).render())
        check("a: add" in note and "x: delete" in note, "the same controls as G, listed")

        goto("models.default")
        await pilot.pause(0.3)
        detail = str(app.screen.query_one("#c-detail", Static).render())
        check("global file" in detail and "this swarm only" in detail, "detail pane: where the value comes from")

        # x on a row this file doesn't set: refused, nothing written
        before = own(cfg)
        await pilot.press("x")
        await pilot.pause(0.5)
        check(own(cfg) == before and json.loads(glob.read_text())["models"]["default"] == "global-model",
              "x on a global row writes nothing (that's G's file)")

        # x on the swarm's own override: back to the default
        goto("agents.max_alive")
        await pilot.pause(0.3)
        detail = str(app.screen.query_one("#c-detail", Static).render())
        check("x drops it and global" in detail, "detail says what x would fall back to")
        await pilot.press("x")
        await pilot.pause(0.8)
        check("max_alive" not in own(cfg).get("agents", {}), "x deletes the override from the swarm's config.json")
        check(src("agents.max_alive") == "global", "...and the row now shows the inherited value's layer")

        goto("orchestrator.max_sleep_s")
        await pilot.press("x")
        await pilot.pause(0.8)
        check("orchestrator" not in own(cfg), "deleting the last key of a section leaves no empty {} behind")

        # edit a global row: becomes this swarm's own override, global file untouched
        goto("models.default")
        await pilot.press("enter")
        await pilot.pause(0.4)
        inp = app.screen.query_one("#text", Input)
        inp.value = '"swarm-model"'
        await pilot.press("enter")
        await pilot.pause(2.0)
        check(own(cfg)["models"]["default"] == "swarm-model"
              and json.loads(glob.read_text())["models"]["default"] == "global-model",
              "editing a global row overrides it for this swarm only")
        check(src("models.default") == "swarm", "...and its row now says swarm")

        # a restart-only key: saved for the next run instead of refused
        goto("api.api_base_env")
        await pilot.press("enter")
        await pilot.pause(0.4)
        app.screen.query_one("#text", Input).value = "MY_BASE"
        await pilot.press("enter")
        await pilot.pause(1.0)
        check(own(cfg).get("api", {}).get("api_base_env") == "MY_BASE",
              "a restart key is saved to config.json (it used to be refused)")

        # a: add a key nothing sets yet
        await pilot.press("a")
        await pilot.pause(0.4)
        app.screen.query_one("#text", Input).value = "llm.extra_params.top_p"
        await pilot.press("enter")
        await pilot.pause(0.4)
        app.screen.query_one("#text", Input).value = "0.9"
        await pilot.press("enter")
        await pilot.pause(2.0)
        check(own(cfg).get("llm", {}).get("extra_params", {}).get("top_p") == 0.9, "a adds a new key")
        keys = [str(k.value) for k in t.rows]
        check("llm.extra_params.top_p" in keys and src("llm.extra_params.top_p") == "swarm",
              "...and it shows up as the swarm's own")

    passed = sum(ok for ok, _ in results)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
