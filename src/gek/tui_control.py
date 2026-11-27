"""Control-plane screens for `gek tui`: new swarm, spawn, push, roles editor, the swarm's config
editor (c), the global config editor (G) and the prompt editor.

Everything goes through the same paths as the CLI (cli.do_control, init_home_swarm,
push_swarm, start_detached), so the TUI can't do anything the protocol doesn't record. The
exceptions write config files directly: the global config editor (G) writes
gek_home()/config.json, because routing a shared setting through the control plane would persist
it into the swarm's own file and shadow the global value forever; and the swarm's config editor
(c) writes restart-only keys and deletes overrides in the swarm's config.json, which the control
plane has no verb for (a running swarm sees the file change like a hand edit).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (Button, Checkbox, DataTable, Footer, Header, Input, Label, ListItem,
                             ListView, Select, Static, TextArea)

from .config import Config, flatten, is_hot, load_config, load_swarm

WHO = "human"

FORM_CSS = """
#form { width: 96; max-width: 100%; height: auto; border: round $accent; padding: 1 2; background: $surface; }
#form Label { margin-top: 1; }
#form .err { color: $error; margin-top: 1; }
#form #buttons { height: auto; margin-top: 1; }
#form Button { margin-right: 2; }
"""


def _reload(cfg: Config) -> Config:
    return load_config(cfg.path, name=cfg.name, portable=cfg.portable) if cfg.portable else load_swarm(cfg.name)


class _Form(ModalScreen):
    """A modal form. Subclasses build fields and implement submit(); errors show in red."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]
    CSS = "_Form { align: center middle; }" + FORM_CSS

    def _err(self, msg: str) -> None:
        self.query_one("#err", Label).update(Text(msg, style="bold red"))

    def action_cancel(self) -> None:
        self.dismiss(None)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        # The one handler for the whole form. textual calls *every* on_button_pressed in the
        # class MRO, so a form that also defined its own would run the press twice: on Save that
        # meant two dismisses and two `mcp set` commits per click. Forms customise submit() (and
        # what "del" dismisses with) instead of the handler.
        event.stop()  # the press is ours: don't let parent screens act on it too
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "del":
            self.dismiss("delete")
        elif event.button.id == "ok":
            try:
                await self.submit()
            except Exception as e:  # UserError, GitError, JSON errors: show, don't crash
                self._err(str(e))

    async def submit(self) -> None:
        raise NotImplementedError


class NewSwarmScreen(_Form):
    def compose(self) -> ComposeResult:
        with Vertical(id="form"):
            yield Label("[b]New swarm[/b]  (created under your gek home, becomes current)")
            yield Label("Name (letters, digits, - _ .)")
            yield Input(id="name", placeholder="todo3")
            yield Label("Goal (what the swarm should do, and when it's done)")
            yield TextArea(id="goal")
            yield Label("Seed: a git URL or path to start from (blank = empty repo)")
            yield Input(id="seed", placeholder="https://github.com/you/repo")
            yield Checkbox("start it now (in the background)", value=True, id="start")
            yield Label("", id="err", classes="err")
            with Horizontal(id="buttons"):
                yield Button("Create", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#goal", TextArea).styles.height = 6
        self.query_one("#name", Input).focus()

    async def submit(self) -> None:
        from .cli import _seed_source, init_home_swarm, start_detached

        name = self.query_one("#name", Input).value.strip()
        goal = self.query_one("#goal", TextArea).text.strip()
        seed = self.query_one("#seed", Input).value.strip()
        if not name or not goal:
            self._err("name and goal are both needed")
            return
        self._err("creating (cloning the seed can take a moment)...")
        cfg, _ = await init_home_swarm(name, goal, _seed_source(seed) if seed else None)
        started = ""
        if self.query_one("#start", Checkbox).value:
            started = f"started (pid {start_detached(cfg)})"
        self.dismiss((cfg, started))


class SpawnScreen(_Form):
    def __init__(self, cfg: Config, roles: list[str]):
        super().__init__()
        self.cfg, self.roles = cfg, roles

    def compose(self) -> ComposeResult:
        with Vertical(id="form"):
            yield Label(f"[b]Spawn an agent[/b] into {self.cfg.name}")
            yield Select([(r, r) for r in self.roles], prompt="role", id="role")
            yield Label("Note for the agent (what to focus on)")
            yield Input(id="note")
            yield Label("Start from branch (optional, e.g. a dead agent's role/coder/ab12)")
            yield Input(id="from")
            yield Label("", id="err", classes="err")
            with Horizontal(id="buttons"):
                yield Button("Spawn", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    async def submit(self) -> None:
        from .cli import do_control

        role = self.query_one("#role", Select).value
        if role is Select.BLANK or not role:
            self._err("pick a role")
            return
        frm = self.query_one("#from", Input).value.strip()
        control = f"spawn {role}" + (f" from={frm}" if frm else "")
        self.dismiss(await do_control(self.cfg, WHO, control, "", self.query_one("#note", Input).value))


class PushScreen(_Form):
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg

    def compose(self) -> ComposeResult:
        with Vertical(id="form"):
            yield Label(f"[b]Push[/b] {self.cfg.name}'s work to a git remote")
            yield Label("Remote: a git URL or path (e.g. /mnt/shared/gek)")
            yield Input(id="remote")
            yield Label("Branch (default main)")
            yield Input(value="main", id="branch")
            yield Label("Destination prefix (blank = gek/<swarm>)")
            yield Input(id="to")
            yield Checkbox("push every branch (main, role/*, swarm/*)", id="all")
            yield Label("", id="err", classes="err")
            with Horizontal(id="buttons"):
                yield Button("Push", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    async def submit(self) -> None:
        from .cli import push_swarm

        remote = self.query_one("#remote", Input).value.strip()
        if not remote:
            self._err("where to?")
            return
        self.dismiss(await push_swarm(self.cfg, remote, self.query_one("#branch", Input).value.strip() or "main",
                                      self.query_one("#to", Input).value.strip() or None,
                                      self.query_one("#all", Checkbox).value))


class RolesScreen(Screen):
    """Role templates: pick one, edit its JSON, see validation against the ceiling live, save."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "back"),
        Binding("ctrl+s", "save", "save"),
        Binding("ctrl+r", "retire", "retire"),
        Binding("ctrl+n", "new", "new role"),
    ]
    CSS = """
    #r-list { width: 28; border-right: solid $primary-darken-2; }
    #r-edit { width: 1fr; }
    #r-status { height: auto; padding: 0 1; }
    .-narrow #r-list { width: 16; }
    """

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = _reload(cfg)
        self.current: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield ListView(id="r-list")
            with Vertical(id="r-edit"):
                yield TextArea(id="r-json")
                yield Static(id="r-status")
        yield Footer()

    async def on_mount(self) -> None:
        await self.refresh_list()

    async def refresh_list(self, select: str | None = None) -> None:
        from pathlib import Path

        from .gitrepo import ROLES, Repo

        lv = self.query_one("#r-list", ListView)
        await lv.clear()
        names = [Path(p).stem for p in await Repo(self.cfg.repo_dir).list_files_at(ROLES, "roles")]
        for n in names:
            item = ListItem(Label(n))
            item.role_name = n  # type: ignore[attr-defined]
            await lv.append(item)
        if names:
            idx = names.index(select) if select in names else 0
            lv.index = idx
            await self.load(names[idx])
        else:
            await self.action_new()

    async def load(self, name: str) -> None:
        from .gitrepo import ROLES, Repo

        text = await Repo(self.cfg.repo_dir).read_file_at(ROLES, f"roles/{name}.json") or "{}"
        self.current = name
        self.query_one("#r-json", TextArea).text = text
        self.validate_text()

    async def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        name = getattr(event.item, "role_name", None)
        if name and name != self.current:
            await self.load(name)

    def validate_text(self) -> dict | None:
        from .roles import Role, validate

        status = self.query_one("#r-status", Static)
        try:
            data = json.loads(self.query_one("#r-json", TextArea).text)
            errs = validate(Role.from_dict(data), self.cfg.raw.get("ceiling", {}),
                            self.cfg.tool_sources())
        except Exception as e:
            status.update(Text(f"not valid JSON: {e}", style="red"))
            return None
        if errs:
            status.update(Text("above the ceiling or invalid:\n- " + "\n- ".join(errs), style="red"))
            return None
        status.update(Text("valid. ctrl+s saves" + (" (sent to the running swarm)" if self._swarm_live()
                                                     else " (written for the next run)"), style="green"))
        return data

    def _swarm_live(self) -> bool:
        from .cli import is_running

        return is_running(self.cfg)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self.validate_text()

    async def action_save(self) -> None:
        from .cli import do_control

        data = self.validate_text()
        if data is None:
            self.notify("fix the errors first", severity="error")
            return
        try:
            msg = await do_control(self.cfg, WHO, "role set", f"set {data.get('name')}",
                                   json.dumps(data, indent=2))
            self.notify(msg)
            if not self._swarm_live():
                await self.refresh_list(select=data.get("name"))
        except Exception as e:
            self.notify(str(e), severity="error")

    async def action_retire(self) -> None:
        from .cli import do_control

        if not self.current:
            return
        try:
            self.notify(await do_control(self.cfg, WHO, f"role retire {self.current}", "retired from the TUI"))
            if not self._swarm_live():
                await self.refresh_list()
        except Exception as e:
            self.notify(str(e), severity="error")

    async def action_new(self) -> None:
        from .roles import Role

        self.current = None
        self.query_one("#r-json", TextArea).text = Role(
            name="new-role", mandate="what this role is for", writes=["src/"]).to_json()
        self.validate_text()


class ConfigScreen(Screen):
    """This swarm's settings, with the same controls as the global editor (G): edit, add, delete.

    Rows are the *merged* view, so every setting the swarm runs on is visible, and the "from"
    column says which layer each value comes from: this swarm's config.json, the global file, or
    the built-in default. Writes always land in this swarm's own file:
      - a live key goes through cli.do_control, exactly as before (a control commit while the swarm
        runs, a config.json write when it doesn't);
      - a restart key (api.*, swarm_dir) is written to config.json directly and applies next run;
      - x deletes the swarm's own override, so the global or default value applies again. Only
        rows "from swarm" can be deleted: the others aren't in this file. A running swarm sees the
        file change and applies the inherited value like any hand edit of config.json.
    """

    BINDINGS = [
        Binding("escape", "app.pop_screen", "back"),
        Binding("enter", "edit", "edit"),
        Binding("e", "edit", "edit", show=False),
        Binding("a", "new", "add"),
        Binding("x", "remove", "remove"),
        Binding("r", "reload", "reload"),
    ]
    CSS = """
    #c-note { height: auto; padding: 0 1; }
    #c-detail { height: auto; max-height: 7; padding: 0 1; background: $boost; }
    """

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.merged: dict[str, Any] = {}
        self.own: dict[str, Any] = {}      # leaves this swarm's config.json sets
        self.shared: dict[str, Any] = {}   # leaves the global file sets (none for a portable swarm)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="c-note")
        yield DataTable(id="c-table", cursor_type="row", zebra_stripes=True)
        yield Static(id="c-detail")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#c-table", DataTable)
        t.add_columns("setting", "value", "from", "applies")
        self.refresh_table()

    def _source(self, key: str) -> str:
        if key in self.own:
            return "swarm"
        if key in self.shared:
            return "global"
        return "default"

    def refresh_table(self) -> None:
        from .cli import is_running

        broken = ""
        try:
            self.cfg = _reload(self.cfg)
            self.own = flatten(_read_obj(self.cfg.path))
            self.shared = {} if self.cfg.portable else flatten(_load_global())
        except (OSError, ValueError) as e:
            broken = f"can't read the config: {e}"
        self.merged = flatten(self.cfg.raw)
        t = self.query_one("#c-table", DataTable)
        keep = t.cursor_row
        t.clear()
        for k, v in sorted(self.merged.items()):
            src = self._source(k)
            src_t = Text(src, style={"swarm": "bold", "global": "cyan", "default": "dim"}[src])
            tag = Text("live", style="green") if is_hot(k) else Text("restart", style="dim")
            val = json.dumps(v)
            t.add_row(k, val if len(val) <= 70 else val[:67] + "...", src_t, tag, key=k)
        if t.row_count:
            t.move_cursor(row=min(keep, t.row_count - 1))
        state = "running, live changes apply now" if is_running(self.cfg) else "not running, changes apply next run"
        note = Text.assemble(
            (f"{self.cfg.name}", "bold"), f": {state}  |  ", (str(self.cfg.path), "bold"),
            f"  |  {len(self.own)} set here",
            ("" if self.cfg.portable else f", {len(self.shared)} from the global file (G)"),
            "  |  enter/e: edit, a: add, x: delete (back to global/default), r: reload")
        if broken:
            note.append("\n" + broken, style="red")
        self.query_one("#c-note", Static).update(note)
        self._detail()

    def _selected(self) -> str | None:
        t = self.query_one("#c-table", DataTable)
        if not t.row_count:
            return None
        return str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)

    def _inherited(self, key: str) -> tuple[str, Any]:
        """What this key would be without the swarm's own override."""
        if key in self.shared:
            return "global", self.shared[key]
        from .config import DEFAULT_CONFIG

        d = flatten(DEFAULT_CONFIG)
        return ("default", d[key]) if key in d else ("nothing", None)

    def _detail(self) -> None:
        box = self.query_one("#c-detail", Static)
        key = self._selected()
        if not key:
            box.update(Text("no settings. a adds one to this swarm's config.json.", style="dim"))
            return
        out = Text()
        out.append(key, style="bold")
        out.append(f" = {json.dumps(self.merged.get(key))}\n")
        if is_hot(key):
            out.append("live: no restart needed; a running swarm applies it at once.\n", style="green")
        else:
            out.append("restart: read once at start-up; saved to config.json, applies next run.\n",
                       style="yellow")
        src = self._source(key)
        if src == "swarm":
            where, val = self._inherited(key)
            back = (f"x drops it and {where} {json.dumps(val)[:50]} applies again" if where != "nothing"
                    else "x drops it (nothing else sets this key)")
            out.append(f"set in this swarm's config.json; {back}.", style="dim")
        elif src == "global":
            out.append("from the global file, shared by every swarm (G edits it there). Editing here "
                       "overrides it for this swarm only.", style="dim")
        else:
            out.append("the built-in default. Editing here sets it for this swarm only; G sets it "
                       "for every swarm.", style="dim")
        box.update(out)

    async def _set(self, key: str, value: Any) -> str:
        """Live keys through the control plane (as before); restart keys straight into config.json."""
        from .cli import do_control
        from .config import persist_key

        if is_hot(key):
            return await do_control(self.cfg, WHO, f"config set {key}", "set from the TUI", json.dumps(value))
        persist_key(self.cfg.path, key, value)
        return f"{key} = {json.dumps(value)[:60]} saved; it's a restart setting, so it applies next run"

    async def _apply(self, key: str, value: Any) -> None:
        try:
            self.notify(await self._set(key, value))
        except Exception as e:  # refused live key, unwritable file: show, don't crash
            self.notify(str(e), severity="error")
        self.set_timer(1.5 if is_hot(key) else 0.1, self.refresh_table)

    def action_edit(self) -> None:
        from .tui import PromptScreen

        key = self._selected()
        if not key:
            return
        current = json.dumps(self.merged.get(key))
        tag = "live" if is_hot(key) else "restart: applies next run"
        warn = "  CEILING: this changes what the swarm may grant." if key.startswith("ceiling") else ""
        title = f"{key} ({tag}) (JSON: 3, true, \"text\", [..], {{..}}).{warn} Enter saves, Esc cancels:"

        async def done(value: str | None) -> None:
            from .config import parse_value

            if value is None or value.strip() == current:
                return
            await self._apply(key, parse_value(value.strip()))

        self.app.push_screen(PromptScreen(title, value=current), done)

    def action_new(self) -> None:
        from .config import get_path, parse_value, set_path
        from .tui import PromptScreen

        def named(value: str | None) -> None:
            if value is None:
                return
            try:
                key = _dotted_key(value)
                probe: dict[str, Any] = {}
                set_path(probe, key, 1)  # a key must round-trip through the dotted path
                if get_path(probe, key) != 1:
                    raise ValueError(f"{key} does not round-trip through a dotted path")
            except ValueError as e:
                self.notify(str(e), severity="error")
                return
            have = json.dumps(self.merged[key]) if key in self.merged else ""

            async def valued(text: str | None) -> None:
                if text is None or not text.strip():
                    return
                await self._apply(key, parse_value(text.strip()))

            self.app.push_screen(
                PromptScreen(f"{key} for {self.cfg.name} (JSON: 3, true, \"text\", [..], {{..}}). "
                             "Enter saves, Esc cancels:", value=have), valued)

        self.app.push_screen(
            PromptScreen(f"New setting for {self.cfg.name}, dotted (e.g. models.default, agents.max_alive):"),
            named)

    def action_remove(self) -> None:
        key = self._selected()
        if not key:
            return
        if key not in self.own:
            where = "the global file (G removes it there)" if key in self.shared else "the built-in defaults"
            self.notify(f"{key} isn't set in this swarm's config.json: it comes from {where}",
                        severity="warning")
            return
        try:
            data = _read_obj(self.cfg.path)
            if not _delete_path(data, key):
                raise ValueError(f"{key} is not in {self.cfg.path}")
            self.cfg.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except Exception as e:
            self.notify(str(e), severity="error")
            return
        where, val = self._inherited(key)
        self.notify(f"{key} removed from {self.cfg.path.name}: "
                    + (f"{where} {json.dumps(val)[:50]} applies again" if where != "nothing" else "unset"))
        self.refresh_table()

    def action_reload(self) -> None:
        self.refresh_table()
        self.notify(f"re-read {self.cfg.path}")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._detail()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.action_edit()


# ------------------------------------------------------------------- plugins
class PluginEditScreen(_Form):
    """Edit one plugin's config block as JSON."""

    def __init__(self, name: str, conf: dict):
        super().__init__()
        self.name_ = name
        self.conf = conf

    def compose(self) -> ComposeResult:
        with Vertical(id="form"):
            yield Label(Text(f"plugins.{self.name_} (JSON object; \"enabled\": false keeps it off)", style="bold"))
            yield TextArea(json.dumps(self.conf, indent=2), id="p-json", language="json")
            yield Label("", id="err", classes="err")
            with Horizontal(id="buttons"):
                yield Button("Save", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    async def submit(self) -> None:
        data = json.loads(self.query_one("#p-json", TextArea).text)
        if not isinstance(data, dict):
            raise ValueError("a plugin's config must be a JSON object")
        self.dismiss(data)


class PluginsScreen(Screen):
    """Every plugin gek can see or is told to load: where from, enabled or not, what it did this run."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "back"),
        Binding("space", "toggle", "enable/disable"),
        Binding("e", "edit", "edit config"),
    ]
    CSS = """
    #pl-note { height: auto; padding: 0 1; }
    #pl-detail { height: auto; max-height: 8; padding: 0 1; background: $boost; }
    """

    def __init__(self, cfg: Config, run=None):
        super().__init__()
        self.cfg = cfg
        self.run = run  # RunData of the run being viewed, for this-run status

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="pl-note")
        yield DataTable(id="pl-table", cursor_type="row", zebra_stripes=True)
        yield Static(id="pl-detail")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#pl-table", DataTable).add_columns(
            "plugin", "config", "from", "this run", "tools", "sha256", "last action")
        self.refresh_table()

    def _conf(self) -> dict:
        return {k: v for k, v in (self.cfg.raw.get("plugins") or {}).items() if not k.startswith("_")}

    def refresh_table(self) -> None:
        from .cli import is_running
        from .plugin import find, installed

        self.cfg = _reload(self.cfg)
        conf = self._conf()
        seen = installed(self.cfg)
        live = (self.run.plugins if self.run is not None else {}) or {}
        t = self.query_one("#pl-table", DataTable)
        keep = t.cursor_row
        t.clear()
        for name in sorted(set(seen) | set(conf) | set(live)):
            c = conf.get(name)
            if c is None:
                cstate = Text("not configured", style="dim")
            elif isinstance(c, dict) and c.get("enabled") is False:
                cstate = Text("disabled", style="yellow")
            else:
                cstate = Text("enabled", style="green")
            where = find(self.cfg, name)[0] or "missing"
            p = live.get(name) or {}
            st = p.get("status", "")
            if st == "loaded" and p.get("errors"):
                st = f"loaded, {p['errors']} errors"
            run_state = Text(st or "-", style={"loaded": "green", "disabled": "red", "load error": "red"}.get(
                p.get("status", ""), "yellow" if p.get("errors") else "dim"))
            tools = ", ".join(p.get("tools") or []) or "-"
            last = p.get("last", "") or "-"
            t.add_row(name, cstate, Text(where, style="red" if where == "missing" else ""), run_state,
                      tools, p.get("sha256", "") or "-", last[:60], key=name)
        if t.row_count:
            t.move_cursor(row=min(keep, t.row_count - 1))
        run_name = self.run.path.parent.name if self.run is not None else "no run"
        self.query_one("#pl-note", Static).update(Text.assemble(
            (f"{self.cfg.name} plugins", "bold"),
            f"  |  'this run' is {run_name}  |  space: enable/disable, e: edit config  |  ",
            ("changes apply on the next run" if is_running(self.cfg) else "changes are saved to config.json",
             "yellow" if is_running(self.cfg) else "dim")))
        self._detail()

    def _selected(self) -> str | None:
        t = self.query_one("#pl-table", DataTable)
        if not t.row_count:
            return None
        return str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)

    def _detail(self) -> None:
        from .plugin import find

        name = self._selected()
        box = self.query_one("#pl-detail", Static)
        if not name:
            box.update(Text("no plugins installed or configured. Bundled ones show here; "
                            "drop your own .py into the swarm's plugins/ folder or $GEK_HOME/plugins/.", style="dim"))
            return
        p = ((self.run.plugins if self.run is not None else {}) or {}).get(name, {})
        where, path, ep = find(self.cfg, name)
        out = Text()
        out.append(name, style="bold")
        out.append(f"  {p.get('path') or path or (ep.value if ep else 'not found')}\n", style="dim")
        out.append("config: " + json.dumps(self._conf().get(name)) + "\n")
        if p.get("error"):
            out.append("error: " + str(p["error"]) + "\n", style="red")
        if p.get("last"):
            out.append("last: " + p["last"], style="dim")
        box.update(out)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._detail()

    def _save(self, name: str, conf: dict) -> None:
        from .config import persist_key

        persist_key(self.cfg.path, f"plugins.{name}", conf)
        self.refresh_table()

    def action_toggle(self) -> None:
        name = self._selected()
        if not name:
            return
        c = self._conf().get(name)
        if c is None:
            c, verb = {}, "enabled"
        elif isinstance(c, dict) and c.get("enabled") is False:
            c = {k: v for k, v in c.items() if k != "enabled"}
            verb = "enabled"
        else:
            c, verb = {**(c if isinstance(c, dict) else {}), "enabled": False}, "disabled"
        self._save(name, c)
        hint = ""
        if verb == "enabled" and name == "notify" and not c.get("url"):
            hint = '; it needs a "url": press e'
        self.notify(f"{name} {verb}{hint}")

    def action_edit(self) -> None:
        name = self._selected()
        if not name:
            return
        conf = self._conf().get(name)
        conf = conf if isinstance(conf, dict) else {}

        def done(res) -> None:
            if res is not None:
                self._save(name, res)
                self.notify(f"plugins.{name} saved")

        self.app.push_screen(PluginEditScreen(name, conf), done)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.action_edit()


# --------------------------------------------------------------- global config
# `gek_home()/config.json` is the *global* layer: every swarm under this gek home merges it
# under its own config.json (config.load_config). Nothing in the TUI could see or write it
# before: ConfigScreen shows the *merged* view and writes the swarm's own file. So this screen
# lists only the leaves this file itself sets. A merged row would be a lie here: you can edit it
# (it becomes a swarm override) but you could never unset it, because the value you see came
# from the defaults or from another file.
#
# It must never go through do_control: the control plane's `config set` ends up in
# config.apply_config, which persists into the *swarm's* config.json (swarm.py). Doing that for
# a global setting would shadow the global value forever. Global writes are
# config.persist_key(gek_home()/"config.json", key, value); nothing here needs a running swarm.
GLOBAL_MISSING = object()


def _global_path() -> Path:
    from .config import gek_home

    return gek_home() / "config.json"


def _read_obj(p: Path) -> dict:
    """A config file's own keys (a missing file sets nothing)."""
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{p} is not a JSON object")
    return data


def _load_global() -> dict:
    """The global config file's own keys: never the defaults, never the merged view."""
    return _read_obj(_global_path())


def _delete_path(raw: dict, key: str) -> bool:
    """Drop one dotted key from a config dict; True if it was there. persist_key cannot delete."""
    parts = key.split(".")
    node: Any = raw
    for part in parts[:-1]:
        if not isinstance(node, dict) or not isinstance(node.get(part), dict):
            return False
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        return False
    del node[parts[-1]]
    # drop parents the delete left empty: an empty {} would otherwise sit in the file as a leaf
    for depth in range(len(parts) - 1, 0, -1):
        parent: Any = raw
        for part in parts[:depth - 1]:
            parent = parent[part]
        if parent[parts[depth - 1]] == {}:
            del parent[parts[depth - 1]]
        else:
            break
    return True


def _dotted_key(text: str) -> str:
    key = (text or "").strip()
    if not key or any(not p.strip() for p in key.split(".")):
        raise ValueError("a dotted key like agents.max_alive or models.default")
    return key


class GlobalConfigScreen(Screen):
    """The settings shared by every swarm under this gek home: what gek_home()/config.json sets.

    Rows are that file's leaves only, so every row can be edited, and deleted back to the
    default. Cold keys (api.*, swarm_dir) are read once at start-up: they are editable here and
    tagged "restart" instead of refused.
    """

    BINDINGS = [
        Binding("escape", "app.pop_screen", "back"),
        Binding("enter", "edit", "edit"),
        Binding("e", "edit", "edit", show=False),
        Binding("a", "new", "add"),
        Binding("x", "remove", "remove"),
        Binding("r", "reload", "reload"),
    ]
    CSS = """
    #gc-note { height: auto; padding: 0 1; }
    #gc-detail { height: auto; max-height: 6; padding: 0 1; background: $boost; }
    """

    def __init__(self, cfg: Config | None = None):
        super().__init__()
        self.cfg = cfg  # the swarm being viewed, only to point at the file this screen doesn't edit
        self.leaves: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="gc-note")
        yield DataTable(id="gc-table", cursor_type="row", zebra_stripes=True)
        yield Static(id="gc-detail")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#gc-table", DataTable).add_columns("setting", "value", "applies")
        self.refresh_table()

    def refresh_table(self) -> None:
        t = self.query_one("#gc-table", DataTable)
        keep = t.cursor_row
        t.clear()
        broken = ""
        try:
            self.leaves = flatten(_load_global())
        except (OSError, ValueError) as e:
            self.leaves, broken = {}, f"can't read the file: {e}"
        for k, v in sorted(self.leaves.items()):
            tag = Text("live", style="green") if is_hot(k) else Text("restart", style="dim")
            val = json.dumps(v)
            t.add_row(k, val if len(val) <= 70 else val[:67] + "...", tag, key=k)
        if t.row_count:
            t.move_cursor(row=min(keep, t.row_count - 1))
        p = _global_path()
        note = Text.assemble(
            ("Global config", "bold"),
            "  |  ",
            (str(p), "bold"),
            ("" if p.exists() else "  (no such file yet: a adds the first key)"),
            "  |  every swarm under this gek home merges this under its own config.json"
            "  |  enter/e: edit, a: add, x: delete, r: reload")
        if broken:
            note.append("\n" + broken, style="red")
        self.query_one("#gc-note", Static).update(note)
        self._detail()

    def _selected(self) -> str | None:
        t = self.query_one("#gc-table", DataTable)
        if not t.row_count:
            return None
        return str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)

    def _detail(self) -> None:
        box = self.query_one("#gc-detail", Static)
        key = self._selected()
        if not key:
            box.update(Text("this file sets nothing: every swarm under this gek home runs on the "
                            "built-in defaults. a adds a key (say models.default), and every swarm "
                            "picks it up unless its own config.json sets it too.", style="dim"))
            return
        out = Text()
        out.append(key, style="bold")
        out.append(f" = {json.dumps(self.leaves.get(key))}\n")
        if is_hot(key):
            out.append("live: no restart needed. A run started after this edit gets it, and a "
                       "running swarm re-reads its config while it runs.\n", style="green")
        else:
            out.append("restart: read once at start-up, so the swarm has to be restarted for it.\n",
                       style="yellow")
        if self.cfg is not None:
            out.append(f"a swarm's own config.json wins over this one; c edits "
                       f"{self.cfg.name}'s own file, G (here) the shared one.", style="dim")
        box.update(out)

    def _set_global(self, key: str, value: Any) -> str:
        from .config import persist_key, write_home_defaults

        p = _global_path()
        if not p.exists():
            write_home_defaults()  # bootstrap the file (and .env) the way `gek init` does
        persist_key(p, key, value)  # one key, every other key in the file kept
        self.refresh_table()
        return f"{key} = {json.dumps(value)[:60]} in {p}"

    def _del_global(self, key: str) -> str:
        p = _global_path()
        raw = _load_global()
        if not _delete_path(raw, key):
            raise ValueError(f"{key} is not in {p}")
        p.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        self.refresh_table()
        return f"{key} removed from {p}: the built-in default applies again"

    def action_edit(self) -> None:
        from .tui import PromptScreen

        key = self._selected()
        if not key:
            return
        current = json.dumps(self.leaves.get(key))
        tag = "live" if is_hot(key) else "restart: read at start-up"
        title = f"global {key} ({tag})  (JSON: 3, true, \"text\", [..], {{..}}). Enter saves, Esc cancels:"

        def done(value: str | None) -> None:
            if value is None or value.strip() == current:
                return
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = value  # treat as plain text, exactly like ConfigScreen.action_edit
            try:
                self.notify(self._set_global(key, parsed))
            except Exception as e:  # unwritable home, JSON errors: show, don't crash
                self.notify(str(e), severity="error")

        self.app.push_screen(PromptScreen(title, value=current), done)

    def action_new(self) -> None:
        from .config import get_path, parse_value, set_path
        from .tui import PromptScreen

        def named(value: str | None) -> None:
            if value is None:
                return
            try:
                key = _dotted_key(value)
                probe: dict[str, Any] = {}
                set_path(probe, key, 1)  # a key must round-trip through the dotted path
                if get_path(probe, key) != 1:
                    raise ValueError(f"{key} does not round-trip through a dotted path")
            except ValueError as e:
                self.notify(str(e), severity="error")
                return
            have = json.dumps(self.leaves[key]) if key in self.leaves else ""

            def valued(text: str | None) -> None:
                if text is None or not text.strip():
                    return
                try:
                    self.notify(self._set_global(key, parse_value(text.strip())))
                except Exception as e:
                    self.notify(str(e), severity="error")

            self.app.push_screen(
                PromptScreen(f"{key} (JSON: 3, true, \"text\", [..], {{..}}). Enter saves, Esc cancels:",
                             value=have), valued)

        self.app.push_screen(
            PromptScreen("New global setting, dotted (e.g. models.default, agents.max_alive):"), named)

    def action_remove(self) -> None:
        key = self._selected()
        if not key:
            return
        try:
            self.notify(self._del_global(key))
        except Exception as e:
            self.notify(str(e), severity="error")

    def action_reload(self) -> None:
        self.refresh_table()
        self.notify(f"re-read {_global_path()}")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._detail()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.action_edit()


# --------------------------------------------------------------------- prompts
# Two settings, both hot, both per swarm: prompts.orchestrator_prior (empty = the built-in
# prior in prompts.py) and prompts.agents_extra (empty = no extra text). Saving goes through
# do_control exactly like every other TUI edit, so it is an audited control commit when the
# swarm runs and a config.json write when it doesn't. G is the other half: the shared settings.
PROMPT_KEYS = ("prompts.orchestrator_prior", "prompts.agents_extra")
PROMPT_HELP = {
    "prompts.orchestrator_prior": "the orchestrator's standing instructions; empty = the built-in prior",
    "prompts.agents_extra": "extra text appended to every agent's system prompt; empty = none",
}


class PromptsScreen(Screen):
    """Edit the prompt text this swarm uses: pick a key, edit the box, ctrl+s saves."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "back"),
        Binding("ctrl+s", "save", "save"),
    ]
    CSS = """
    #pr-list { width: 26; border-right: solid $primary-darken-2; }
    #pr-note { height: auto; padding: 0 1; }
    .-narrow #pr-list { width: 14; }
    """

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.key = PROMPT_KEYS[0]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield ListView(id="pr-list")
            with Vertical(id="pr-edit"):
                yield TextArea(id="pr-text")
                yield Static(id="pr-note")
        yield Footer()

    async def on_mount(self) -> None:
        lv = self.query_one("#pr-list", ListView)
        for k in PROMPT_KEYS:
            item = ListItem(Label(k.split(".", 1)[1]))
            item.prompt_key = k  # type: ignore[attr-defined]
            await lv.append(item)
        lv.index = 0
        self.query_one("#pr-text", TextArea).text = self._shown()
        self.query_one("#pr-text", TextArea).focus()  # typing goes into the text, not the list
        self._note()

    def _stored(self, key: str) -> str:
        from .config import get_path

        v = get_path(self.cfg.raw, key)
        return v if isinstance(v, str) else ""

    def _shown(self) -> str:
        """The override, or the built-in text when there is none (editing it overrides it)."""
        from .prompts import ORCHESTRATOR_PRIOR

        stored = self._stored(self.key)
        if stored:
            return stored
        return ORCHESTRATOR_PRIOR if self.key == "prompts.orchestrator_prior" else ""

    async def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        key = getattr(event.item, "prompt_key", None)
        if key and key != self.key:
            self.key = key
            self.query_one("#pr-text", TextArea).text = self._shown()
        self._note()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        self.query_one("#pr-text", TextArea).focus()  # picked a key: now edit its text

    def _live(self) -> bool:
        from .cli import is_running

        return is_running(self.cfg)

    def _note(self) -> None:
        stored = self._stored(self.key)
        if not stored and self.key == "prompts.orchestrator_prior":
            state = ("nothing is set: the box shows the built-in prior. Edit it and ctrl+s to "
                     "override it for this swarm.")
        elif not stored:
            state = "nothing is set: no agent gets extra text. Type and ctrl+s to add."
        else:
            state = (f"this swarm overrides it ({len(stored)} characters). ctrl+s saves the box; "
                     "clear it and ctrl+s to fall back to the built-in.")
        out = Text.assemble(
            (self.key, "bold"), "\n",
            (PROMPT_HELP[self.key], "dim"), "\n", state, "\n",
            "file: ", (str(self.cfg.path), "bold"),
            "  (prompts are per swarm; G is the shared settings file under your gek home)\n",
            ("a running swarm applies this within a few seconds: the next orchestrator wake and "
             "agents spawned after it" if self._live()
             else "not running, so this is saved for the next run"),
            ",  the list picks the key (click it, or tab to it), ctrl+s: save, esc: back")
        self.query_one("#pr-note", Static).update(out)

    def action_save(self) -> None:
        # The commit has to outlive this screen (see McpScreen._edit): run it on the App.
        self.app.run_worker(self._save())

    async def _save(self) -> None:
        from .cli import do_control

        key = self.key
        text = self.query_one("#pr-text", TextArea).text
        try:
            msg = await do_control(self.cfg, WHO, f"config set {key}", "edited from the TUI",
                                   json.dumps(text))
        except Exception as e:
            self.notify(str(e), severity="error")
            return
        self.notify(msg)
        if not self._live():
            self.cfg = _reload(self.cfg)  # offline it is on disk already, so show it as stored
            self._note()


# ------------------------------------------------------------------- mcp servers
# An MCP server is one entry of the top-level `mcp_servers` dict: stdio ones have command/args
# (plus env, cwd), streamable-http ones have a url. Every `mcp_servers.*` key is hot, so a change
# rides the control plane into the running swarm and the agents reconnect; no restart. Nothing
# here writes config.json: a not-running swarm gets the cli's offline branch of do_control.
MCP_FIELDS = ("command", "args", "env", "cwd", "url", "timeout_s", "enabled")


def _mcp_servers(cfg: Config) -> dict[str, dict]:
    """The configured servers, minus _comment keys; a bare string entry means {"command": it}."""
    out: dict[str, dict] = {}
    for name, spec in (cfg.raw.get("mcp_servers") or {}).items():
        if str(name).startswith("_"):
            continue
        if isinstance(spec, dict):
            out[str(name)] = dict(spec)
        elif isinstance(spec, str):
            out[str(name)] = {"command": spec}
        else:
            out[str(name)] = {}
    return out


def _mcp_endpoint(spec: dict | None) -> str:
    if not spec:
        return "-"
    if spec.get("url"):
        return str(spec["url"])
    parts = [str(spec.get("command") or "?")] + [str(a) for a in spec.get("args") or []]
    return " ".join(parts)


def _mcp_run_state(run) -> dict[str, dict]:
    """What the run viewed behind this screen says about each server (empty when no run)."""
    return dict(getattr(run, "mcp", None) or {})


class McpEditScreen(_Form):
    """One MCP server. stdio: command + args (+ env, cwd). http: url. Keys we don't show survive."""

    def __init__(self, name: str, spec: dict):
        super().__init__()
        self.name_ = name
        self.spec = dict(spec or {})
        self.extra = {k: v for k, v in self.spec.items() if k not in MCP_FIELDS}

    def compose(self) -> ComposeResult:
        args = self.spec.get("args") or []
        env = self.spec.get("env") or {}
        with Vertical(id="form"):
            yield Label(Text(f"mcp_servers.{self.name_}", style="bold"))
            yield Label("command (stdio: e.g. uvx, npx). Leave blank for an http server.")
            yield Input(value=str(self.spec.get("command") or ""), id="cmd")
            yield Label("args (spaced, or a JSON list)")
            yield Input(value=" ".join(str(a) for a in args), id="args")
            yield Label('env (JSON object, e.g. {"API_KEY": "..."})')
            yield Input(value=json.dumps(env) if env else "", id="env")
            yield Label("cwd (optional)")
            yield Input(value=str(self.spec.get("cwd") or ""), id="cwd")
            yield Label("url (http servers only)")
            yield Input(value=str(self.spec.get("url") or ""), id="url")
            yield Label("timeout_s (how long connecting may take; default 60)")
            yield Input(value=str(self.spec.get("timeout_s") or ""), id="timeout")
            yield Checkbox("enabled (off = configured, but never connected)", id="enabled",
                           value=self.spec.get("enabled") is not False)
            if self.extra:
                yield Label(Text("kept as-is: " + ", ".join(sorted(self.extra)), style="dim"))
            yield Label("", id="err", classes="err")
            with Horizontal(id="buttons"):
                yield Button("Save", variant="primary", id="ok")
                if self.spec:
                    yield Button("Remove", id="del", variant="error")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#cmd", Input).focus()

    async def submit(self) -> None:
        import shlex

        conf = dict(self.extra)  # anything we don't show round-trips untouched
        command = self.query_one("#cmd", Input).value.strip()
        url = self.query_one("#url", Input).value.strip()
        if not command and not url:
            self._err("a command (stdio) or a url (http) is needed")
            return
        if command:
            conf["command"] = command
        raw_args = self.query_one("#args", Input).value.strip()
        if raw_args:
            if raw_args.startswith("["):
                parsed = json.loads(raw_args)
                if not isinstance(parsed, list):
                    raise ValueError('args as JSON must be a list, e.g. ["--root", "/tmp"]')
                conf["args"] = [str(a) for a in parsed]
            else:
                conf["args"] = shlex.split(raw_args)
        raw_env = self.query_one("#env", Input).value.strip()
        if raw_env:
            parsed_env = json.loads(raw_env)
            if not isinstance(parsed_env, dict):
                raise ValueError('env must be a JSON object, e.g. {"API_KEY": "sk-..."}')
            conf["env"] = {str(k): str(v) for k, v in parsed_env.items()}
        cwd = self.query_one("#cwd", Input).value.strip()
        if cwd:
            conf["cwd"] = cwd
        if url:
            conf["url"] = url
        timeout = self.query_one("#timeout", Input).value.strip()
        if timeout:
            conf["timeout_s"] = float(timeout)
        if self.query_one("#enabled", Checkbox).value:
            conf.pop("enabled", None)  # enabled is the default; a missing key keeps the JSON small
        else:
            conf["enabled"] = False
        self.dismiss(conf)


class McpScreen(Screen):
    """MCP servers: what's configured, what this run connected, and live enable/disable/edit.

    Shaped like PluginsScreen, but every change is an audited control-plane action (do_control),
    so it lands in the orchestrator's outside-actions briefing and applies without a restart.
    """

    BINDINGS = [
        Binding("escape", "app.pop_screen", "back"),
        Binding("space", "toggle", "enable/disable"),
        Binding("e", "edit", "edit"),
        Binding("enter", "edit", "edit", show=False),
        Binding("a", "new", "add"),
        Binding("x", "remove", "remove"),
    ]
    CSS = """
    #m-note { height: auto; padding: 0 1; }
    #m-detail { height: auto; max-height: 9; padding: 0 1; background: $boost; }
    """

    def __init__(self, cfg: Config, run=None):
        super().__init__()
        self.cfg = cfg
        self.run = run  # RunData of the run being viewed; its mcp events say what connected

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="m-note")
        yield DataTable(id="m-table", cursor_type="row", zebra_stripes=True)
        yield Static(id="m-detail")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#m-table", DataTable).add_columns("name", "endpoint", "tools", "enabled", "live")
        self.refresh_table()

    def refresh_table(self) -> None:
        from .cli import is_running

        self.cfg = _reload(self.cfg)
        conf = _mcp_servers(self.cfg)
        live = _mcp_run_state(self.run)
        t = self.query_one("#m-table", DataTable)
        keep = t.cursor_row
        t.clear()
        for name in sorted(set(conf) | set(live)):
            spec = conf.get(name)
            if spec is None:
                enabled = Text("not configured", style="dim")
                endpoint = Text(_mcp_endpoint(live[name] if isinstance(live.get(name), dict) else None),
                                style="dim")
            else:
                enabled = (Text("disabled", style="yellow") if spec.get("enabled") is False
                           else Text("enabled", style="green"))
                endpoint = Text(_mcp_endpoint(spec)[:48], style="dim")
            st = live.get(name) or {}
            if st.get("ok"):
                lstate = Text("connected", style="green")
            elif st.get("error"):
                lstate = Text("failed", style="red")
            else:
                lstate = Text("-", style="dim")
            granted, listed = st.get("granted") or [], st.get("listed") or []
            if granted:
                tools = Text(", ".join(granted)[:52])
            elif listed:
                tools = Text(f"{len(listed)} listed, none granted", style="yellow")
            else:
                tools = Text("-", style="dim")
            t.add_row(name, endpoint, tools, enabled, lstate, key=name)
        if t.row_count:
            t.move_cursor(row=min(keep, t.row_count - 1))
        running = is_running(self.cfg)
        where = ("running, changes apply now (agents reconnect)"
                 if running else "not running, changes apply next run")
        self.query_one("#m-note", Static).update(Text.assemble(
            (f"{self.cfg.name} MCP servers", "bold"),
            "  |  space: enable/disable, e/enter: edit, a: add, x: remove  |  ",
            (where, "yellow" if running else "dim")))
        self._detail()

    def _selected(self) -> str | None:
        t = self.query_one("#m-table", DataTable)
        if not t.row_count:
            return None
        return str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)

    def _detail(self) -> None:
        name = self._selected()
        box = self.query_one("#m-detail", Static)
        if not name:
            box.update(Text("no MCP servers configured. a adds one: a command (+ args) for stdio, "
                            "or a url for streamable-http. Grant a server to a role with "
                            "powers.mcp: {server: [tool globs]}.", style="dim"))
            return
        spec = _mcp_servers(self.cfg).get(name)
        st = _mcp_run_state(self.run).get(name) or {}
        out = Text()
        out.append(name, style="bold")
        out.append(f"  {_mcp_endpoint(spec) if spec else 'not in mcp_servers'}\n", style="dim")
        if spec:
            out.append("config: " + json.dumps(spec)[:300] + "\n")
        if st.get("granted"):
            out.append("tools this run: " + ", ".join(st["granted"])[:200] + "\n")
        if st.get("listed"):
            out.append(f"listed by the server: {len(st['listed'])}\n", style="dim")
        if st.get("error"):
            out.append("error: " + str(st["error"])[:200] + "\n", style="red")
        box.update(out)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._detail()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.action_edit()

    async def _control(self, control: str, reason: str, payload: str = "") -> None:
        from .cli import do_control

        try:
            msg = await do_control(self.cfg, WHO, control, reason, payload)
        except Exception as e:
            text = str(e)
            if "isn't running" in text:
                text += "  (start it with R: MCP servers connect at run start)"
            self.notify(text, severity="error")
            return
        self.notify(msg)
        self.set_timer(1.5, self.refresh_table)  # a running swarm applies it within a few seconds

    async def action_toggle(self) -> None:
        name = self._selected()
        if not name:
            return
        spec = _mcp_servers(self.cfg).get(name)
        if spec is None:
            self.notify(f"{name} is not in mcp_servers yet: press e to configure it", severity="warning")
            return
        verb = "enable" if spec.get("enabled") is False else "disable"
        await self._control(f"mcp {verb} {name}", f"{verb}d from the TUI")

    def action_edit(self) -> None:
        name = self._selected()
        if not name:
            return
        self._edit(name, _mcp_servers(self.cfg).get(name) or {})

    async def action_remove(self) -> None:
        name = self._selected()
        if not name or name not in _mcp_servers(self.cfg):
            return
        await self._control(f"mcp rm {name}", "removed from the TUI")

    def action_new(self) -> None:
        from .tui import PromptScreen

        def done(value: str | None) -> None:
            name = (value or "").strip()
            if name:
                self.app.call_after_refresh(lambda: self._edit(name, {}))

        self.app.push_screen(PromptScreen("Name for the new MCP server (e.g. files), Enter to configure:"),
                             done)

    def _edit(self, name: str, spec: dict) -> None:
        def done(res) -> None:
            # The control commit outlives this screen: a worker owned by McpScreen is cancelled
            # when the screen unmounts (textual Widget._on_unmount -> workers.cancel_node), i.e.
            # as soon as the user leaves the MCP screen, so run it on the App.
            if isinstance(res, dict):
                self.app.run_worker(self._control(f"mcp set {name}", f"set {name} from the TUI",
                                                  json.dumps(res)))
            elif res == "delete":
                self.app.run_worker(self._control(f"mcp rm {name}", "removed from the TUI"))

        self.app.push_screen(McpEditScreen(name, spec), done)


# ------------------------------------------------------------------- resume
class ResumeScreen(_Form):
    """What the last run left behind, before resuming it."""

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg

    def _summary(self) -> tuple[Text, bool]:
        import subprocess

        from .cli import is_running
        from .resume import read_resume_info

        if is_running(self.cfg):
            return Text(f"{self.cfg.name} is running; there's nothing to resume.", style="yellow"), False
        info = read_resume_info(self.cfg)
        if info.run is None:
            return Text("no runs yet: use R (run) instead.", style="yellow"), False
        out = Text()
        out.append(f"last run {info.run}: ", style="bold")
        out.append("CRASHED (no clean shutdown)\n" if info.crashed else f"ended: {info.ended}\n",
                   style="red" if info.crashed else "")
        if info.last_note:
            out.append(f"orchestrator's last note: {info.last_note[:300]}\n", style="dim")
        out.append(f"tokens so far: {info.tokens:,} (carried into the budget unless you tick fresh budget)\n")
        if info.interrupted:
            n = len(info.interrupted)
            out.append(f"\n{n} agent{'s' if n != 1 else ''} {'were' if n != 1 else 'was'} mid-work:\n", style="bold")
            for a in info.interrupted[:12]:
                out.append(f"  {a.id:<22} {a.how:<8} {a.steps} steps on {a.branch}\n")
                if a.last:
                    out.append(f"      last: {a.last[:110]}\n", style="dim")
        else:
            out.append("\nno agents were mid-work\n", style="dim")
        dirty = []
        wt_root = self.cfg.worktrees_dir
        if wt_root.exists():
            for wt in sorted(p for p in wt_root.iterdir() if p.is_dir()):
                try:
                    r = subprocess.run(["git", "status", "--porcelain"], cwd=wt, capture_output=True,
                                       text=True, timeout=15)
                    n = len([ln for ln in r.stdout.splitlines() if ln.strip()])
                except Exception:  # noqa: BLE001
                    n = -1
                if n:
                    dirty.append((wt.name, n))
        if dirty:
            out.append("\nuncommitted work to salvage onto its branch:\n", style="bold")
            for name, n in dirty:
                out.append(f"  {name}: {'?' if n < 0 else n} files\n")
        return out, True

    def compose(self) -> ComposeResult:
        text, ok = self._summary()
        self.ok = ok
        with Vertical(id="form"):
            yield Label(Text(f"Resume {self.cfg.name}", style="bold"))
            yield Static(text, id="r-summary")
            yield Checkbox("respawn the interrupted agents onto their branches", value=True, id="r-respawn",
                           disabled=not ok)
            yield Checkbox("fresh budget (don't count earlier runs' tokens)", value=False, id="r-fresh",
                           disabled=not ok)
            yield Label("", id="err", classes="err")
            with Horizontal(id="buttons"):
                yield Button("Resume", id="ok", variant="primary", disabled=not ok)
                yield Button("Cancel", id="cancel")

    async def submit(self) -> None:
        from .cli import start_detached

        extra = ["--resume"]
        if self.query_one("#r-respawn", Checkbox).value:
            extra.append("--respawn")
        if self.query_one("#r-fresh", Checkbox).value:
            extra.append("--fresh-budget")
        pid = start_detached(self.cfg, extra=extra)
        self.dismiss(f"{self.cfg.name} resuming in the background (pid {pid}; {' '.join(extra[1:]) or 'no respawn'})")
