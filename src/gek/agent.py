"""An agent: one role instance, one worktree, one branch, one tool loop."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .gitrepo import CHATTER, INBOX, MAIN, Identity
from .llm import BudgetExceeded
from .mcp_bridge import MCPBridge
from .prompts import agent_system
from .roles import Role
from .tools import I, S, ToolBox, arr, obj

if TYPE_CHECKING:
    from .swarm import Swarm

CONTEXT_SOFT_LIMIT = 250_000  # chars; older tool outputs get elided past this


@dataclass
class AgentHandle:
    id: str
    role: Role
    branch: str
    worktree: Path
    note: str = ""
    spawned_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = None
    status: str = "starting"  # starting, running, finished, killed, failed
    steps: int = 0
    summary: str = ""


class Agent:
    def __init__(self, swarm: "Swarm", h: AgentHandle):
        self.swarm = swarm
        self.h = h
        self.ident = Identity.for_actor(h.id)
        self.finished: str | None = None
        cfg = swarm.cfg.section("agents")
        self.box = ToolBox(h.id, swarm.log, max_output=cfg.get("max_tool_output_chars", 12000))
        self.mcp = MCPBridge(h.id, swarm.log)
        self.messages: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- paths
    def _path(self, rel: str) -> Path:
        rel = (rel or ".").replace("\\", "/")
        p = (self.h.worktree / rel).resolve()
        root = self.h.worktree.resolve()
        if p != root and root not in p.parents:
            raise ValueError(f"path {rel!r} is outside your worktree")
        if ".git" in p.relative_to(root).parts:
            raise ValueError("the .git entry is off limits")
        return p

    # ---------------------------------------------------------------- tools
    def _build_tools(self) -> None:
        r = self.h.role
        b = self.box
        b.add("read_log", "Read the shared git log. refs: list of branch names "
              "(main, swarm/chatter, swarm/roles, role/<role>/<hex>); empty = all refs. "
              "since_minutes limits to recent commits.",
              obj(refs=arr(), since_minutes=I, limit=I), self.t_read_log)
        b.add("list_files", "List files in your worktree (or under a subdirectory).",
              obj(path=S), self.t_list_files)
        b.add("read_file", "Read a file from your worktree. Use ref to read it from another "
              "branch instead (e.g. main, or another agent's branch).",
              obj(["path"], path=S, ref=S), self.t_read_file)
        if r.writes:
            b.add("write_file", "Write a whole file in your worktree (creates directories). "
                  f"You may only commit under: {', '.join(r.writes)}.",
                  obj(["path", "content"], path=S, content=S), self.t_write_file)
            b.add("commit", "Commit all changes in your worktree to your branch. Fails if you "
                  "changed paths outside your writes scope.",
                  obj(["message"], message=S), self.t_commit)
        if r.powers.shell:
            b.add("run", "Run a shell command in your worktree. Returns exit code and output.",
                  obj(["command"], command=S, timeout_s=I), self.t_run)
        b.add("post", "Post to swarm/chatter (status, handoffs, problems, accusations). "
              "Everyone reads it.", obj(["message"], message=S), self.t_post)
        if r.powers.interrupt:
            b.add("interrupt", "Post to the orchestrator's inbox. This wakes it up; use sparingly.",
                  obj(["message"], message=S), self.t_interrupt)
        if r.powers.merge:
            b.add("merge", "Merge a branch into main. Reports conflicts instead of merging if any.",
                  obj(["branch"], branch=S, message=S), self.t_merge)
            b.add("diff", "Show the diff a branch would bring into main.",
                  obj(["branch"], branch=S), self.t_diff)
        if r.powers.kill:
            b.add("list_agents", "List live agents.", obj(), self.t_list_agents)
            b.add("kill", f"Kill a live agent of a role you may kill ({', '.join(r.powers.kill)}). "
                  "Give a reason; it goes on the record.",
                  obj(["agent_id", "reason"], agent_id=S, reason=S), self.t_kill)
        b.add("finish", "End yourself. Call when your work is done or you are stuck for good. "
              "Commit first: uncommitted work is lost.", obj(["summary"], summary=S), self.t_finish)
        b.extra_schemas = self.mcp.schemas
        b.fallback = self._mcp_call

    async def _mcp_call(self, name: str, args: dict[str, Any]) -> str:
        if name in self.mcp.tools:
            return await self.mcp.call(name, args)
        return f"ERROR: no tool named {name!r}"

    async def t_read_log(self, refs: list[str] | None = None, since_minutes: int | None = None,
                         limit: int = 50) -> str:
        since = time.time() - since_minutes * 60 if since_minutes else None
        return await self.swarm.repo.log_text(refs or None, since=since, limit=min(limit, 300))

    async def t_list_files(self, path: str = "") -> str:
        root = self._path(path)
        out = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for f in filenames:
                if f == ".git":
                    continue
                out.append(str((Path(dirpath) / f).relative_to(self.h.worktree)).replace("\\", "/"))
            if len(out) > 2000:
                out.append("...(more than 2000 files; list a subdirectory)")
                break
        return "\n".join(sorted(out)) or "(empty)"

    async def t_read_file(self, path: str, ref: str = "") -> str:
        if ref:
            text = await self.swarm.repo.read_file_at(ref, path.replace("\\", "/"))
            return text if text is not None else f"ERROR: {path} not found on {ref}"
        p = self._path(path)
        if not p.is_file():
            return f"ERROR: no file {path}"
        return p.read_text(encoding="utf-8", errors="replace")

    async def t_write_file(self, path: str, content: str) -> str:
        p = self._path(path)
        rel = str(p.relative_to(self.h.worktree.resolve())).replace("\\", "/")
        warn = "" if self.h.role.may_write(rel) else (
            f" WARNING: {rel} is outside your writes scope; commit will refuse it.")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return f"wrote {rel} ({len(content)} chars).{warn}"

    async def _changed_paths(self) -> list[str]:
        out = await self.swarm.repo.git("status", "--porcelain=v1", "-z", "--untracked-files=all",
                                        cwd=self.h.worktree, actor=self.h.id)
        items = out.split("\0")
        paths, i = [], 0
        while i < len(items):
            e = items[i]
            if len(e) > 3:
                paths.append(e[3:])
                if e[0] in "RC":
                    i += 1
                    if i < len(items) and items[i]:
                        paths.append(items[i])
            i += 1
        return paths

    async def t_commit(self, message: str) -> str:
        changed = await self._changed_paths()
        if not changed:
            return "nothing to commit"
        bad = [p for p in changed if not self.h.role.may_write(p)]
        if bad:
            return ("ERROR: refusing to commit; these paths are outside your writes scope "
                    f"({', '.join(self.h.role.writes)}): {', '.join(bad[:30])}. "
                    "Revert them (e.g. `git checkout -- <path>` or delete new files) and retry.")
        repo = self.swarm.repo
        await repo.git("add", "-A", cwd=self.h.worktree, actor=self.h.id)
        trailers = f"\n\nRole: {self.h.role.name}\nInstance: {self.h.id.split('/')[-1]}"
        await repo.git("commit", "-q", "-m", message.strip() + trailers, cwd=self.h.worktree,
                       identity=self.ident, actor=self.h.id)
        sha = (await repo.git("rev-parse", "HEAD", cwd=self.h.worktree)).strip()
        self.swarm.log.event("commit", self.h.id, ref=self.h.branch, sha=sha, message=message,
                             files=changed)
        return f"committed {sha[:10]} on {self.h.branch} ({len(changed)} paths)"

    async def t_run(self, command: str, timeout_s: int | None = None) -> str:
        limit = min(timeout_s or 10**9, self.swarm.cfg.section("agents").get("shell_timeout_s", 180))
        # shell git commits get the agent's identity too (otherwise they show up as the OS user)
        env = dict(os.environ)
        env.update(GIT_AUTHOR_NAME=self.ident.name, GIT_AUTHOR_EMAIL=self.ident.email,
                   GIT_COMMITTER_NAME=self.ident.name, GIT_COMMITTER_EMAIL=self.ident.email,
                   GEK_AGENT=self.h.id, GEK_ROLE=self.h.role.name, GEK_BRANCH=self.h.branch)
        proc = await asyncio.create_subprocess_shell(
            command, cwd=str(self.h.worktree), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=env)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=limit)
        except asyncio.TimeoutError:
            proc.kill()
            return f"ERROR: timed out after {limit}s"
        return f"exit {proc.returncode}\n{out.decode('utf-8', 'replace')}"

    async def t_post(self, message: str) -> str:
        sha = await self.swarm.post(CHATTER, self.h.id, message,
                                    {"Role": self.h.role.name, "Instance": self.h.id.split("/")[-1],
                                     "Branch": self.h.branch})
        return f"posted {sha[:10]}"

    async def t_interrupt(self, message: str) -> str:
        sha = await self.swarm.post(INBOX, self.h.id, message, {"Role": self.h.role.name})
        return f"sent to inbox {sha[:10]}"

    async def t_merge(self, branch: str, message: str = "") -> str:
        ok, res = await self.swarm.repo.merge_into_main(
            branch, message or f"merge {branch}\n\nMerged-By: {self.h.id}", self.ident, self.h.id)
        return res if ok else "ERROR: " + res

    async def t_diff(self, branch: str) -> str:
        return await self.swarm.repo.git("diff", f"refs/heads/{MAIN}...refs/heads/{branch}",
                                         check=False, actor=self.h.id) or "(no diff)"

    async def t_list_agents(self) -> str:
        return self.swarm.describe_agents()

    async def t_kill(self, agent_id: str, reason: str) -> str:
        target = self.swarm.agents.get(agent_id)
        if not target or target.status not in ("starting", "running"):
            return f"ERROR: no live agent {agent_id}"
        if not self.h.role.may_kill(target.role.name):
            return f"ERROR: you may not kill role {target.role.name}"
        if agent_id == self.h.id:
            return "ERROR: use finish to end yourself"
        await self.swarm.kill(agent_id, self.h.id, reason)
        return f"killed {agent_id}"

    async def t_finish(self, summary: str) -> str:
        self.finished = summary
        return "ok, ending"

    # ----------------------------------------------------------------- loop
    def _record(self, msg: dict[str, Any]) -> None:
        self.messages.append(msg)
        self.swarm.log.transcript(self.h.id, msg)

    def _shrink_written(self, tc: dict[str, Any]) -> None:
        """After write_file runs, drop the file body from the history we resend. The file is on
        disk; resending it every step was ~30% of a builder's prompt tokens. Done once, right
        away, so the history prefix stays stable for provider-side prompt caching.
        (The transcript log already has the full call.)"""
        if tc["function"]["name"] != "write_file":
            return
        try:
            args = json.loads(tc["function"]["arguments"] or "{}")
        except Exception:
            return
        content = args.get("content")
        if isinstance(content, str) and len(content) > 300:
            args["content"] = (f"<{len(content)} chars written to {args.get('path')}; "
                               "use read_file if you need it again>")
            tc["function"]["arguments"] = json.dumps(args)

    def _elide(self) -> None:
        total = sum(len(json.dumps(m, default=str)) for m in self.messages)
        if total <= CONTEXT_SOFT_LIMIT:
            return
        for m in self.messages[2:-12]:
            if m.get("role") == "tool" and len(m.get("content", "")) > 200:
                m["content"] = m["content"][:200] + "...(elided to save context; re-run the tool if needed)"

    async def run(self) -> None:
        sw = self.swarm
        h = self.h
        cfg = sw.cfg.section("agents")
        max_steps = h.role.max_steps or cfg.get("max_steps", 60)
        how = "max_steps"
        try:
            await self.mcp.connect(h.role.powers.mcp, sw.cfg.raw.get("mcp_servers", {}))
            self._build_tools()
            h.status = "running"
            model = sw.cfg.model(h.role.model or None)
            self._record({"role": "system", "content": agent_system(h.role, h.id, h.branch, sw.goal)})
            chatter = await sw.repo.log_text([CHATTER], limit=30, max_chars=8000)
            mainlog = await sw.repo.log_text([MAIN], limit=10, max_chars=3000)
            self._record({"role": "user", "content": (
                f"You have just been spawned. Note from the orchestrator: {h.note or '(none)'}\n\n"
                f"Recent chatter:\n{chatter}\n\nRecent main:\n{mainlog}\n\nBegin.")})
            tools = self.box.schemas()
            for step in range(max_steps):
                h.steps = step + 1
                self._elide()
                msg = await sw.llm.chat(h.id, model, self.messages, tools)
                self._record(msg)
                calls = msg.get("tool_calls") or []
                if not calls:
                    self._record({"role": "user", "content": (
                        "Keep going with tools. If you are done, commit and call finish.")})
                    continue
                for tc in calls:
                    out = await self.box.run(tc["function"]["name"], tc["function"]["arguments"])
                    self._record({"role": "tool", "tool_call_id": tc["id"], "content": out})
                    self._shrink_written(tc)
                if self.finished is not None:
                    how = "finish"
                    h.summary = self.finished
                    break
        except asyncio.CancelledError:
            # Killed. Clear the cancellation so teardown (MCP close, exit record) runs cleanly;
            # the task then ends normally. Nothing awaits an agent's result.
            how = "killed"
            task = asyncio.current_task()
            if task is not None:
                while task.uncancel():
                    pass
        except BudgetExceeded as e:
            how = "budget"
            h.summary = str(e)
        except Exception as e:
            how = "error"
            h.summary = f"{type(e).__name__}: {e}"
            sw.log.event("note", h.id, error=h.summary)
        finally:
            h.status = {"finish": "finished", "killed": "killed"}.get(how, "failed" if how == "error" else how)
            # MCP sessions must close in the task that opened them (anyio cancel scopes)
            try:
                await self.mcp.close()
            except BaseException:
                pass
            await asyncio.shield(self._teardown(how))

    async def _teardown(self, how: str) -> None:
        sw, h = self.swarm, self.h
        dirty = []
        try:
            dirty = await self._changed_paths()
        except Exception:
            pass
        sw.log.event("agent_exit", h.id, how=how, steps=h.steps, summary=h.summary,
                     uncommitted_lost=dirty[:50], branch=h.branch,
                     lived_s=round(time.time() - h.spawned_at, 1))
        try:
            await sw.post(CHATTER, h.id, f"exit ({how}): {h.summary or 'no summary'}",
                          {"Role": h.role.name, "Instance": h.id.split("/")[-1],
                           "Branch": h.branch, "Exit": how})
        except Exception as e:
            sw.log.event("note", h.id, error=f"could not post exit: {e}")
        await sw.repo.remove_worktree(h.worktree)
        sw.on_agent_exit(h, how)
