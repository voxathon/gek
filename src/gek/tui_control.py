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
