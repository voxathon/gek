"""The swarm runtime: roles, agents, the orchestrator's wake/sleep loop."""

from __future__ import annotations

import asyncio
import json
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
from .prompts import ORCHESTRATOR_PRIOR
from .roles import Role, validate
from .tools import B, I, S, ToolBox, arr, obj

ORCH = "orchestrator"


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
    def __init__(self, cfg: Config, log: RunLog):
        self.cfg = cfg
        self.log = log
        self.llm = LLM(cfg, log)
        self.repo = Repo(cfg.repo_dir, log)
        self.roles: dict[str, Role] = {}
        self.agents: dict[str, AgentHandle] = {}
        self.goal: str = cfg.raw.get("goal", "")
        self.wake = asyncio.Event()
        self.wake_reasons: list[str] = []
        self.halted: str | None = None
        self.last_wake: float = 0.0
        self.wakes = 0
        self.orch_ident = Identity("orchestrator", "orchestrator@swarm.gek")

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
            rows.append(f"{a.id:<24} {a.status:<9} steps={a.steps:<3} age={age}s branch={a.branch}"
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
        errs = validate(role, self.cfg.raw.get("ceiling", {}), self.cfg.raw.get("mcp_servers", {}))
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
                             temperament: str = "", model: str = "", max_steps: int = 0) -> str:
            return await self.write_role(dict(name=name, mandate=mandate, reads=reads, writes=writes,
                                              powers=powers or {}, temperament=temperament,
                                              model=model, max_steps=max_steps))

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
                    powers=powers_schema, temperament=S, model=S, max_steps=I), write_role)
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
        aliases = self.cfg.raw.get("models", {}).get("aliases", {})
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": ORCHESTRATOR_PRIOR},
            {"role": "user", "content": (
                f"Wake #{self.wakes} at {time.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
                f"Why you woke: {', '.join(reasons)}.\n\n"
                f"Goal:\n{self.goal}\n\n"
                f"Your ceiling (most you may grant): {json.dumps(ceiling)}\n"
                f"MCP servers available: {mcp_avail or 'none'}\n"
                f"Model aliases for roles: {sorted(aliases) or 'none'} (blank = default)\n"
                f"Limits: max {self.cfg.section('agents').get('max_alive')} live agents; "
                f"sleep between {ocfg.get('min_sleep_s')}s and {ocfg.get('max_sleep_s')}s.\n"
                f"Tokens used so far: {self.llm.total_tokens}"
                f" of {self.llm.max_tokens_budget or 'unlimited'}.\n\n"
                f"Role templates:\n{self.describe_roles()}\n\n"
                f"Agents:\n{self.describe_agents()}\n\n"
                f"Git log {'since your last wake' if since else '(everything)'}:\n{log_since}\n")},
        ]
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
        poll = self.cfg.section("orchestrator").get("inbox_poll_s", 5)
        last = await self.repo.tip(INBOX)
        while True:
            await asyncio.sleep(poll)
            tip = await self.repo.tip(INBOX)
            if tip and tip != last:
                author = (await self.repo.git("log", "-1", "--format=%an", tip, check=False)).strip()
                swarm_actor = author in self.agents or author in (ORCH, "gek")
                if not swarm_actor:  # in-process posts already woke us
                    msg = await self.repo.git("log", "-1", "--format=%B", tip, check=False)
                    self.log.event("inbox", author, message=msg.strip(), sha=tip, external=True)
                    self.request_wake(f"inbox from {author}")
                last = tip

    async def run(self) -> str:
        ocfg = self.cfg.section("orchestrator")
        self.log.write_meta({"run": self.log.run_id, "started": time.time(), "version": __version__,
                             "python": sys.version, "platform": platform.platform(),
                             "config_path": str(self.cfg.path), "config": self.cfg.redacted()})
        self.log.event("run_start", "harness", version=__version__, goal=self.goal,
                       config=self.cfg.redacted(), platform=platform.platform(), python=sys.version)
        await self.load_roles()
        watcher = asyncio.create_task(self._inbox_watch())
        reason = "?"
        reasons = ["start"]
        try:
            while not self.halted:
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
                self.wake.clear()
                self.wake_reasons = []
                try:
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
                           prompt_tokens=self.llm.prompt_tokens,
                           completion_tokens=self.llm.completion_tokens,
                           cost=round(self.llm.total_cost, 6), calls=self.llm.calls)
        return reason
