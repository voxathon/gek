"""Control-plane screens for `gek tui`: new swarm, spawn, push, roles editor, config table.

Everything goes through the same paths as the CLI (cli.do_control, init_home_swarm,
push_swarm, start_detached), so the TUI can't do anything the protocol doesn't record.
"""

from __future__ import annotations

import json

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
        if event.button.id == "cancel":
            self.dismiss(None)
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
                            self.cfg.raw.get("mcp_servers", {}))
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
    """Every setting; Enter on a live one edits it (applied without restarting)."""

    BINDINGS = [Binding("escape", "app.pop_screen", "back"), Binding("enter", "edit", "edit")]

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="c-note")
        yield DataTable(id="c-table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#c-table", DataTable)
        t.add_columns("setting", "value", "")
        self.refresh_table()

    def refresh_table(self) -> None:
        from .cli import is_running

        self.cfg = _reload(self.cfg)
        t = self.query_one("#c-table", DataTable)
        keep = t.cursor_row
        t.clear()
        for k, v in sorted(flatten(self.cfg.raw).items()):
            tag = Text("live", style="green") if is_hot(k) else Text("restart", style="dim")
            val = json.dumps(v)
            t.add_row(k, val if len(val) <= 80 else val[:77] + "...", tag, key=k)
        if t.row_count:
            t.move_cursor(row=min(keep, t.row_count - 1))
        self.query_one("#c-note", Static).update(
            f"{self.cfg.name}: {'running, changes apply now' if is_running(self.cfg) else 'not running, changes apply next run'}"
            f"  |  file: {self.cfg.path}")

    def action_edit(self) -> None:
        from .tui import PromptScreen

        t = self.query_one("#c-table", DataTable)
        if not t.row_count:
            return
        key = str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
        if not is_hot(key):
            self.notify(f"{key} needs a restart: edit {self.cfg.path}", severity="warning")
            return
        from .config import get_path

        current = json.dumps(get_path(self.cfg.raw, key))
        warn = "  CEILING: this changes what the swarm may grant." if key.startswith("ceiling") else ""
        title = f"{key} (JSON: 3, true, \"text\", [..], {{..}}).{warn} Enter saves, Esc cancels:"

        async def done(value: str | None) -> None:
            from .cli import do_control

            if value is None or value.strip() == current:
                return
            try:
                json.loads(value)
            except json.JSONDecodeError:
                value = json.dumps(value)  # treat as plain text
            try:
                self.notify(await do_control(self.cfg, WHO, f"config set {key}", "set from the TUI", value))
            except Exception as e:
                self.notify(str(e), severity="error")
            self.set_timer(1.5, self.refresh_table)

        self.app.push_screen(PromptScreen(title, value=current), done)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.action_edit()
