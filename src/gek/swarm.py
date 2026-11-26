"""The swarm runtime: roles, agents, the orchestrator's wake/sleep loop."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import secrets
import sys
import time
from typing import Any

from . import __version__
from .agent import Agent, AgentHandle
from .config import Config
from .gitrepo import CHATTER, INBOX, MAIN, ROLES, Identity, Repo
from .llm import LLM, BudgetExceeded
from .logs import RunLog
from .prompts import orchestrator_prior
from .config import (flatten, get_path, global_config_path, is_hot, mcp_edit, mcp_entry,
                       mcp_servers, parse_value, persist_key, set_path)
from .roles import Role, validate
from .tools import B, I, S, ToolBox, arr, obj

ORCH = "orchestrator"


def mcp_entry_summary(servers: Any) -> dict[str, Any]:
    """What gets logged about mcp_servers: names, enabled flags, the command -- never env values."""
    out: dict[str, Any] = {}
    for name, spec in mcp_servers(servers, only_enabled=False).items():
        e = mcp_entry(spec)
        out[name] = {"enabled": e["enabled"], "command": os.path.basename(e.get("command", "")),
                     "env_vars": sorted((e.get("env") or {}))}  # names only, never values
    return out


def _trailers(t: dict[str, str]) -> str:
    return "".join(f"{k}: {v}\n" for k, v in t.items() if v)


def _message(message: str, trailers: dict[str, str] | None) -> str:
    """Subject = first line (cut at 72), body = the full text if it didn't fit, then trailers."""
    msg = message.strip() or "(empty)"
    first = msg.splitlines()[0]
    subject = first if len(first) <= 72 else first[:69] + "..."
    body = "" if msg == subject else msg
    parts = [subject] + ([body] if body else []) + ([_trailers(trailers)] if trailers and _trailers(trailers) else [])
    return "\n\n".join(p.rstrip() for p in parts) + "\n"


class Swarm:
    def __init__(self, cfg: Config, log: RunLog, resume: Any = None, respawn: bool = False,
                 sandbox: Any = None):
        from .sandbox import detect

        self.sandbox = sandbox if sandbox is not None else detect(cfg)
        self.cfg = cfg
        self.log = log
        self.llm = LLM(cfg, log)
        self.repo = Repo(cfg.repo_dir, log)
        self.roles: dict[str, Role] = {}
        self.agents: dict[str, AgentHandle] = {}
        self.live: dict[str, Any] = {}  # agent id -> Agent (transcripts, for plugins)
        from .plugin import PluginHost

        self.plugins = PluginHost(self)
        self.goal: str = cfg.raw.get("goal", "")
        self.wake = asyncio.Event()
        self.wake_reasons: list[str] = []
        self.halted: str | None = None
        self.last_wake: float = 0.0
        self.wakes = 0
        self.started: float = 0.0
        self.orch_ident = Identity("orchestrator", "orchestrator@swarm.gek")
        self.resumed = asyncio.Event()  # cleared while paused; agents wait on it between steps
        self.resumed.set()
        self.outside: list[str] = []  # outside actions since the orchestrator's last wake
        self._cfg_mtime = self._mtime()
        # Bumped whenever mcp_servers (or the ceiling that gates it) changes. Agents hold the
        # value they connected at and reconnect their MCP bridge between steps when it moves,
        # in their own task (anyio cancel scopes require open/close in one task).
        self.mcp_generation = 0
        self.resume = resume  # resume.ResumeInfo when this run picks up an earlier one
        self.respawn = respawn
        if resume is not None:
            self.llm.prior_tokens = int(resume.tokens or 0)

    # ------------------------------------------------------- control plane
    @property
    def mode(self) -> str:
        return str(self.cfg.section("orchestrator").get("mode") or "llm")

    @property
    def paused(self) -> bool:
        return not self.resumed.is_set()

    def _mtime(self) -> float:
        """Newest config file this swarm reads: its own, and for a home swarm the shared
        GEK_HOME/config.json, so a global edit reaches _reload_config_file like a hand edit."""
        try:
            mtime = self.cfg.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if not self.cfg.portable:
            try:
                mtime = max(mtime, global_config_path().stat().st_mtime)
            except OSError:
                pass
        return mtime

    async def apply_config(self, key: str, value: Any, author: str, persist: bool = True) -> str:
        """Change one live setting. Returns a result line; raises ValueError when refused."""
        if not is_hot(key):
            raise ValueError(f"{key} needs a restart (not a live setting)")
        old = get_path(self.cfg.raw, key)
        if old == value:
            return f"{key} already {json.dumps(value)}"
        set_path(self.cfg.raw, key, value)
        if persist:
            persist_key(self.cfg.path, key, value)
            self._cfg_mtime = self._mtime()
        self.log.event("config", author, key=key, old=old, new=value)
        line = f"{key}: {json.dumps(old)} -> {json.dumps(value)}"
        if key == "goal":
            self.goal = str(value)
        if key.startswith("ceiling"):
            bad = []
            for r in self.roles.values():
                errs = validate(r, self.cfg.raw.get("ceiling", {}), self.cfg.tool_sources())
                if errs:
                    bad.append(f"{r.name} ({'; '.join(errs)})")
            if bad:
                line += ". Roles now above the ceiling (live agents keep their powers): " + ", ".join(bad)
                self.log.event("note", "harness", roles_above_ceiling=bad)
        if key == "mcp_servers" or key.startswith("mcp_servers.") or key.startswith("ceiling"):
            self.mcp_generation += 1
            self.log.event("mcp", "harness", generation=self.mcp_generation, key=key,
                           servers=mcp_entry_summary(self.cfg.raw.get("mcp_servers")), author=author)
        if key == "orchestrator.mode" or (key == "orchestrator" and isinstance(value, dict)):
            self.log.event("mode", author, mode=self.mode)
            if self.mode == "llm":
                self.request_wake(f"{author} switched to llm mode")
        return line

    async def _reload_config_file(self) -> None:
        """config.json changed on disk (a hand edit): apply the hot keys that differ."""
        from .config import load_config

        self._cfg_mtime = self._mtime()
        try:
            fresh = load_config(self.cfg.path, name=self.cfg.name, portable=self.cfg.portable).raw
        except Exception as e:
            self.log.event("note", "file", error=f"config.json changed but can't be read: {e}")
            return
        old_flat, new_flat = flatten(self.cfg.raw), flatten(fresh)
        changed = {k for k in set(old_flat) | set(new_flat) if old_flat.get(k) != new_flat.get(k)}
        # mcp_servers as a whole: a hand edit of one leaf (or a removed server) is one live change,
        # so agents reconnect once. Applying leaves would also leave a half-applied server behind.
        if any(k == "mcp_servers" or k.startswith("mcp_servers.") for k in changed):
            try:
                line = await self.apply_config("mcp_servers", fresh.get("mcp_servers", {}), "file",
                                               persist=False)
                self.outside.append(f"config.json edited by hand: {line}")
                changed = {k for k in changed if not (k == "mcp_servers" or k.startswith("mcp_servers."))}
            except ValueError as e:
                self.log.event("config", "file", key="mcp_servers", refused=str(e))
        for key in sorted(changed):
            try:
                line = await self.apply_config(key, get_path(fresh, key), "file", persist=False)
                self.outside.append(f"config.json edited by hand: {line}")
            except ValueError as e:
                self.log.event("config", "file", key=key, refused=str(e))

    # -------------------------------------------------------------- helpers
    async def post(self, ref: str, actor: str, message: str, trailers: dict[str, str] | None = None) -> str:
        text = _message(message, trailers)
        sha = await self.repo.empty_commit(ref, text, Identity.for_actor(actor), actor=actor)
        if ref == INBOX and actor != ORCH:
            self.log.event("inbox", actor, message=message, sha=sha)
            self.request_wake(f"inbox from {actor}")
        return sha

    def request_wake(self, reason: str) -> None:
        self.wake_reasons.append(reason)
        self.wake.set()

    def alive(self) -> list[AgentHandle]:
        return [a for a in self.agents.values() if a.status in ("starting", "running")]

    def describe_agents(self) -> str:
        rows = []
        for a in self.agents.values():
            age = round(time.time() - a.spawned_at)
            pa = self.llm.per_actor.get(a.id, {})
            cap_s = a.role.max_steps or self.cfg.section("agents").get("max_steps", 60)
            tok = int(pa.get("tokens", 0))
            tok_s = f"{tok // 1000}k" + (f"/{a.role.max_tokens // 1000}k" if a.role.max_tokens else "")
            rows.append(f"{a.id:<24} {a.status:<9} steps={a.steps}/{cap_s} tokens={tok_s} "
                        f"age={age}s branch={a.branch}"
                        + (f" summary={a.summary[:120]!r}" if a.summary else ""))
        return "\n".join(rows) or "(no agents this run)"

    def describe_roles(self) -> str:
        if not self.roles:
            return "(no roles yet)"
        return "\n\n".join(r.to_json() for r in self.roles.values())

    # ---------------------------------------------------------------- roles
    async def load_roles(self) -> None:
        for path in await self.repo.list_files_at(ROLES, "roles"):
            text = await self.repo.read_file_at(ROLES, path)
            try:
                role = Role.from_dict(json.loads(text or "{}"))
                self.roles[role.name] = role
            except Exception as e:
                self.log.event("note", "harness", error=f"bad role file {path}: {e}")
        self.log.event("note", "harness", loaded_roles=sorted(self.roles))

    async def write_role(self, data: dict[str, Any], actor: str = ORCH) -> str:
        role = Role.from_dict(data)
        errs = validate(role, self.cfg.raw.get("ceiling", {}), self.cfg.tool_sources())
        if errs:
            self.log.event("role_rejected", actor, role=data, errors=errs)
            return "ERROR: role rejected:\n- " + "\n- ".join(errs)
        verb = "update" if role.name in self.roles else "create"
        self.roles[role.name] = role
        await self.repo.write_files_commit(ROLES, {f"roles/{role.name}.json": role.to_json()},
                                           f"{actor}: {verb} role {role.name}\n\nRole-Template: {role.name}\n",
                                           self.orch_ident, actor=actor)
        self.log.event("role_written", actor, verb=verb, role=json.loads(role.to_json()))
        return f"role {role.name} {verb}d"

    async def retire_role(self, name: str, reason: str, actor: str = ORCH) -> str:
        if name not in self.roles:
            return f"ERROR: no role {name}"
        del self.roles[name]
        await self.repo.write_files_commit(ROLES, {f"roles/{name}.json": None},
                                           f"{actor}: retire role {name}\n\n{reason}\n\nRetired: {name}\n",
                                           self.orch_ident, actor=actor)
        self.log.event("role_retired", actor, role=name, reason=reason)
        return f"role {name} retired (live agents of it keep running until they end or are killed)"

    # --------------------------------------------------------------- agents
    async def spawn(self, role_name: str, note: str = "", resume_branch: str = "",
                    actor: str = ORCH) -> str:
        role = self.roles.get(role_name)
        if not role:
            return f"ERROR: no role {role_name}. Roles: {', '.join(self.roles) or 'none'}"
        cap = self.cfg.section("agents").get("max_alive", 6)
        if len(self.alive()) >= cap:
            return f"ERROR: {cap} agents already alive (agents.max_alive); kill or wait"
        hexid = secrets.token_hex(2)
        while f"{role_name}/{hexid}" in self.agents:
            hexid = secrets.token_hex(2)
        aid = f"{role_name}/{hexid}"
        branch = f"role/{role_name}/{hexid}"
        wt = self.cfg.worktrees_dir / f"{role_name}-{hexid}"
        base = MAIN
        if resume_branch:
            if not await self.repo.tip(resume_branch):
                return f"ERROR: no branch {resume_branch} to resume"
            base = resume_branch
        await self.repo.add_worktree(wt, branch, base=base)
        h = AgentHandle(id=aid, role=role, branch=branch, worktree=wt, note=note)
        self.agents[aid] = h
        agent = Agent(self, h)
        self.live[aid] = agent
        h.task = asyncio.create_task(agent.run(), name=aid)
        self.log.event("agent_spawn", actor, id=aid, role=role_name, branch=branch, base=base,
                       worktree=str(wt), note=note)
        await self.post(CHATTER, actor, f"spawned {aid} on {branch} (from {base})" + (f": {note}" if note else ""),
                        {"Spawned": aid, "Role": role_name, "Branch": branch})
        return f"spawned {aid} on {branch} (from {base})"

    async def kill(self, agent_id: str, killer: str, reason: str) -> str:
        h = self.agents.get(agent_id)
        if not h or h.status not in ("starting", "running"):
            return f"ERROR: no live agent {agent_id}"
        self.log.event("agent_kill", killer, target=agent_id, reason=reason)
        h.summary = f"killed by {killer}: {reason}"
        if h.task:
            h.task.cancel()
        await self.post(CHATTER, killer, f"killed {agent_id}: {reason}",
                        {"Kills": agent_id, "Reason": reason.splitlines()[0][:200] if reason else ""})
        return f"killed {agent_id}"

    def on_agent_exit(self, h: AgentHandle, how: str) -> None:
        if self.cfg.section("orchestrator").get("wake_on_agent_exit", True) and how != "killed":
            self.request_wake(f"{h.id} exited ({how})")

    # --------------------------------------------------------- orchestrator
    def _orch_tools(self, state: dict[str, Any]) -> ToolBox:
        box = ToolBox(ORCH, self.log, self.cfg.section("agents").get("max_tool_output_chars", 12000))

        async def read_log(refs: list[str] | None = None, since_minutes: int | None = None, limit: int = 100) -> str:
            since = time.time() - since_minutes * 60 if since_minutes else None
            return await self.repo.log_text(refs or None, since=since, limit=min(limit, 400))

        async def list_roles() -> str:
            return self.describe_roles()

        async def write_role(name: str, mandate: str, reads: list[str] | None = None,
                             writes: list[str] | None = None, powers: dict[str, Any] | None = None,
                             temperament: str = "", model: str = "", max_steps: int = 0,
                             max_tokens: int = 0, compact_at: int = 0) -> str:
            return await self.write_role(dict(name=name, mandate=mandate, reads=reads, writes=writes,
                                              powers=powers or {}, temperament=temperament,
                                              model=model, max_steps=max_steps, max_tokens=max_tokens,
                                              compact_at=compact_at))

        async def retire_role(name: str, reason: str = "") -> str:
            return await self.retire_role(name, reason)

        async def list_agents() -> str:
            return self.describe_agents()

        async def spawn(role: str, count: int = 1, note: str = "", resume_branch: str = "") -> str:
            outs = []
            for _ in range(max(1, min(int(count), 20))):
                outs.append(await self.spawn(role, note=note, resume_branch=resume_branch))
                if resume_branch:
                    break
            return "\n".join(outs)

        async def kill(agent_id: str, reason: str) -> str:
            return await self.kill(agent_id, ORCH, reason)

        async def list_files(ref: str = MAIN, path: str = "") -> str:
            return "\n".join(await self.repo.list_files_at(ref, path)) or "(empty)"

        async def read_file(path: str, ref: str = MAIN) -> str:
            t = await self.repo.read_file_at(ref, path)
            return t if t is not None else f"ERROR: {path} not found on {ref}"

        async def post(message: str) -> str:
            return "posted " + (await self.post(CHATTER, ORCH, message))[:10]

        async def sleep(seconds: int, reason: str) -> str:
            state["sleep"] = (int(seconds), reason)
            return "ok"

        async def halt(reason: str) -> str:
            state["halt"] = reason
            return "ok, halting the swarm"

        powers_schema = obj(
            shell=B, merge=B, interrupt=B, kill=arr(),
            mcp={"type": "object", "description": "server name -> list of tool name globs",
                 "additionalProperties": arr()})
        box.add("read_log", "Read the git log. refs: branch names (main, swarm/chatter, swarm/roles, "
                "swarm/inbox, role/<role>/<hex>); empty = all.", obj(refs=arr(), since_minutes=I, limit=I), read_log)
        box.add("list_roles", "Show current role templates.", obj(), list_roles)
        box.add("write_role", "Create or replace a role template. Validated against your ceiling.",
                obj(["name", "mandate"], name=S, mandate=S, reads=arr(), writes=arr(),
                    powers=powers_schema, temperament=S, model=S, max_steps=I, max_tokens=I, compact_at=I), write_role)
        box.add("retire_role", "Retire a role template.", obj(["name"], name=S, reason=S), retire_role)
        box.add("list_agents", "List agents spawned this run and their status.", obj(), list_agents)
        box.add("spawn", "Spawn agents into a role. resume_branch starts the new agent from an "
                "existing branch (e.g. a dead agent's) instead of main.",
                obj(["role"], role=S, count=I, note=S, resume_branch=S), spawn)
        box.add("kill", "Kill a live agent, on the record.", obj(["agent_id", "reason"], agent_id=S, reason=S), kill)
        box.add("list_files", "List files on a branch.", obj(ref=S, path=S), list_files)
        box.add("read_file", "Read a file from a branch (default main).", obj(["path"], path=S, ref=S), read_file)
        box.add("post", "Post to swarm/chatter, e.g. notes to future-you or direction for agents.",
                obj(["message"], message=S), post)
        box.add("sleep", "End this wake. seconds = how long until your next check-up; reason = "
                "a note to future-you.", obj(["seconds", "reason"], seconds=I, reason=S), sleep)
        box.add("halt", "Stop the whole swarm (live agents are killed). Only when the goal is done "
                "or impossible.", obj(["reason"], reason=S), halt)
        return box

    async def _orch_wake(self, reasons: list[str]) -> tuple[int, str]:
        ocfg = self.cfg.section("orchestrator")
        since = self.last_wake or None
        self.last_wake = time.time()
        self.wakes += 1
        self.log.event("orch_wake", ORCH, n=self.wakes, reasons=reasons, alive=[a.id for a in self.alive()])
        log_since = await self.repo.log_text(None, since=since, limit=300,
                                             max_chars=ocfg.get("log_chars_per_wake", 30000))
        state: dict[str, Any] = {}
        box = self._orch_tools(state)
        ceiling = {k: v for k, v in self.cfg.raw.get("ceiling", {}).items() if not k.startswith("_")}
        mcp_avail = sorted(k for k in self.cfg.raw.get("mcp_servers", {}) if not k.startswith("_"))
        plugin_tools = self.plugins.tool_catalog()
        aliases = self.cfg.raw.get("models", {}).get("aliases", {})
        acfg = self.cfg.section("agents")
        me = self.llm.per_actor.get(ORCH, {})
        elapsed = (time.time() - self.started) / 60 if self.started else 0
        from .agent import env_notes_text

        notes = env_notes_text(self.cfg)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": orchestrator_prior(self.cfg)},
            {"role": "user", "content": (
                f"Wake #{self.wakes} at {time.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
                f"Why you woke: {', '.join(reasons)}.\n\n"
                f"Goal:\n{self.goal}\n\n"
                + (f"{self.resume.briefing()}\n\n" if self.resume is not None and self.wakes == 1 else "")
                + f"Your ceiling (most you may grant): {json.dumps(ceiling)}\n"
                f"MCP servers available: {mcp_avail or 'none'}\n"
                + (f"Plugins with tools (grant like MCP servers, under powers.mcp): "
                   f"{json.dumps(plugin_tools)}\n" if plugin_tools else "")
                + f"Models: default {self.cfg.raw['models'].get('default')}; aliases for roles: "
                f"{json.dumps({k: v for k, v in aliases.items() if not k.startswith('_')}) or 'none'} "
                f"(aliases that map to the same model behave the same)\n"
                f"Limits:\n"
                f"- max {acfg.get('max_alive')} live agents\n"
                f"- each agent: {acfg.get('max_steps')} steps unless its role sets max_steps; no token cap "
                f"unless its role sets max_tokens; "
                + (f"history compacted past {int(acfg.get('compact_at_tokens') or 0):,} prompt tokens "
                   "(a role's compact_at overrides; -1 = never)" if int(acfg.get('compact_at_tokens') or 0) > 0
                   else "no history compaction unless its role sets compact_at")
                + f"; shell timeout {acfg.get('shell_timeout_s')}s; tool output "
                f"cut at {acfg.get('max_tool_output_chars')} chars; {self.sandbox.describe()}\n"
                f"- you: {ocfg.get('max_steps_per_wake')} steps per wake; sleep between "
                f"{ocfg.get('min_sleep_s')}s and {ocfg.get('max_sleep_s')}s\n"
                f"Spend: {self.llm.total_tokens:,} tokens this run"
                + (f" + {self.llm.prior_tokens:,} in the runs this one resumes" if self.llm.prior_tokens else "")
                + f" of {f'{self.llm.max_tokens_budget:,}' if self.llm.max_tokens_budget else 'unlimited'} "
                f"(${self.llm.total_cost:.4f}); you alone: {int(me.get('tokens', 0)):,} tokens over "
                f"{int(me.get('calls', 0))} calls. Run time {elapsed:.1f} min, wake {self.wakes}.\n"
                + (f"Environment notes (agents see these too):\n{notes}\n" if notes else "")
                + "\n"
                + (("Outside actions since your last wake (a human or another client; authoritative):\n"
                    + "\n".join(f"- {o}" for o in self.outside) + "\n\n") if self.outside else "")
                + f"Role templates:\n{self.describe_roles()}\n\n"
                f"Agents:\n{self.describe_agents()}\n\n"
                f"Git log {'since your last wake' if since else '(everything)'}:\n{log_since}\n")},
        ]
        self.outside = []
        for m in messages:
            self.log.transcript(ORCH, {**m, "wake": self.wakes})
        tools = box.schemas()
        for _ in range(ocfg.get("max_steps_per_wake", 30)):
            msg = await self.llm.chat(ORCH, self.cfg.model(self.cfg.raw["models"]["orchestrator"]),
                                      messages, tools)
            messages.append(msg)
            self.log.transcript(ORCH, {**msg, "wake": self.wakes})
            calls = msg.get("tool_calls") or []
            if not calls:
                nudge = {"role": "user", "content": "Use your tools. End the wake with sleep (or halt)."}
                messages.append(nudge)
                self.log.transcript(ORCH, {**nudge, "wake": self.wakes})
                continue
            for tc in calls:
                out = await box.run(tc["function"]["name"], tc["function"]["arguments"])
                tm = {"role": "tool", "tool_call_id": tc["id"], "content": out}
                messages.append(tm)
                self.log.transcript(ORCH, {**tm, "wake": self.wakes})
            if "halt" in state:
                self.halted = state["halt"]
                return 0, state["halt"]
            if "sleep" in state:
                break
        secs, why = state.get("sleep", (ocfg.get("default_sleep_s", 300), "(ran out of steps without sleeping)"))
        secs = max(ocfg.get("min_sleep_s", 20), min(secs, ocfg.get("max_sleep_s", 3600)))
        await self.post(CHATTER, ORCH, f"sleeping {secs}s: {why}", {"Sleep": str(secs)})
        self.log.event("orch_sleep", ORCH, seconds=secs, reason=why)
        return secs, why

    async def _inbox_watch(self) -> None:
        """Watch swarm/inbox for commits from outside this process (gek inbox, gek kill/halt,
        the TUI, plugins in other processes). Plain messages wake the orchestrator; commits with
        a `Control:` trailer are handled by the harness itself: `kill <agent>` or `halt`."""
        poll = self.cfg.section("orchestrator").get("inbox_poll_s", 5)
        last = await self.repo.tip(INBOX)
        fmt = ("%H%x1f%an%x1f%(trailers:key=Control,valueonly,separator=%x2c)%x1f"
               "%(trailers:key=Reason,valueonly,separator=%x20)%x1f%s%x1f%b%x1e")
        while True:
            await asyncio.sleep(self.cfg.section("orchestrator").get("inbox_poll_s", poll))
            if self._mtime() != self._cfg_mtime:
                await self._reload_config_file()
            tip = await self.repo.tip(INBOX)
            if not tip or tip == last:
                continue
            span = f"{last}..{tip}" if last else tip
            out = await self.repo.git("log", "--reverse", f"--format={fmt}", span, check=False)
            last = tip
            for rec in out.split("\x1e"):
                if not rec.strip():
                    continue
                sha, author, control, reason, subject, body = (rec.strip("\n").split("\x1f") + [""] * 6)[:6]
                if author in self.agents or author in (ORCH, "gek"):
                    continue  # in-process posts already woke us
                control = control.strip()
                if control:
                    # payload = body minus the trailer paragraph
                    paras = [p for p in body.strip().split("\n\n")]
                    if paras and any(l.startswith("Control:") for l in paras[-1].splitlines()):
                        paras = paras[:-1]
                    await self._control(author, control, reason.strip() or subject.strip(),
                                        "\n\n".join(paras).strip(), sha)
                else:
                    message = (subject + ("\n\n" + body if body.strip() else "")).strip()
                    self.log.event("inbox", author, message=message, sha=sha, external=True)
                    self.request_wake(f"inbox from {author}")

    async def apply_mcp(self, arg: str, payload: str, author: str) -> str:
        """`mcp set|rm|enable|disable <name>` from the control plane: apply live (and persist), then
        tell the agents to reconnect. Returns a line for the human."""
        action, _, name = arg.strip().partition(" ")
        action, name = action.strip(), name.strip()
        if action not in ("set", "rm", "enable", "disable") or not name:
            raise ValueError("mcp needs `mcp set|rm|enable|disable <name>`"
                             + (" (set also needs the entry as JSON in the body)" if action == "set" else ""))
        entry = None
        if action == "set":
            if not payload:
                raise ValueError(f"mcp set {name} needs the server entry (JSON) in the body")
            text = payload[payload.find("{"): payload.rfind("}") + 1]
            if not text:
                raise ValueError(f"mcp set {name} needs the server entry (JSON) in the body")
            try:
                entry = json.loads(text)
            except json.JSONDecodeError as e:
                raise ValueError(f"mcp set {name}: the entry isn't valid JSON: {e}")
        servers = mcp_edit(self.cfg.raw.get("mcp_servers"), action, name, entry)
        line = await self.apply_config("mcp_servers", servers, author)
        n = len(mcp_servers(servers))
        line += f" ({n} mcp server{'s' if n != 1 else ''} enabled; live agents reconnect between steps)"
        return line

    async def _control(self, author: str, control: str, reason: str, payload: str, sha: str) -> None:
        """Carry out one Control: command (docs/CONTROL.md)."""
        verb, _, arg = control.strip().partition(" ")
        arg = arg.strip()
        reason = reason or control
        ok, result = True, ""
        try:
            if verb == "kill":
                result = await self.kill(arg, author, reason)
                if not result.startswith("ERROR"):
                    self.request_wake(f"{author} killed {arg}")
            elif verb == "halt":
                self.halted = f"halted by {author}: {reason}"
                self.resumed.set()
                self.request_wake(f"halt by {author}")
                result = "halting"
            elif verb == "wake":
                self.request_wake(f"{author} asked: {reason}")
                result = "wake requested" + (" (human mode: you'll get a notice, no model call)"
                                             if self.mode == "human" else "")
            elif verb == "spawn":
                role, _, rest = arg.partition(" ")
                frm = rest.strip()[5:] if rest.strip().startswith("from=") else ""
                note = payload or (reason if reason != control else "")
                result = await self.spawn(role, note=note, resume_branch=frm, actor=author)
            elif verb == "role" and arg == "set":
                text = payload[payload.find("{"): payload.rfind("}") + 1]
                if not text:
                    raise ValueError("role set needs the template as JSON in the body")
                result = await self.write_role(json.loads(text), actor=author)
            elif verb == "role" and arg.startswith("retire"):
                result = await self.retire_role(arg[6:].strip(), reason, actor=author)
            elif verb == "config" and arg.startswith("set "):
                key = arg[4:].strip()
                if not payload:
                    raise ValueError("config set needs the value (JSON) in the body")
                value = parse_value(payload)
                result = await self.apply_config(key, value, author)
            elif verb == "mcp":
                result = await self.apply_mcp(arg, payload, author)
            elif verb == "mode" and arg in ("llm", "human"):
                result = await self.apply_config("orchestrator.mode", arg, author)
            elif verb == "pause":
                if self.paused:
                    result = "already paused"
                else:
                    self.resumed.clear()
                    self.log.event("pause", author, paused=True, reason=reason)
                    result = "paused: the orchestrator won't wake, agents wait after their current step"
            elif verb in ("unpause", "resume"):  # "resume" kept as an alias
                if not self.paused:
                    result = "not paused"
                else:
                    self.resumed.set()
                    self.log.event("pause", author, paused=False, reason=reason)
                    self.request_wake(f"{author} resumed the swarm")
                    result = "resumed"
            else:
                raise ValueError(f"unknown control {control!r} (see docs/CONTROL.md)")
            if str(result).startswith("ERROR"):
                ok = False
        except Exception as e:
            ok, result = False, f"ERROR: {type(e).__name__}: {e}"
        self.log.event("control", author, verb=verb, arg=arg, reason=reason, sha=sha, ok=ok,
                       result=str(result)[:2000])
        line = (f"{author}: {control}" + (f" ({reason})" if reason and reason != control else "")
                + f" -> {str(result)[:300]}")
        self.outside.append(line)
        if not ok:
            try:
                await self.post(CHATTER, "harness", f"control from {author} failed: {control}: {result}",
                                {"Control-Failed": control})
            except Exception:
                pass

    async def run(self) -> str:
        ocfg = self.cfg.section("orchestrator")
        self.log.write_meta({"run": self.log.run_id, "started": time.time(), "version": __version__,
                             "python": sys.version, "platform": platform.platform(),
                             "config_path": str(self.cfg.path), "config": self.cfg.redacted()})
        self.started = time.time()
        self.log.event("run_start", "harness", version=__version__, goal=self.goal,
                       config=self.cfg.redacted(), platform=platform.platform(), python=sys.version,
                       resume=self.resume.to_dict() if self.resume is not None else None)
        sb = self.sandbox
        self.log.event("sandbox", "harness", using=sb.kind, mode=sb.mode, network=sb.network,
                       reason=sb.reason, warning=sb.warning(),
                       shell_allowed=bool(self.cfg.raw.get("ceiling", {}).get("shell")))
        await self.plugins.start()
        await self.load_roles()
        if self.resume is not None:
            self.log.event("resume", "harness", **self.resume.to_dict())
            if self.respawn:
                for a in self.resume.interrupted:
                    if a.role not in self.roles:
                        self.outside.append(f"could not respawn {a.id}: role {a.role} no longer exists")
                        continue
                    note = (f"You are continuing {a.id}'s work, interrupted when the last run "
                            f"{'crashed' if a.how == 'crashed' else 'stopped'}. Its branch is yours now; "
                            f"read its commits first. Its last state: {a.last or 'unknown'}")
                    res = await self.spawn(a.role, note=note, resume_branch=a.branch, actor="harness")
                    self.outside.append(f"harness (resume --respawn): {a.id} -> {res}")
        watcher = asyncio.create_task(self._inbox_watch())
        reason = "?"
        reasons = ["start"]
        try:
            while not self.halted:
                ocfg = self.cfg.section("orchestrator")  # live config: re-read every loop
                # Clear wake requests *before* the wake, not after: anything that arrives while
                # the orchestrator is awake (an inbox message, an agent exiting) must still
                # cause another wake once this one sleeps.
                self.wake.clear()
                self.wake_reasons = []
                if self.paused:
                    await self.resumed.wait()
                    if self.halted:
                        break
                    reasons = ["resumed"] + reasons
                    self.wake.clear()
                    self.wake_reasons = []
                if self.mode == "human":
                    # no model call: the wake becomes a notice for the human
                    self.last_wake = time.time()
                    self.log.event("human_wake", "harness", reasons=reasons,
                                   outside=self.outside[-20:], alive=[a.id for a in self.alive()])
                    self.outside = self.outside[-50:]  # kept for the model when you hand back
                    await self.wake.wait()
                    if self.halted:
                        break
                    await asyncio.sleep(ocfg.get("debounce_s", 10))
                    reasons = list(dict.fromkeys(self.wake_reasons)) or ["interrupt"]
                    continue
                try:
                    secs, _ = await self._orch_wake(reasons)
                except BudgetExceeded as e:
                    self.halted = f"budget: {e}"
                    self.log.event("budget", "harness", exhausted=True, detail=str(e))
                    break
                except Exception as e:
                    # a failed wake (model down, bad response) is not fatal: sleep and retry
                    secs = ocfg.get("default_sleep_s", 300)
                    self.log.event("note", ORCH, error=f"wake failed: {type(e).__name__}: {e}",
                                   retry_in_s=secs)
                if self.halted:
                    break
                try:
                    ocfg = self.cfg.section("orchestrator")
                    await asyncio.wait_for(self.wake.wait(), timeout=secs)
                    # interrupted: debounce, and never wake twice within min_sleep
                    await asyncio.sleep(ocfg.get("debounce_s", 10))
                    floor = self.last_wake + ocfg.get("min_sleep_s", 20) - time.time()
                    if floor > 0:
                        await asyncio.sleep(floor)
                    reasons = list(dict.fromkeys(self.wake_reasons)) or ["interrupt"]
                except asyncio.TimeoutError:
                    reasons = ["timer"] + list(dict.fromkeys(self.wake_reasons))
            reason = self.halted or "?"
        except (KeyboardInterrupt, asyncio.CancelledError):
            reason = "interrupted (Ctrl-C)"
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)  # let its git subprocess close
            for a in self.alive():
                try:
                    await self.kill(a.id, "harness", f"swarm stopping: {reason}")
                except Exception:
                    pass
            pending = [a.task for a in self.agents.values() if a.task and not a.task.done()]
            if pending:
                await asyncio.wait(pending, timeout=30)
            self.log.event("run_end", "harness", reason=reason, wakes=self.wakes,
                           agents=len(self.agents), tokens=self.llm.total_tokens,
                           tokens_cumulative=self.llm.total_tokens + self.llm.prior_tokens,
                           prompt_tokens=self.llm.prompt_tokens,
                           completion_tokens=self.llm.completion_tokens,
                           cost=round(self.llm.total_cost, 6), calls=self.llm.calls)
            try:
                await self.plugins.stop()  # after run_end, so plugins see it
            except Exception as e:  # noqa: BLE001
                self.log.event("note", "harness", error=f"stopping plugins: {type(e).__name__}: {e}")
        return reason
