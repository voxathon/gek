"""`gek tui`: watch swarms. Reads the run logs (events.jsonl) directly, so it works on a
live run, a finished one, or one from four months ago.

Keys: up/down pick a swarm, [ and ] step through runs, Enter on an agent opens its
transcript (f full/short, s system prompt, esc back), i sends an inbox message, k kills the
selected agent, H halts the swarm, q quits.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.rule import Rule
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Label, ListItem, ListView, RichLog, Static
from textual_plotext import PlotextPlot

from .config import Config, current_swarm, gek_home, list_swarms, load_swarm
from .logs import feed_line

TICK_S = 1.0


# ----------------------------------------------------------------------- data
@dataclass
class AgentRow:
    id: str
    role: str = ""
    status: str = "alive"
    calls: int = 0
    prompt: int = 0
    cached: int = 0
    completion: int = 0
    last: str = ""
    last_prompt: int = 0      # size of its latest prompt, what compaction looks at
    compactions: int = 0
    tools: dict = field(default_factory=dict)  # source -> [tool names], from its `tools` event


@dataclass
class RunData:
    """Incrementally parsed events.jsonl for one run."""

    path: Path
    offset: int = 0
    t0: float | None = None
    t_last: float | None = None
    ended: str | None = None
    wakes: int = 0
    goal: str = ""
    tokens: int = 0
    cached: int = 0
    cost: float = 0.0
    token_series: list[tuple[float, int, int]] = field(default_factory=list)
    alive_series: list[tuple[float, int]] = field(default_factory=list)
    alive: int = 0
    commits: list[float] = field(default_factory=list)
    merges: list[float] = field(default_factory=list)
    agents: dict[str, AgentRow] = field(default_factory=dict)
    feed: deque = field(default_factory=lambda: deque(maxlen=2000))
    view_end: float | None = None  # x-axis end: now for a live run, last event otherwise
    wake_list: list[tuple[float, int, str]] = field(default_factory=list)  # (t, n, why)
    mode: str = "llm"
    paused: bool = False
    new_feed: list[str] = field(default_factory=list)
    sandbox: dict = field(default_factory=dict)       # the `sandbox` event's data
    compaction_list: list[tuple[float, str, int, int, int]] = field(default_factory=list)  # t, actor, step, before, after
    compact_notes: dict = field(default_factory=dict)  # (actor, step) -> (before, after)
    plugins: dict = field(default_factory=dict)        # name -> what this run's events say about it
    mcp: dict = field(default_factory=dict)            # server -> what this run's events say about it
    role_compact_at: dict = field(default_factory=dict)  # role -> compact_at, from role_written
    events: int = 0
    _partial: str = ""

    def poll(self) -> bool:
        """Read new lines. Returns True if anything changed."""
        try:
            with open(self.path, encoding="utf-8") as f:
                f.seek(self.offset)
                chunk = f.read()
                self.offset = f.tell()
        except OSError:
            return False
        if not chunk:
            return False
        data = self._partial + chunk
        lines = data.split("\n")
        self._partial = lines.pop()  # incomplete last line, if any
        changed = False
        for line in lines:
            if not line.strip():
                continue
            try:
                self._ingest(json.loads(line))
                changed = True
            except Exception:
                continue
        return changed

    def _agent(self, aid: str) -> AgentRow:
        a = self.agents.get(aid)
        if a is None:
            a = self.agents[aid] = AgentRow(id=aid, role=aid.split("/")[0])
            if aid == "orchestrator":
                a.status = "-"
        return a

    def _ingest(self, e: dict[str, Any]) -> None:
        k, actor, d, t = e["kind"], e.get("actor", ""), e.get("data", {}), e["t"]
        if self.t0 is None:
            self.t0 = t
            self.alive_series.append((t, 0))
        self.t_last = t
        self.events += 1
        if k == "run_start":
            self.goal = d.get("goal", "")
        elif k == "run_end":
            self.ended = d.get("reason", "?")
        elif k == "mode":
            self.mode = d.get("mode", self.mode)
        elif k == "pause":
            self.paused = bool(d.get("paused"))
        elif k == "orch_wake":
            self.wakes = d.get("n", self.wakes + 1)
            self.wake_list.append((t, self.wakes, ", ".join(d.get("reasons") or []) or "?"))
        elif k == "llm_response":
            u = d.get("usage") or {}
            tot = u.get("total_tokens", 0) or 0
            cch = u.get("cached_prompt_tokens", 0) or 0
            self.tokens += tot
            self.cached += cch
            self.cost += d.get("cost") or 0.0
            self.token_series.append((t, self.tokens, self.cached))
            a = self._agent(actor)
            a.calls += 1
            a.prompt += u.get("prompt_tokens", 0) or 0
            a.completion += u.get("completion_tokens", 0) or 0
            a.cached += cch
            a.last_prompt = u.get("prompt_tokens", 0) or a.last_prompt
        elif k == "agent_spawn":
            a = self._agent(d.get("id", "?"))
            a.role = d.get("role", a.role)
            a.status = "alive"
            self.alive += 1
            self.alive_series.append((t, self.alive))
        elif k == "agent_exit":
            a = self._agent(actor)
            a.status = d.get("how", "exited")
            self.alive = max(0, self.alive - 1)
            self.alive_series.append((t, self.alive))
        elif k == "sandbox":
            self.sandbox = dict(d)
        elif k == "compact":
            a = self._agent(actor)
            if d.get("ok"):
                a.compactions += 1
                a.last_prompt = d.get("after_tokens_est", a.last_prompt)
                self.compaction_list.append((t, actor, d.get("step", 0), d.get("before_tokens", 0),
                                             d.get("after_tokens_est", 0)))
                self.compact_notes[(actor, d.get("step", 0))] = (d.get("before_tokens", 0), d.get("after_tokens_est", 0))
        elif k == "tools":
            self._agent(actor).tools = dict(d.get("sources") or {})
        elif k == "role_written":
            role = d.get("role") or {}
            if isinstance(role, dict) and role.get("name"):
                self.role_compact_at[role["name"]] = int(role.get("compact_at") or 0)
        elif k == "plugin" and actor.startswith("plugin:"):
            name = actor.split(":", 1)[1]
            p = self.plugins.setdefault(name, {"status": "?", "errors": 0, "since": self.events, "last": ""})
            if "loaded" in d:
                p.update(status="loaded" if d["loaded"] else "load error", since=self.events,
                         where=d.get("where", ""), path=d.get("path", ""), sha256=d.get("sha256", ""),
                         tools=d.get("tools") or [], error=d.get("error", ""))
            elif d.get("disabled"):
                p.update(status="disabled", error=d.get("reason", ""))
            elif d.get("error"):
                p["errors"] = d.get("errors", p["errors"] + 1)
                p["error"] = d["error"]
            else:
                p["last"] = ", ".join(f"{k2}={v}" for k2, v in d.items())[:120]
                p["last_t"] = t
        elif k == "mcp":
            # per-agent connect attempts: server, ok, listed (offered), granted (kept for the role)
            s = self.mcp.setdefault(d.get("server", "?"), {})
            if d.get("closing"):
                pass
            elif d.get("ok"):
                s.update(ok=True, error="", listed=d.get("listed") or [], granted=d.get("granted") or [],
                         actor=actor)
            else:
                s.update(ok=False, error=d.get("error", ""), actor=actor)
        elif k == "tool_call":
            self._agent(actor).last = d.get("name", "")
        elif k == "commit" and not str(d.get("ref", "")).startswith("swarm/"):
            self.commits.append(t)
        elif k == "merge" and d.get("ok"):
            self.merges.append(t)
        fl = feed_line(k, actor, d)
        if fl:
            ts = time.strftime("%H:%M:%S", time.localtime(t))
            line = f"{ts} {fl}"
            self.feed.append(line)
            self.new_feed.append(line)


def runs_of(cfg: Config) -> list[Path]:
    if not cfg.logs_dir.exists():
        return []
    from .logs import list_runs

    return list_runs(cfg.logs_dir)


def running_run(cfg: Config) -> str | None:
    lock = cfg.swarm_dir / "run.lock"
    try:
        return json.loads(lock.read_text(encoding="utf-8")).get("run")
    except Exception:
        return None


# ------------------------------------------------------------------- widgets
def _mins(run: RunData, t: float) -> float:
    return round((t - (run.t0 or t)) / 60.0, 3)


WAKE_COLOR = "orange"
COMPACT_COLOR = "cyan"


def _wake_lines(plt, run: RunData, as_minute_index: bool = False) -> None:
    """Orchestrator wakes as vertical lines, so you can see what each wake set off."""
    for t, _, _ in run.wake_list:
        x = int((t - (run.t0 or t)) // 60) if as_minute_index else _mins(run, t)
        plt.vline(x, WAKE_COLOR)


class TokensPlot(PlotextPlot):
    def draw(self, run: RunData | None) -> None:
        plt = self.plt
        plt.clear_data()
        plt.clear_figure()
        plt.title("tokens (k)   orange | wake   cyan | compaction")
        plt.xlabel("minutes")
        if run and run.token_series:
            pts = list(run.token_series)
            if run.view_end and run.view_end > pts[-1][0]:
                pts.append((run.view_end, pts[-1][1], pts[-1][2]))  # flat until now
            xs = [_mins(run, t) for t, _, _ in pts]
            _wake_lines(plt, run)
            for ct, *_ in run.compaction_list:  # each compaction: does the slope flatten after it?
                plt.vline(_mins(run, ct), COMPACT_COLOR)
            plt.plot(xs, [n / 1000 for _, n, _ in pts], label="total", marker="braille")
            if run.cached:
                plt.plot(xs, [c / 1000 for _, _, c in pts], label="cached", marker="braille")
        self.refresh()


class AlivePlot(PlotextPlot):
    def draw(self, run: RunData | None) -> None:
        plt = self.plt
        plt.clear_data()
        plt.clear_figure()
        plt.title("live agents")
        plt.xlabel("minutes")
        if run and run.alive_series:
            xs, ys = [], []
            prev = None
            for t, n in run.alive_series:  # step shape
                x = _mins(run, t)
                if prev is not None:
                    xs.append(x)
                    ys.append(prev)
                xs.append(x)
                ys.append(n)
                prev = n
            end = run.view_end or run.t_last or 0
            xs.append(_mins(run, end))
            ys.append(prev or 0)
            _wake_lines(plt, run)
            plt.plot(xs, ys, marker="braille")
            top = max(ys + [1]) + 1
            plt.ylim(0, top)
            plt.yticks(list(range(0, top + 1)))
        self.refresh()


class CommitsPlot(PlotextPlot):
    def draw(self, run: RunData | None) -> None:
        plt = self.plt
        plt.clear_data()
        plt.clear_figure()
        plt.title("commits / minute")
        plt.xlabel("minute")
        if run and run.t0 is not None and (run.commits or run.t_last):
            end = run.view_end or run.t_last or run.t0
            nmin = int((end - run.t0) // 60) + 1
            buckets = [0] * nmin
            for t in run.commits:
                i = int((t - run.t0) // 60)
                if 0 <= i < nmin:
                    buckets[i] += 1
            plt.bar(list(range(nmin)), buckets, marker="sd")
            plt.yticks(list(range(0, max(buckets + [1]) + 1)))
        self.refresh()


class SwarmItem(ListItem):
    def __init__(self, label: str, cfg: Config):
        super().__init__(Static(label))
        self.cfg = cfg


# ---------------------------------------------------------------- transcript
def _clip_block(text: str, full: bool, limit: int) -> str:
    if full or len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({len(text) - limit} more chars; press f for full)"


def _pretty_args(raw: str) -> str:
    try:
        obj = json.loads(raw or "{}")
    except Exception:
        return raw
    if isinstance(obj, dict) and set(obj) == {"command"}:
        return str(obj["command"])
    if isinstance(obj, dict) and obj and all(isinstance(v, (str, int, float, bool)) for v in obj.values()):
        # simple args: one "key: value" per line, with real newlines in long strings
        out = []
        for k, v in obj.items():
            v = str(v)
            out.append(f"{k}:\n  " + v.replace("\n", "\n  ") if "\n" in v or len(v) > 80 else f"{k}: {v}")
        return "\n".join(out)
    return json.dumps(obj, indent=2, ensure_ascii=False)


class TranscriptScreen(Screen):
    """One actor's whole conversation, tailing while it lives."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "back"),
        Binding("q", "app.pop_screen", "back"),
        Binding("f", "toggle_full", "full/short"),
        Binding("s", "toggle_system", "system prompt"),
        Binding("g", "top", "top"),
        Binding("G", "bottom", "bottom"),
        Binding("t", "toggle_tools", "tools"),
    ]
    CSS = """
    #t-head { height: auto; max-height: 12; padding: 0 1; background: $boost; }
    #t-log { height: 1fr; }
    """

    def __init__(self, path: Path, actor: str, run: RunData, cfg: Config | None):
        super().__init__()
        self._path, self._actor, self._run, self._cfg = path, actor, run, cfg
        self._off = 0
        self._part = ""
        self._msgs: list[dict[str, Any]] = []
        self._full = False
        self._show_sys = False
        self._show_tools = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="t-head")
        yield RichLog(id="t-log", wrap=True, highlight=False, markup=False, max_lines=20000)
        yield Footer()

    def on_mount(self) -> None:
        self._read_new()
        self._render_all()
        self.set_interval(TICK_S, self._tick)

    def _read_new(self) -> list[dict[str, Any]]:
        try:
            with open(self._path, encoding="utf-8") as f:
                f.seek(self._off)
                chunk = f.read()
                self._off = f.tell()
        except OSError:
            return []
        lines = (self._part + chunk).split("\n")
        self._part = lines.pop()
        new = []
        for line in lines:
            if line.strip():
                try:
                    new.append(json.loads(line))
                except Exception:
                    pass
        self._msgs.extend(new)
        return new

    def _head(self) -> None:
        a = self._run.agents.get(self._actor)
        stat = (("" if a.status == "-" else f"{a.status}, ") + f"{a.calls} calls, {a.prompt:,} prompt tokens "
                f"({100 * a.cached / max(a.prompt, 1):.0f}% cached), {a.completion:,} out") if a else ""
        mode = "full" if self._full else "short (f: full)"
        head = Text()
        head.append(self._actor, style="bold")
        head.append("  " + stat + "\n")
        head.append(f"{len(self._msgs)} messages | {mode} | s: system prompt "
                    f"{'shown' if self._show_sys else 'hidden'} | {self._path}", style="dim")
        if a is not None and (a.compactions or a.last_prompt):
            head.append(f"\ncontext {a.last_prompt:,} tokens", style="dim")
            if a.compactions:
                head.append(f", compacted {a.compactions}x", style="cyan")
        if a is not None and a.tools:
            head.append("\ntools ", style="bold")
            parts = []
            for src, names in sorted(a.tools.items(), key=lambda kv: kv[0] != "gek"):
                if src == "gek" and not self._show_tools:
                    parts.append(f"gek: {len(names)} core")
                else:
                    parts.append(f"{src}: {', '.join(names)}")
            head.append(" | ".join(parts))
            head.append("   (t: " + ("hide core names)" if self._show_tools else "show core names)"), style="dim")
        self.query_one("#t-head", Static).update(head)

    def _render_all(self) -> None:
        log = self.query_one("#t-log", RichLog)
        log.clear()
        self._wake = None
        for m in self._msgs:
            self._write(log, m)
        self._head()

    def _tick(self) -> None:
        new = self._read_new()
        if new:
            log = self.query_one("#t-log", RichLog)
            for m in new:
                self._write(log, m)
            self._head()

    def _write(self, log: RichLog, m: dict[str, Any]) -> None:
        if "wake" in m and m["wake"] != self._wake:
            self._wake = m["wake"]
            log.write(Rule(f"wake #{self._wake}", style="dark_orange"))
        ts = str(m.get("ts", ""))[11:19]
        role = m.get("role", "?")
        content = m.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        if role == "system":
            log.write(Text(f"{ts} SYSTEM PROMPT", style="bold dim"))
            log.write(Text(content if self._show_sys else _clip_block(content, False, 240), style="dim"))
        elif role == "user" and content.startswith("[gek: your history before step"):
            import re as _re

            mm = _re.search(r"before step (\d+)", content)
            step = int(mm.group(1)) if mm else 0
            before, after = self._run.compact_notes.get((self._actor, step), (0, 0))
            sizes = f": {before:,} -> ~{after:,} tokens" if before else ""
            log.write(Rule(f"compacted at step {step}{sizes}", style="cyan"))
            notes = content.split("]", 1)[1].strip() if "]" in content else content
            log.write(Text("notes the agent wrote for itself:", style="bold cyan"))
            log.write(Text(_clip_block(notes, self._full, 1500), style="cyan"))
            log.write(Rule(style="cyan"))
        elif role == "user":
            log.write(Text(f"{ts} HARNESS -> {self._actor}", style="bold cyan"))
            log.write(Text(_clip_block(content, self._full, 2500)))
        elif role == "assistant":
            log.write(Text(f"{ts} {self._actor}", style="bold magenta"))
            if content.strip():
                log.write(Text(_clip_block(content, self._full, 4000)))
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                log.write(Text(f"  -> {fn.get('name')}", style="bold yellow"))
                log.write(Text(_clip_block(_pretty_args(fn.get("arguments", "")), self._full, 1500),
                               style="yellow"))
        elif role == "tool":
            bad = content.startswith("ERROR")
            log.write(Text(f"{ts}   <- result", style="bold red" if bad else "bold green"))
            log.write(Text(_clip_block(content, self._full, 1500), style="red" if bad else ""))
        else:
            log.write(Text(f"{ts} {role}: {content[:2000]}"))
        log.write(Text(""))

    def action_toggle_full(self) -> None:
        self._full = not self._full
        self._render_all()

    def action_toggle_system(self) -> None:
        self._show_sys = not self._show_sys
        self._render_all()

    def action_toggle_tools(self) -> None:
        self._show_tools = not self._show_tools
        self._head()

    def action_top(self) -> None:
        self.query_one("#t-log", RichLog).scroll_home(animate=False)

    def action_bottom(self) -> None:
        self.query_one("#t-log", RichLog).scroll_end(animate=False)


# ------------------------------------------------------------------- prompts
class PromptScreen(ModalScreen):
    """One line of input. Enter sends, Esc cancels. Dismisses with the text or None."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]
    CSS = """
    PromptScreen { align: center middle; }
    #box { width: 90; height: auto; border: round $warning; padding: 1 2; background: $surface; }
    #box Label { margin-bottom: 1; }
    """

    def __init__(self, title: str, placeholder: str = "", value: str = ""):
        super().__init__()
        self._title, self._placeholder, self._value = title, placeholder, value

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self._title)
            yield Input(value=self._value, placeholder=self._placeholder, id="text")

    def on_mount(self) -> None:
        self.query_one("#text", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ------------------------------------------------------------------------ app
class GekTUI(App):
    TITLE = "gek"
    CSS = """
    #side { width: 30; border-right: solid $primary-darken-2; }
    #side-title { padding: 0 1; color: $text-muted; }
    #stats { height: 4; padding: 0 1; }
    #wakes { height: 2; padding: 0 1; color: $text-muted; }
    #plots { height: 16; }
    #plots PlotextPlot { width: 1fr; }
    #bottom { height: 1fr; }
    #agents { width: 1fr; }
    #feed { width: 1fr; border-left: solid $primary-darken-2; }
    /* phones and narrow windows: no sidebar (switch swarms with ctrl+p), one graph, panels stacked */
    .-narrow #side { display: none; }
    .-narrow #stats { height: auto; }
    .-narrow #wakes { display: none; }
    .-narrow #plots { height: 12; }
    .-narrow #p_alive, .-narrow #p_commits { display: none; }
    .-narrow #bottom { layout: vertical; }
    .-narrow #agents { height: 1fr; width: 100%; }
    .-narrow #feed { height: 1fr; width: 100%; border-left: none; border-top: solid $primary-darken-2; }
    """
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (100, "-wide")]
    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("[", "prev_run", "older run", show=False),
        Binding("]", "next_run", "newer run", show=False),
        Binding("r", "rescan", "rescan", show=False),
        Binding("enter", "noop", "transcript", show=True),
        Binding("i", "inbox", "inbox"),
        Binding("s", "spawn", "spawn"),
        Binding("k", "kill_agent", "kill"),
        Binding("p", "pause_toggle", "pause/unpause"),
        Binding("w", "wake", "wake", show=False),
        Binding("m", "mode_toggle", "mode"),
        Binding("o", "roles", "roles"),
        Binding("c", "config", "config"),
        Binding("n", "new_swarm", "new swarm"),
        Binding("R", "start_run", "run"),
        Binding("P", "push", "push", show=False),
        Binding("H", "halt", "halt"),
        Binding("L", "plugins", "plugins"),
        Binding("M", "mcp", "mcp"),
        Binding("U", "resume_review", "resume"),
    ]

    # -------------------------------------------------- control plane
    def get_system_commands(self, screen):  # the command palette (ctrl+p) lists every action
        from textual.app import SystemCommand

        yield from super().get_system_commands(screen)
        for title, help_, action in [
            ("New swarm", "create a swarm (name, goal, seed) and start it", self.action_new_swarm),
            ("Run swarm", "start the selected swarm in the background", self.action_start_run),
            ("Halt swarm", "stop the selected swarm; live agents are killed", self.action_halt),
            ("Pause / unpause", "freeze or unfreeze the swarm", self.action_pause_toggle),
            ("Resume swarm", "pick up after a halt or crash: salvage work, brief the orchestrator", self.action_resume),
            ("Resume swarm + respawn", "resume, and respawn interrupted agents onto their branches",
             lambda: self.action_resume(respawn=True)),
            ("Spawn agent", "spawn an agent into a role", self.action_spawn),
            ("Kill agent", "kill the agent selected in the table", self.action_kill_agent),
            ("Inbox message", "message the orchestrator (wakes it)", self.action_inbox),
            ("Wake orchestrator", "wake it now", self.action_wake),
            ("Switch mode", "llm <-> human orchestration", self.action_mode_toggle),
            ("Roles", "view and edit role templates", self.action_roles),
            ("Config", "view and change settings live", self.action_config),
            ("Push", "push swarm work to a git remote", self.action_push),
            ("Plugins", "installed vs enabled plugins; enable, disable, edit their config", self.action_plugins),
            ("MCP servers", "view, add, enable/disable MCP servers live", self.action_mcp),
            ("Resume (review first)", "see what the last run left behind, then resume", self.action_resume_review),
        ]:
            yield SystemCommand(title, help_, action)
        for name in list_swarms():
            yield SystemCommand(f"Swarm: {name}", "switch to this swarm", lambda n=name: self._switch_to(n))

    def _switch_to(self, name: str) -> None:
        try:
            self.select_swarm(load_swarm(name))
            lv = self.query_one("#swarms", ListView)
            for i, item in enumerate(lv.children):
                if isinstance(item, SwarmItem) and item.cfg.name == name:
                    lv.index = i
        except Exception as e:
            self.notify(str(e), severity="error")

    async def _do(self, control: str, reason: str = "", payload: str = "") -> None:
        from .cli import do_control

        try:
            self.notify(await do_control(self.cfg, "human", control, reason, payload))
        except Exception as e:
            self.notify(str(e), severity="error")

    def action_new_swarm(self) -> None:
        from .tui_control import NewSwarmScreen

        def done(res) -> None:
            if res:
                cfg, started = res
                self.notify(f"created {cfg.name}" + (f", {started}" if started else ""))
                self.rescan()
                self._switch_to(cfg.name)

        self.push_screen(NewSwarmScreen(), done)

    def action_resume(self, respawn: bool = False) -> None:
        from .cli import start_detached

        if self.cfg is None:
            return
        try:
            pid = start_detached(self.cfg, extra=["--resume"] + (["--respawn"] if respawn else []))
            self.notify(f"{self.cfg.name} resuming in the background (pid {pid})")
        except Exception as e:
            self.notify(str(e), severity="error")

    def action_plugins(self) -> None:
        from .tui_control import PluginsScreen

        if self.cfg is not None:
            self.push_screen(PluginsScreen(self.cfg, self.cur))

    def action_mcp(self) -> None:
        from .tui_control import McpScreen

        if self.cfg is None:
            self.notify("start the swarm first (R)", severity="warning")
            return
        # the screen saves through do_control, which needs no live swarm: offline it saves for next run
        self.push_screen(McpScreen(self.cfg, self.cur), lambda msg: self.notify(msg) if msg else None)

    def action_resume_review(self) -> None:
        from .tui_control import ResumeScreen

        if self.cfg is None:
            return

        def done(res) -> None:
            if res:
                self.notify(res)

        self.push_screen(ResumeScreen(self.cfg), done)

    def action_start_run(self) -> None:
        from .cli import start_detached

        if self.cfg is None:
            return
        try:
            pid = start_detached(self.cfg)
            self.notify(f"{self.cfg.name} started in the background (pid {pid})")
        except Exception as e:
            self.notify(str(e), severity="error")

    def action_spawn(self) -> None:
        from .tui_control import SpawnScreen

        if self.cfg is None or not self._live():
            self.notify("start the swarm first (R)", severity="warning")
            return

        async def go() -> None:
            from pathlib import Path

            from .gitrepo import ROLES, Repo

            roles = [Path(p).stem for p in await Repo(self.cfg.repo_dir).list_files_at(ROLES, "roles")]
            if not roles:
                self.notify("no roles yet: add one with o (roles)", severity="warning")
                return
            self.push_screen(SpawnScreen(self.cfg, roles),
                             lambda msg: self.notify(msg) if msg else None)

        self.run_worker(go())

    def action_push(self) -> None:
        from .tui_control import PushScreen

        if self.cfg is not None:
            self.push_screen(PushScreen(self.cfg), lambda msg: self.notify(msg) if msg else None)

    def action_roles(self) -> None:
        from .tui_control import RolesScreen

        if self.cfg is not None:
            self.push_screen(RolesScreen(self.cfg))

    def action_config(self) -> None:
        from .tui_control import ConfigScreen

        if self.cfg is not None:
            self.push_screen(ConfigScreen(self.cfg))

    def action_wake(self) -> None:
        if self.cfg is not None:
            self.run_worker(self._do("wake", "woken from the TUI"))

    def action_mode_toggle(self) -> None:
        if self.cfg is None:
            return
        from .config import get_path

        live = self.cur.mode if (self.cur and self._live()) else None
        current = live or str(get_path(self.cfg.raw, "orchestrator.mode") or "llm")
        new = "human" if current == "llm" else "llm"
        self.run_worker(self._do(f"mode {new}", f"switched from the TUI ({current} -> {new})"))

    def action_pause_toggle(self) -> None:
        if self.cfg is None or not self._live():
            self.notify("this swarm isn't running", severity="warning")
            return
        verb = "unpause" if (self.cur and self.cur.paused) else "pause"
        self.run_worker(self._do(verb, f"{verb}d from the TUI"))

    # --------------------------------------------------------- steering
    async def _send(self, control: str | None, text: str) -> str:
        from .cli import send_control
        from .gitrepo import INBOX, Identity, Repo
        from .swarm import _message

        if control:
            sha = await send_control(self.cfg, "human", control, text)
        else:
            sha = await Repo(self.cfg.repo_dir).empty_commit(
                INBOX, _message(text, {"From": "human"}), Identity("human", "human@outside.gek"),
                actor="human")
        return sha[:10]

    def _live(self) -> bool:
        return self.cfg is not None and running_run(self.cfg) is not None

    def action_inbox(self) -> None:
        if self.cfg is None:
            return

        async def done(text: str | None) -> None:
            if text and text.strip():
                sha = await self._send(None, text.strip())
                self.notify(f"sent to {self.cfg.name}'s inbox ({sha})"
                            + ("" if self._live() else "; swarm isn't running, it'll read it next run"))

        self.push_screen(PromptScreen(f"Message to {self.cfg.name}'s orchestrator (wakes it):"), done)

    def action_kill_agent(self) -> None:
        t = self.query_one("#agents", DataTable)
        if self.cfg is None or not t.row_count:
            return
        aid = str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
        a = self.cur.agents.get(aid) if self.cur else None
        if aid == "orchestrator" or a is None or a.status != "alive" or not self._live():
            self.notify(f"{aid} isn't a live agent of a running swarm", severity="warning")
            return

        async def done(reason: str | None) -> None:
            if reason is not None:
                sha = await self._send(f"kill {aid}", reason.strip() or "killed from the TUI")
                self.notify(f"kill {aid} sent ({sha}); it lands within a few seconds")

        self.push_screen(PromptScreen(f"Kill {aid}? Reason (goes on the record), Enter to confirm, Esc to cancel:"),
                         done)

    def action_halt(self) -> None:
        if not self._live():
            self.notify("this swarm isn't running", severity="warning")
            return

        async def done(reason: str | None) -> None:
            if reason is not None:
                sha = await self._send("halt", reason.strip() or "halted from the TUI")
                self.notify(f"halt sent ({sha}); live agents will be killed")

        self.push_screen(PromptScreen(f"Halt {self.cfg.name}? Reason, Enter to confirm, Esc to cancel:"), done)

    def action_noop(self) -> None:  # Enter is handled by the agents table (RowSelected)
        pass

    def __init__(self, initial: Config | None = None):
        super().__init__()
        self.initial = initial
        self.cfg: Config | None = None
        self.runs: list[Path] = []
        self.run_idx = -1
        self.follow_latest = True
        self.cur: RunData | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="side"):
                yield Static(f"swarms in {gek_home()}", id="side-title")
                yield ListView(id="swarms")
            with Vertical():
                yield Static(id="stats")
                yield Static(id="wakes")
                with Horizontal(id="plots"):
                    yield TokensPlot(id="p_tokens")
                    yield AlivePlot(id="p_alive")
                    yield CommitsPlot(id="p_commits")
                with Horizontal(id="bottom"):
                    yield DataTable(id="agents", zebra_stripes=True, cursor_type="row")
                    yield RichLog(id="feed", wrap=True, highlight=False, max_lines=2000)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#agents", DataTable)
        t.add_columns("agent", "status", "calls", "prompt k", "cached", "out k", "context", "last tool")
        self.rescan()
        self.set_interval(TICK_S, self.tick)

    # ------------------------------------------------------------ swarms
    def _swarm_entries(self) -> list[tuple[str, Config]]:
        out: list[tuple[str, Config]] = []
        if self.initial is not None and self.initial.portable:
            out.append((self.initial.name, self.initial))
        for n in list_swarms():
            try:
                out.append((n, load_swarm(n)))
            except Exception:
                continue
        return out

    def _label(self, name: str, cfg: Config) -> str:
        mark = "*" if name == current_swarm() else " "
        live = " [running]" if running_run(cfg) else ""
        short = name if len(name) <= 22 else "..." + name[-19:]
        return f"{mark} {short}{live}"

    def rescan(self) -> None:
        lv = self.query_one("#swarms", ListView)
        selected = self.cfg.path if self.cfg else (self.initial.path if self.initial else None)
        lv.clear()
        entries = self._swarm_entries()
        idx = 0
        for i, (name, cfg) in enumerate(entries):
            lv.append(SwarmItem(self._label(name, cfg), cfg))
            if selected is not None and cfg.path == selected:
                idx = i
        if entries:
            lv.index = idx
            if self.cfg is None or self.cfg.path != entries[idx][1].path:
                self.select_swarm(entries[idx][1])
        else:
            self.query_one("#stats", Static).update(
                "no swarms yet: `gek init NAME --goal \"...\"` then `gek run`")

    def action_rescan(self) -> None:
        self.rescan()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        if isinstance(item, SwarmItem) and (self.cfg is None or item.cfg.path != self.cfg.path):
            self.select_swarm(item.cfg)

    def select_swarm(self, cfg: Config) -> None:
        self.cfg = cfg
        self.runs = runs_of(cfg)
        self.follow_latest = True
        self.open_run(len(self.runs) - 1)

    # --------------------------------------------------------------- runs
    def open_run(self, idx: int) -> None:
        feed = self.query_one("#feed", RichLog)
        feed.clear()
        self.run_idx = idx
        if not self.runs or idx < 0:
            self.cur = None
        else:
            self.cur = RunData(self.runs[idx] / "events.jsonl")
            self.cur.poll()
            for line in self.cur.feed:
                feed.write(Text(line))
            self.cur.new_feed.clear()
        self.redraw()

    def action_prev_run(self) -> None:
        if self.runs and self.run_idx > 0:
            self.follow_latest = False
            self.open_run(self.run_idx - 1)

    def action_next_run(self) -> None:
        if self.runs and self.run_idx < len(self.runs) - 1:
            self.open_run(self.run_idx + 1)
            self.follow_latest = self.run_idx == len(self.runs) - 1

    # ------------------------------------------------------------- update
    def tick(self) -> None:
        if self.cfg is None:
            return
        runs = runs_of(self.cfg)
        if len(runs) != len(self.runs):
            self.runs = runs
            if self.follow_latest:
                self.open_run(len(runs) - 1)
                return
        changed = self.cur.poll() if self.cur else False
        live = self.cur is not None and self.cur.ended is None and running_run(self.cfg) == self.cur.path.parent.name
        if changed or live:
            if self.cur and self.cur.new_feed:
                feed = self.query_one("#feed", RichLog)
                for line in self.cur.new_feed:
                    feed.write(Text(line))
                self.cur.new_feed.clear()
            self.redraw()

    def redraw(self) -> None:
        run = self.cur
        stats = self.query_one("#stats", Static)
        if self.cfg is None:
            return
        if run is None:
            stats.update(f"[b]{escape(self.cfg.name)}[/b]: no runs yet. goal: {escape(str(self.cfg.raw.get('goal', '')))}")
        else:
            live = running_run(self.cfg) == run.path.parent.name and run.ended is None
            reason = " ".join((run.ended or "").split())
            reason = reason if len(reason) <= 70 else reason[:67] + "..."
            status = ("[green]RUNNING[/green]" + (" [yellow]PAUSED[/yellow]" if run.paused else "")
                      + (" [cyan]human mode[/cyan]" if run.mode == "human" else "")) if live else (
                f"ended: {escape(reason)}" if run.ended else "[yellow]no run_end (crashed or killed)[/yellow]")
            if not live and self.run_idx == len(self.runs) - 1:
                status += "  [dim](U: review and resume)[/dim]"
            sb = run.sandbox
            if sb.get("using") == "bwrap":
                status += f"  [green]sandbox: bwrap[/green] [dim](net {'on' if sb.get('network') else 'off'})[/dim]"
            elif sb and sb.get("shell_allowed") and sb.get("mode") != "off":
                status += "  [b yellow]NOT SANDBOXED[/b yellow]"
            if run.plugins:
                bad = sum(1 for p in run.plugins.values() if p.get("status") in ("disabled", "load error"))
                status += f"  plugins {len(run.plugins)}" + (f" [red]({bad} failed)[/red]" if bad else "") + " [dim](L)[/dim]"
            end = time.time() if live else (run.t_last or 0)
            mins = (end - (run.t0 or end)) / 60
            total_agents = sum(1 for a in run.agents if a != "orchestrator")
            cached_pct = f"{100 * run.cached / max(run.tokens, 1):.0f}% cached" if run.cached else "no cache data"
            goal = " ".join(run.goal.split())
            goal = goal if len(goal) <= 110 else goal[:107] + "..."
            stats.update(
                f"[b]{escape(self.cfg.name)} [/b]| run {run.path.parent.name} ({self.run_idx + 1}/{len(self.runs)})  {status}\n"
                f"{mins:.1f} min  wakes {run.wakes}  agents {run.alive} live / {total_agents} total  "
                f"tokens {run.tokens:,} ({cached_pct})  cost ${run.cost:.4f}  commits {len(run.commits)}  "
                f"merges {len(run.merges)}\n[dim]goal: {escape(goal)}[/dim]")
        if run:
            live_now = running_run(self.cfg) == run.path.parent.name and run.ended is None
            run.view_end = max(run.t_last or 0, time.time()) if live_now else run.t_last
        self.query_one("#p_tokens", TokensPlot).draw(run)
        self.query_one("#p_alive", AlivePlot).draw(run)
        self.query_one("#p_commits", CommitsPlot).draw(run)
        wk = self.query_one("#wakes", Static)
        if run and run.wake_list:
            line = Text("wakes: ", style="dim")
            shown = run.wake_list[-8:]
            for wt, n, why in shown:
                m = (wt - (run.t0 or wt)) / 60
                why = why if len(why) <= 48 else why[:45] + "..."
                line.append(f"#{n} {m:.1f}m ", style="bold dark_orange")
                line.append(why + "   ", style="dim")
            if len(run.wake_list) > 8:
                line.append(f"(+{len(run.wake_list) - 8} earlier)", style="dim")
            wk.update(line)
        else:
            wk.update(Text("wakes: -", style="dim"))
        t = self.query_one("#agents", DataTable)
        keep = t.cursor_row
        t.clear()
        if run:
            rows = sorted(run.agents.values(), key=lambda a: (a.id != "orchestrator", a.status != "alive", a.id))
            for a in rows:
                cached = f"{100 * a.cached / max(a.prompt, 1):.0f}%" if a.cached else "-"
                status = Text(a.status, style="green" if a.status == "alive" else
                              ("red" if a.status in ("killed", "failed", "error") else ""))
                t.add_row(a.id, status, str(a.calls), f"{a.prompt / 1000:.0f}", cached,
                          f"{a.completion / 1000:.1f}", self._context_cell(run, a), a.last, key=a.id)
            if t.row_count:
                t.move_cursor(row=min(keep, t.row_count - 1), animate=False)

    def _context_cell(self, run: RunData, a: AgentRow) -> Text:
        """Latest prompt size against the compaction threshold, and how many compactions so far."""
        if a.id == "orchestrator" or not a.last_prompt:
            return Text("-", style="dim")
        at = run.role_compact_at.get(a.role) or int(
            (self.cfg.raw.get("agents") or {}).get("compact_at_tokens") or 0) if self.cfg else 0
        times = f" x{a.compactions}" if a.compactions else ""
        if at <= 0:
            return Text(f"{a.last_prompt / 1000:.0f}k (no compaction){times}", style="dim")
        frac = a.last_prompt / at
        style = "bold yellow" if frac >= 0.8 and a.status == "alive" else ("cyan" if a.compactions else "")
        return Text(f"{a.last_prompt / 1000:.0f}k/{at / 1000:.0f}k{times}", style=style)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "agents" or self.cur is None or event.row_key is None:
            return
        aid = str(event.row_key.value)
        path = self.cur.path.parent / "agents" / (aid.replace("/", "__") + ".jsonl")
        self.push_screen(TranscriptScreen(path, aid, self.cur, self.cfg))


def run_tui(initial: Config | None) -> None:
    GekTUI(initial).run()
