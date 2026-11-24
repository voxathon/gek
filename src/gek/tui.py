"""`gek tui`: watch swarms. Reads the run logs (events.jsonl) directly, so it works on a
live run, a finished one, or one from four months ago.

Keys: up/down pick a swarm, [ and ] step through runs, Enter on an agent opens its
transcript (f full/short, s system prompt, esc back), q quits.
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
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, ListItem, ListView, RichLog, Static
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
    new_feed: list[str] = field(default_factory=list)
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
        if k == "run_start":
            self.goal = d.get("goal", "")
        elif k == "run_end":
            self.ended = d.get("reason", "?")
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
    return sorted(p for p in cfg.logs_dir.iterdir() if (p / "events.jsonl").exists())


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
        plt.title("tokens (k)   | = orchestrator wake")
        plt.xlabel("minutes")
        if run and run.token_series:
            pts = list(run.token_series)
            if run.view_end and run.view_end > pts[-1][0]:
                pts.append((run.view_end, pts[-1][1], pts[-1][2]))  # flat until now
            xs = [_mins(run, t) for t, _, _ in pts]
            _wake_lines(plt, run)
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
    ]
    CSS = """
    #t-head { height: 2; padding: 0 1; background: $boost; }
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

    def action_top(self) -> None:
        self.query_one("#t-log", RichLog).scroll_home(animate=False)

    def action_bottom(self) -> None:
        self.query_one("#t-log", RichLog).scroll_end(animate=False)


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
    """
    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("[", "prev_run", "older run"),
        Binding("]", "next_run", "newer run"),
        Binding("r", "rescan", "rescan swarms"),
        Binding("enter", "noop", "agent transcript", show=True),
    ]

    def action_noop(self) -> None:  # Enter is handled by the agents table (RowSelected)
        pass

    def __init__(self, initial: Config | None = None):
        super().__init__()
        self.initial = initial
        self.cfg: Config | None = None
        self.runs: list[Path] = []
        self.run_idx = -1
        self.follow_latest = True
        self.run: RunData | None = None

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
                    yield RichLog(id="feed", wrap=False, highlight=False, max_lines=2000)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#agents", DataTable)
        t.add_columns("agent", "status", "calls", "prompt k", "cached", "out k", "last tool")
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
            self.run = None
        else:
            self.run = RunData(self.runs[idx] / "events.jsonl")
            self.run.poll()
            for line in self.run.feed:
                feed.write(Text(line))
            self.run.new_feed.clear()
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
        changed = self.run.poll() if self.run else False
        live = self.run is not None and self.run.ended is None and running_run(self.cfg) == self.run.path.parent.name
        if changed or live:
            if self.run and self.run.new_feed:
                feed = self.query_one("#feed", RichLog)
                for line in self.run.new_feed:
                    feed.write(Text(line))
                self.run.new_feed.clear()
            self.redraw()

    def redraw(self) -> None:
        run = self.run
        stats = self.query_one("#stats", Static)
        if self.cfg is None:
            return
        if run is None:
            stats.update(f"[b]{escape(self.cfg.name)}[/b]: no runs yet. goal: {escape(str(self.cfg.raw.get('goal', '')))}")
        else:
            live = running_run(self.cfg) == run.path.parent.name and run.ended is None
            reason = " ".join((run.ended or "").split())
            reason = reason if len(reason) <= 70 else reason[:67] + "..."
            status = "[green]RUNNING[/green]" if live else (
                f"ended: {escape(reason)}" if run.ended else "[yellow]no run_end (crashed or killed)[/yellow]")
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
                          f"{a.completion / 1000:.1f}", a.last, key=a.id)
            if t.row_count:
                t.move_cursor(row=min(keep, t.row_count - 1), animate=False)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self.run is None or event.row_key is None:
            return
        aid = str(event.row_key.value)
        path = self.run.path.parent / "agents" / (aid.replace("/", "__") + ".jsonl")
        self.push_screen(TranscriptScreen(path, aid, self.run, self.cfg))


def run_tui(initial: Config | None) -> None:
    GekTUI(initial).run()
