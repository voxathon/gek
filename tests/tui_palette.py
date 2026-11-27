"""The command palette (ctrl+p), headless: swarms don't flood it. There is one "Switch swarm"
entry that opens a second search over swarm names, with each swarm's state and goal.

    uv run python tests/tui_palette.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="gek-tuipal-"))
os.environ["GEK_HOME"] = str(HOME)

from gek.cli import init_home_swarm  # noqa: E402
from gek.config import load_swarm  # noqa: E402
from gek.tui import GekTUI  # noqa: E402

NAMES = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")


def screen_text(app) -> str:
    return re.sub(r"<[^>]+>", "", app.export_screenshot()).replace("&#160;", " ")


async def type_into(pilot, text: str) -> None:
    for ch in text:
        await pilot.press({" ": "space"}.get(ch, ch))


async def main() -> int:
    for n in NAMES:
        await init_home_swarm(n, f"goal of {n}", None)

    app = GekTUI(load_swarm("alpha"))
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.pause(1.0)
        cmds = [c.title for c in app.get_system_commands(app.screen)]
        check(not any(t.startswith("Swarm:") for t in cmds), "no per-swarm entries in the palette")
        check("Switch swarm" in cmds, "one Switch swarm entry instead")
        check(len(cmds) < 30, f"the palette's command count doesn't grow with swarms ({len(cmds)})")

        await pilot.press("ctrl+p")
        await pilot.pause(0.5)
        await type_into(pilot, "switch swarm")
        await pilot.pause(0.8)
        await pilot.press("enter")
        await pilot.pause(1.0)
        txt = screen_text(app)
        check("Switch to swarm" in txt, "it opens a second search, just for swarms")
        check(all(n in txt for n in NAMES[:4]) and "goal of charlie" in txt,
              "...listing swarms with their state and goal")
        check("current, idle" in txt, "...marking the one you're on")
        await type_into(pilot, "delta")
        await pilot.pause(0.8)
        await pilot.press("enter")
        await pilot.pause(1.0)
        check(app.cfg is not None and app.cfg.name == "delta", "picking one switches to it")

    passed = sum(ok for ok, _ in results)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
