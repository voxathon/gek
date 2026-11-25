"""Plugins: plain, unsandboxed Python modules that run inside the harness.

A plugin is a .py file, or a folder with __init__.py, that defines `gek_plugin`: a Plugin
subclass. Only plugins named under "plugins" in config.json load; installed is not enabled.

    "plugins": {
      "notify": {"url": "https://ntfy.sh/my-topic"},
      "mine":   {"enabled": false}
    }

Where a name is looked up, most specific first:

    <swarm folder>/plugins/<name>[.py]    one swarm's plugins
    $GEK_HOME/plugins/<name>[.py]         yours, for every swarm
    the gek.plugins entry point group     pip-installed plugins
    gek/plugins/<name>.py                 bundled with gek

A plugin can:
  - start background work (start), see every event (on_event), clean up (stop)
  - post to the inbox (wakes the orchestrator) or chatter, read the log, read transcripts
  - provide tools. Roles are granted them like MCP tools: powers.mcp (or powers.tools)
    {"<plugin>": ["glob", ...]}, within the ceiling. Agents see them as <plugin>__<tool>.
  - touch anything: ctx.swarm is the live Swarm. Ceiling and grants don't apply to plugin code.

The harness does one thing for you: crash isolation. start/on_event/stop run behind a
per-plugin queue and try/except; a plugin that raises 5 times is disabled for the run.
Each load is logged with the file's path and sha256, so a plugin you didn't put there shows up.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .config import Config, gek_home
from .gitrepo import CHATTER, INBOX
from .tools import Tool, granted

if TYPE_CHECKING:
    from .swarm import Swarm

MAX_ERRORS = 5
BUNDLED = Path(__file__).parent / "plugins"


@dataclass
class Caller:
    """Who called a plugin tool."""
    id: str             # agent id, e.g. coder/ab12
    role: str
    branch: str
    worktree: Path


@dataclass
class PluginTool:
    """A tool a plugin offers. handler(caller, **args) -> str (async)."""
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[str]]


class Plugin:
    """Subclass this; set `gek_plugin = YourPlugin` at module level."""
    name: str = ""

    def __init__(self, ctx: "PluginContext", config: dict[str, Any]):
        self.ctx = ctx
        self.config = config

    async def start(self) -> None:
        """Spawn pollers with asyncio.create_task here. Must return."""

    async def on_event(self, event: dict[str, Any]) -> None:
        """Every events.jsonl record, in order: {ts, t, run, seq, kind, actor, data}."""

    async def stop(self) -> None:
        """The run is ending."""

    def tools(self) -> list[PluginTool]:
        """Tools agents can be granted."""
        return []


class PluginContext:
    """The barebones SDK: conveniences, not a boundary."""

    def __init__(self, swarm: "Swarm", name: str):
        self.swarm = swarm
        self.name = name
        self.actor = f"plugin:{name}"

    @property
    def cfg(self) -> Config:
        return self.swarm.cfg

    async def post_inbox(self, message: str, sender: str | None = None) -> str:
        """A message for the orchestrator; wakes it. `sender` shows in From: (default plugin:<name>)."""
        return await self.swarm.post(INBOX, sender or self.actor, message, {"Plugin": self.name})

    async def post_chatter(self, message: str) -> str:
        return await self.swarm.post(CHATTER, self.actor, message, {"Plugin": self.name})

    async def read_log(self, refs: list[str] | None = None, since: float | None = None,
                       limit: int = 50) -> str:
        return await self.swarm.repo.log_text(refs, since=since, limit=limit)

    def transcript(self, actor: str) -> list[dict[str, Any]]:
        """An agent's messages so far (a copy)."""
        a = getattr(self.swarm, "live", {}).get(actor)
        return [dict(m) for m in a.messages] if a is not None else []

    def log(self, **data: Any) -> None:
        self.swarm.log.event("plugin", self.actor, **data)


# ------------------------------------------------------------------------- discovery
def _hash(path: Path) -> str:
    h = hashlib.sha256()
    files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
    for f in files:
        h.update(f.relative_to(path).as_posix().encode() if path.is_dir() else b"")
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def search_dirs(cfg: Config) -> list[tuple[str, Path]]:
    return [("swarm", cfg.root / "plugins"), ("home", gek_home() / "plugins")]


def find(cfg: Config, name: str) -> tuple[str, Path | None, Any]:
    """(where, path, entry_point). where is swarm, home, entry_point, bundled, or '' if missing."""
    for where, d in search_dirs(cfg):
        for cand in (d / f"{name}.py", d / name / "__init__.py"):
            if cand.is_file():
                return where, cand, None
    try:
        for ep in metadata.entry_points(group="gek.plugins"):
            if ep.name == name:
                return "entry_point", None, ep
    except Exception:
        pass
    cand = BUNDLED / f"{name}.py"
    if cand.is_file():
        return "bundled", cand, None
    return "", None, None


def installed(cfg: Config) -> dict[str, str]:
    """Every plugin gek can see, name -> where (for `gek plugins`)."""
    out: dict[str, str] = {}
    for p in sorted(BUNDLED.glob("*.py")):
        if p.stem != "__init__":
            out[p.stem] = "bundled"
    try:
        for ep in metadata.entry_points(group="gek.plugins"):
            out[ep.name] = "entry_point"
    except Exception:
        pass
    for where, d in reversed(search_dirs(cfg)):
        if d.is_dir():
            for p in d.iterdir():
                if p.suffix == ".py" or (p / "__init__.py").is_file():
                    out[p.stem if p.suffix == ".py" else p.name] = where
    return out


def load_class(cfg: Config, name: str) -> tuple[type, dict[str, Any]]:
    where, path, ep = find(cfg, name)
    if not where:
        raise LookupError(f"no plugin named {name!r} (looked in {', '.join(str(d) for _, d in search_dirs(cfg))}, "
                          "the gek.plugins entry points, and gek's bundled plugins)")
    info: dict[str, Any] = {"where": where}
    if ep is not None:
        obj = ep.load()
        info["entry_point"] = f"{ep.value}"
        mod = sys.modules.get(obj.__module__) if inspect.isclass(obj) else obj
        f = getattr(mod, "__file__", None)
        if f:
            info.update(path=f, sha256=_hash(Path(f)))
        cls = obj if inspect.isclass(obj) else getattr(obj, "gek_plugin", None)
    else:
        assert path is not None
        root = path.parent if path.name == "__init__.py" else path
        info.update(path=str(root), sha256=_hash(root))
        modname = f"gek_plugin_{where}_{name}".replace("-", "_")
        spec = importlib.util.spec_from_file_location(
            modname, path, submodule_search_locations=[str(path.parent)] if path.name == "__init__.py" else None)
        if spec is None or spec.loader is None:
            raise ImportError(f"can't import {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[modname] = mod
        spec.loader.exec_module(mod)
        cls = getattr(mod, "gek_plugin", None)
    if not (inspect.isclass(cls) and issubclass(cls, Plugin)):
        raise TypeError(f"plugin {name!r} must set gek_plugin to a gek.plugin.Plugin subclass")
    return cls, info


# ------------------------------------------------------------------------- the host
class _Running:
    def __init__(self, name: str, plugin: Plugin, info: dict[str, Any]):
        self.name = name
        self.plugin = plugin
        self.info = info
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.errors = 0
        self.disabled = ""
        self.events = 0
        self.tools: list[PluginTool] = []


class PluginHost:
    """Loads the configured plugins for one run, feeds them events, hands their tools out."""

    def __init__(self, swarm: "Swarm", serve_only: bool = False):
        self.swarm = swarm
        self.serve_only = serve_only  # gek mcp-serve: tools only, no start/events
        self.running: dict[str, _Running] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def configured(self) -> dict[str, dict[str, Any]]:
        raw = self.swarm.cfg.raw.get("plugins") or {}
        out = {}
        for name, conf in raw.items():
            if name.startswith("_"):
                continue
            conf = conf if isinstance(conf, dict) else {}
            if conf.get("enabled", True) is False:
                continue
            out[name] = conf
        return out

    async def start(self) -> None:
        log = self.swarm.log
        self._loop = asyncio.get_running_loop()
        for name, conf in self.configured().items():
            try:
                cls, info = load_class(self.swarm.cfg, name)
                plugin = cls(PluginContext(self.swarm, name), conf)
                plugin.name = name
                r = _Running(name, plugin, info)
                r.tools = list(plugin.tools() or [])
            except Exception as e:  # noqa: BLE001
                log.event("plugin", f"plugin:{name}", loaded=False, error=f"{type(e).__name__}: {e}")
                continue
            self.running[name] = r
            log.event("plugin", f"plugin:{name}", loaded=True, **info, tools=[t.name for t in r.tools])
            if self.serve_only:
                continue
            try:
                await asyncio.wait_for(plugin.start(), timeout=30)
            except Exception as e:  # noqa: BLE001
                self._fail(r, "start", e)
            r.task = asyncio.create_task(self._pump(r), name=f"plugin:{name}")
        if self.running and not self.serve_only:
            log.listeners.append(self._on_event)

    def _on_event(self, rec: dict[str, Any]) -> None:
        loop = self._loop
        for r in self.running.values():
            if r.disabled or r.task is None:
                continue
            if loop is None:
                continue
            try:
                running_here = asyncio.get_running_loop() is loop
            except RuntimeError:
                running_here = False
            if running_here:
                r.queue.put_nowait(rec)
            else:
                loop.call_soon_threadsafe(r.queue.put_nowait, rec)

    async def _pump(self, r: _Running) -> None:
        while True:
            rec = await r.queue.get()
            if rec is None:
                return
            if r.disabled:
                continue
            # a plugin's own events would echo back forever if it logs from on_event
            if rec.get("actor") == f"plugin:{r.name}" and rec.get("kind") == "plugin":
                continue
            r.events += 1
            try:
                await r.plugin.on_event(rec)
            except Exception as e:  # noqa: BLE001
                self._fail(r, "on_event", e)

    def _fail(self, r: _Running, where: str, e: BaseException) -> None:
        r.errors += 1
        err = f"{where}: {type(e).__name__}: {e}"
        if r.errors >= MAX_ERRORS and not r.disabled:
            r.disabled = f"raised {r.errors} times; last {err}"
            self.swarm.log.event("plugin", f"plugin:{r.name}", disabled=True, reason=r.disabled)
        else:
            self.swarm.log.event("plugin", f"plugin:{r.name}", error=err, errors=r.errors)

    async def stop(self) -> None:
        log = self.swarm.log
        if self._on_event in log.listeners:
            log.listeners.remove(self._on_event)
        for r in self.running.values():
            if r.task is not None:
                r.queue.put_nowait(None)
                try:
                    await asyncio.wait_for(r.task, timeout=10)
                except Exception:  # noqa: BLE001
                    r.task.cancel()
            if self.serve_only:
                continue
            try:
                await asyncio.wait_for(r.plugin.stop(), timeout=15)
            except Exception as e:  # noqa: BLE001
                self._fail(r, "stop", e)

    # ------------------------------------------------------------------ tools
    def tool_catalog(self) -> dict[str, list[str]]:
        """plugin -> tool names, for the orchestrator's briefing."""
        return {n: [t.name for t in r.tools] for n, r in self.running.items() if r.tools and not r.disabled}

    def tools_for(self, agent: Any) -> list[Tool]:
        """The plugin tools an agent's role was granted, as registry Tools."""
        grants = agent.h.role.powers.mcp
        out: list[Tool] = []
        for name, r in self.running.items():
            globs = grants.get(name)
            if not globs or r.disabled:
                continue
            caller = Caller(agent.h.id, agent.h.role.name, agent.h.branch, agent.h.worktree)
            for t in r.tools:
                if granted(globs, t.name):
                    out.append(Tool(f"{name}__{t.name}"[:64], t.description, t.parameters,
                                    self._wrap(r, t, caller), source=name))
        return out

    def _wrap(self, r: _Running, t: PluginTool, caller: Caller):
        async def h(**args: Any) -> str:
            if r.disabled:
                return f"ERROR: plugin {r.name} is disabled ({r.disabled})"
            try:
                return str(await t.handler(caller, **args))
            except TypeError:
                raise  # bad arguments: the registry reports those
            except Exception as e:  # noqa: BLE001
                self._fail(r, f"tool {t.name}", e)
                return f"ERROR: {type(e).__name__}: {e}"
        return h
