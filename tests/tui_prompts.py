"""The global config editor and the prompt editor, headless, the way a human uses them:

  G  opens the *global* settings screen ($GEK_HOME/config.json, the layer shared by every swarm),
     lists the keys that live in that file (and not the ones only this swarm's config.json has),
     and editing agents.max_alive there writes the shared file -- never the swarm's, and never as a
     `config set` control commit (the swarm's apply_config would persist it into its own file and
     shadow the global value forever);
  S  opens the prompt editor, and ctrl+s puts `Control: config set prompts.agents_extra` on
     swarm/inbox with the text that was typed, so a running swarm picks it up through the inbox.

The swarm is not started and no model is reachable: a run.lock naming a live pid (this process)
makes do_control take its "a swarm is running" branch, so the commit is the real control commit.
Nothing outside the temp GEK_HOME is read or written.

    uv run python tests/tui_prompts.py
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

HOME = Path(tempfile.mkdtemp(prefix="gek-tuiprompts-"))
os.environ["GEK_HOME"] = str(HOME)
os.environ.pop("GEK_API_KEY", None)
os.environ["GEK_API_BASE"] = "http://127.0.0.1:9"  # nothing listens: a model call would fail fast

from textual.widgets import Button, DataTable, Input, ListItem, ListView, TextArea  # noqa: E402

from gek.cli import init_home_swarm  # noqa: E402
from gek.config import load_swarm, persist_key  # noqa: E402
from gek.tui import GekTUI  # noqa: E402

GLOBAL = HOME / "config.json"  # the shared layer: $GEK_HOME/config.json
# a value that exists only in the shared file (DEFAULT_CONFIG ships the key, this value is ours)
GLOBAL_KEY, GLOBAL_VALUE = "models.aliases.smart", "probe-global-smart"
# a key that exists only in the swarm's own config.json -- write_home_defaults() drops "goal" from
# the shared file, so it is genuinely swarm-only and must not show up in a global editor
SWARM_ONLY_KEY = "goal"
MAX_ALIVE = "agents.max_alive"
PROMPT_KEY = "prompts.agents_extra"
TYPED = "zzed prompt note"  # no key a screen might bind on its own (no g, s, q, escape)

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


def first(screen, typ, ident: str | None = None):
    """The widget of that type on this screen, preferring the conventional id when it exists."""
    if ident:
        try:
            return screen.query_one(f"#{ident}", typ)
        except Exception:  # noqa: BLE001 - the builder used another id: fall back to the first one
            pass
    try:
        found = list(screen.query(typ))
    except Exception:  # noqa: BLE001 - no such widget on this screen
        return None
    return found[0] if found else None


def table_keys(t: DataTable) -> list[str]:
    """The dotted config key of every row: the row key if the screen set one, else column 0."""
    keys = [str(k.value) for k in t.rows]
    if any("." in k for k in keys):
        return keys
    try:
        return [str(t.get_row_at(i)[0]) for i in range(t.row_count)]
    except Exception:  # noqa: BLE001 - no readable first column
        return keys


def item_text(item) -> str:
    """A ListItem's visible text plus whatever name/id attributes the screen tagged it with.

    textual 8.x keeps a Label's text in ``.content`` (a Content, with ``.plain``); older releases
    used ``.renderable``/``.text``. All three are read, plus ``render()`` as a last resort, so the
    item is found whichever attribute this textual version uses.
    """
    parts = [str(getattr(item, "id", "") or ""), str(getattr(item, "name", "") or "")]
    for ch in getattr(item, "children", []):
        for attr in ("content", "renderable", "text"):
            try:
                v = getattr(ch, attr)
            except Exception:  # noqa: BLE001 - that attribute is not on this widget
                continue
            parts.append(str(getattr(v, "plain", v) or ""))
        try:
            rendered = ch.render()
        except Exception:  # noqa: BLE001 - no render(), nothing more to read
            rendered = None
        if rendered is not None:
            parts.append(str(getattr(rendered, "plain", rendered) or ""))
    for attr in ("prompt_key", "role_name", "key", "config_key", "value"):
        parts.append(str(getattr(item, attr, "") or ""))
    return " ".join(parts)


def inbox_log(cfg) -> str:
    """Every message on swarm/inbox, newest first; "" when the ref does not exist."""
    r = subprocess.run(["git", "--git-dir", str(cfg.repo_dir), "log", "--format=%B", "swarm/inbox"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def inbox_tip(cfg) -> str:
    r = subprocess.run(["git", "--git-dir", str(cfg.repo_dir), "log", "-1", "--format=%B",
                        "swarm/inbox"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def count_control(cfg, control: str) -> int:
    """How many commits on swarm/inbox carry exactly `Control: <control>` in their trailers."""
    want = f"Control: {control}"
    return sum(1 for line in inbox_log(cfg).splitlines() if line.strip() == want)


def json_at(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001 - missing or broken file: nothing was written
        return {}


async def edit_value(pilot, screen, value: str) -> bool:
    """Set the editor's value to `value` and save with it: typed into the field, saved by a press.

    The one-line PromptScreen has just an #text Input and saves on Enter (ConfigScreen's own
    pattern); a _Form has an #ok button. Both are driven by a key press or a click, as a human does.
    """
    inp = first(screen, Input, "text") or first(screen, Input)
    area = None if inp is not None else (first(screen, TextArea, "g-value") or first(screen, TextArea))
    ok_btn = first(screen, Button, "ok")
    if inp is not None:
        inp.focus()
        inp.value = value
        await pilot.pause(0.2)
        if ok_btn is not None:
            await pilot.click("#ok")
        else:
            await pilot.press("enter")
        return True
    if area is not None:
        area.focus()
        area.text = value
        await pilot.pause(0.2)
        if ok_btn is not None:
            await pilot.click("#ok")
        return True
    return False


async def select_entry(pilot, screen, name: str) -> bool:
    """Move the prompt list's cursor onto the entry naming `name`, the way a human would."""
    lv = first(screen, ListView, "p-list")
    if lv is not None:
        items = list(lv.children)
        idx = next((i for i, it in enumerate(items) if name in item_text(it)), None)
        if idx is None:
            return False
        lv.focus()
        await pilot.pause(0.2)
        lv.index = 0
        await pilot.pause(0.3)
        if idx == 0:  # force a Highlighted message so the screen loads the entry
            await pilot.press("down")
            await pilot.pause(0.2)
            await pilot.press("up")
            await pilot.pause(0.3)
        for _ in range(idx):
            await pilot.press("down")
            await pilot.pause(0.25)
        return True
    t = first(screen, DataTable, "p-table")
    if t is not None:
        keys = table_keys(t)
        idx = next((i for i, k in enumerate(keys) if name in k), None)
        if idx is None:
            return False
        t.focus()
        t.move_cursor(row=idx)
        await pilot.pause(0.3)
        return True
    return False


async def flow(cfg, swarm_before: bytes) -> dict:
    r: dict = {}
    app = GekTUI(cfg)
    try:
        async with app.run_test(size=(170, 48)) as pilot:
            await pilot.pause(1.0)

            # ---------------------------------------------------------- G: global settings
            main_screen = app.screen
            await pilot.press("G")
            await pilot.pause(0.8)
            r["g_screen"] = app.screen is not main_screen
            r["g_clean_1"] = app._exception is None
            txt = screen_text(app)
            r["g_text"] = r["g_screen"] and "global" in txt.lower()
            table = first(app.screen, DataTable, "g-table")
            r["g_table"] = table is not None and table.row_count > 0
            keys: list[str] = []
            rows: list[list[str]] = []
            if r["g_table"]:
                keys = table_keys(table)
                rows = [[str(c) for c in table.get_row(k)] for k in table.rows]
            r["g_keys"] = keys
            r["g_has_global_key"] = GLOBAL_KEY in keys
            r["g_global_value_shown"] = GLOBAL_VALUE in " ".join(" ".join(row) for row in rows)
            r["g_no_swarm_only_key"] = SWARM_ONLY_KEY not in keys and str(cfg.raw.get("goal")) not in txt
            r["g_has_max_alive"] = MAX_ALIVE in keys

            # ------------------------------------------- edit agents.max_alive -> 2, in the shared file
            if r["g_has_max_alive"]:
                table.focus()
                table.move_cursor(row=keys.index(MAX_ALIVE))
                await pilot.pause(0.3)
                await pilot.press("enter")
                await pilot.pause(0.6)
                edited = await edit_value(pilot, app.screen, "2")
                r["g_editor"] = edited
                if edited:
                    r["g_written"] = await wait(
                        lambda: (json_at(GLOBAL).get("agents") or {}).get("max_alive") == 2, timeout=15)
            r["g_clean_2"] = app._exception is None
            after_global = json_at(GLOBAL)
            r["g_kept_rest"] = after_global.get("models", {}).get("aliases", {}).get("smart") == GLOBAL_VALUE
            r["swarm_untouched"] = cfg.path.read_bytes() == swarm_before
            r["no_control_for_global"] = count_control(cfg, f"config set {MAX_ALIVE}") == 0
            await pilot.press("escape")
            await pilot.pause(0.5)
            r["g_closed"] = app.screen is main_screen

            # ---------------------------------------------------------- S: the prompt editor
            await pilot.press("S")
            await pilot.pause(0.8)
            r["s_screen"] = app.screen is not main_screen
            r["s_clean_1"] = app._exception is None
            ptxt = screen_text(app)
            area = first(app.screen, TextArea, "p-text") or first(app.screen, TextArea)
            r["s_list"] = await select_entry(pilot, app.screen, PROMPT_KEY)
            r["s_text_offered"] = PROMPT_KEY in ptxt or "agents_extra" in ptxt
            if area is not None:
                area.focus()
                await pilot.pause(0.3)
                await type_into(pilot, TYPED)
                await pilot.pause(0.3)
                r["s_typed"] = area.text.endswith(TYPED) or area.text == TYPED
                await pilot.press("ctrl+s")
                r["s_committed"] = await wait(
                    lambda: count_control(cfg, f"config set {PROMPT_KEY}") >= 1, timeout=20)
            r["s_clean_2"] = app._exception is None
            r["s_count"] = count_control(cfg, f"config set {PROMPT_KEY}")
            tip = inbox_tip(cfg)
            r["s_payload"] = TYPED in tip
    except Exception as e:  # noqa: BLE001 - the app's panic comes back out of run_test
        r["raised"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    return r


async def main() -> int:
    cfg, _notes = await init_home_swarm("prompts", "Edit global config and prompts from the TUI.", None)
    # the shared layer: a key whose value only exists in $GEK_HOME/config.json
    persist_key(GLOBAL, GLOBAL_KEY, GLOBAL_VALUE)
    cfg = load_swarm("prompts")
    # this process is the run: do_control's "a swarm is running" branch, i.e. a control commit
    (cfg.swarm_dir / "run.lock").write_text(json.dumps({"pid": os.getpid(), "run": "tuiprompts"}))

    swarm_file_before = cfg.path.read_bytes()
    print(f"global file: {GLOBAL}\nswarm file:  {cfg.path}")
    check(json_at(GLOBAL).get("models", {}).get("aliases", {}).get("smart") == GLOBAL_VALUE,
          f"setup: {GLOBAL_KEY} is seeded in the shared config.json only")
    check("agents" not in json_at(cfg.path), "setup: the swarm's own config.json has no agents block")
    check(SWARM_ONLY_KEY in json_at(cfg.path) and SWARM_ONLY_KEY not in json_at(GLOBAL),
          f"setup: {SWARM_ONLY_KEY} is in the swarm's config.json and not in the shared one")

    r = await flow(cfg, swarm_file_before)
    tip = inbox_tip(cfg)
    if tip:
        print("\nswarm/inbox tip:\n" + tip)

    check(r.get("g_screen") is True,
          "G opens a new screen" if r.get("g_screen")
          else f"G opens a new screen (raised: {r.get('raised')})")
    check(r.get("g_text") is True, "the screen it opens is the global settings screen")
    check(r.get("g_table") is True, "the global screen has a table of settings")
    check(r.get("g_has_global_key") is True,
          f"the table lists {GLOBAL_KEY}, which only the shared config.json has")
    check(r.get("g_global_value_shown") is True,
          f"...with the value seeded in the shared file ({GLOBAL_VALUE})")
    check(r.get("g_no_swarm_only_key") is True,
          f"it does not list {SWARM_ONLY_KEY}, which only the swarm's own config.json has")
    check(r.get("g_has_max_alive") is True, f"the table offers {MAX_ALIVE}")
    check(r.get("g_editor") is True, f"Enter on {MAX_ALIVE} opens an editor for its value")
    check(r.get("g_written") is True,
          f"saving 2 writes agents.max_alive = 2 into $GEK_HOME/config.json (got "
          f"{(json_at(GLOBAL).get('agents') or {}).get('max_alive')!r})")
    check(r.get("g_kept_rest") is True, "the shared file keeps the settings it already had")
    check(r.get("swarm_untouched") is True, "the swarm's own config.json is untouched")
    check(r.get("no_control_for_global") is True,
          f"a global edit is not sent to the swarm (no `Control: config set {MAX_ALIVE}` on swarm/inbox)")
    check(r.get("g_closed") is True, "Escape goes back to the swarm view")
    check(r.get("s_screen") is True, "S opens a new screen" if r.get("s_screen") else "S opens a new screen")
    check(r.get("s_text_offered") is True, f"the prompts screen offers {PROMPT_KEY}")
    check(r.get("s_list") is True, f"the prompt list can be moved onto {PROMPT_KEY}")
    check(r.get("s_typed") is True, "typing lands in the prompt's text box")
    check(r.get("s_committed") is True,
          f"ctrl+s puts `Control: config set {PROMPT_KEY}` on swarm/inbox")
    check(r.get("s_count") == 1, f"exactly one such commit (got {r.get('s_count')})")
    check(r.get("s_payload") is True, f"...with the typed text in its payload ({TYPED!r})")
    check(r.get("g_clean_1") is True and r.get("g_clean_2") is True
          and r.get("s_clean_1") is True and r.get("s_clean_2") is True,
          "app._exception stays None throughout")
    check(r.get("raised") is None, f"nothing was raised out of run_test ({r.get('raised')})")

    bad = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    if bad:
        print("failed: " + json.dumps(bad))
    print(f"temp home: {HOME}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
