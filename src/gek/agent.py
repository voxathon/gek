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

# What an agent's shell command inherits from the harness environment. Nothing else gets through:
# os.environ holds the API key and the harness's own settings.
SHELL_ENV_NAMES = frozenset((
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TEMP", "TMP",
    "SYSTEMROOT", "COMSPEC", "PATHEXT", "VIRTUAL_ENV",
))
SHELL_ENV_PREFIXES = ("LC_", "UV_")


def env_notes_text(cfg) -> str:
    """agents.env_notes: a string or a list of lines every agent (and the orchestrator) sees."""
    notes = cfg.section("agents").get("env_notes") or ""
    if isinstance(notes, list):
        notes = "\n".join(f"- {n}" for n in notes)
    return str(notes).strip()


def safe_env(cfg) -> dict[str, str]:
    """The harness env, reduced to the allowlist plus agents.env_passthrough, minus credentials.
    Used for agent shell commands and as the base env of MCP servers agents connect to."""
    extra = cfg.section("agents").get("env_passthrough") or []
    env = {k: v for k, v in os.environ.items()
           if k in SHELL_ENV_NAMES or k.startswith(SHELL_ENV_PREFIXES) or k in extra}
    return strip_credentials(cfg, env)


def strip_credentials(cfg, env: dict[str, str]) -> dict[str, str]:
    api = cfg.section("api")
    for name in (api.get("api_base_env") or "GEK_API_BASE", api.get("api_key_env") or "GEK_API_KEY"):
        env.pop(name, None)  # last, so no rule can let a credential back in
    return env


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
        self.compactions = 0
        self._compact_min = 0  # don't try again below this prompt size (after a failure or a big tail)

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
        for t in self.mcp.listed:
            b.add_tool(t)
        host = getattr(self.swarm, "plugins", None)
        if host is not None:
            for t in host.tools_for(self):
                b.add_tool(t)
        self.swarm.log.event("tools", self.h.id, sources=b.sources())

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

    def _shell_env(self) -> dict[str, str]:
        """The environment for a run-tool command: an allowlist of the harness env plus the values
        gek sets itself, and never the API credentials."""
        env = safe_env(self.swarm.cfg)
        # shell git commits get the agent's identity too (otherwise they show up as the OS user)
        env.update(GIT_AUTHOR_NAME=self.ident.name, GIT_AUTHOR_EMAIL=self.ident.email,
                   GIT_COMMITTER_NAME=self.ident.name, GIT_COMMITTER_EMAIL=self.ident.email,
                   GEK_AGENT=self.h.id, GEK_ROLE=self.h.role.name, GEK_BRANCH=self.h.branch)
        return strip_credentials(self.swarm.cfg, env)

    async def t_run(self, command: str, timeout_s: int | None = None) -> str:
        limit = min(timeout_s or 10**9, self.swarm.cfg.section("agents").get("shell_timeout_s", 180))
        env = self._shell_env()
        sb = self.swarm.sandbox
        if sb.active:
            from .sandbox import hidden_roots

            argv = sb.argv(command, Path(self.h.worktree).resolve(), self.swarm.cfg.repo_dir.resolve(),
                           hidden_roots(self.swarm.cfg), branch=self.h.branch)
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=sb.env(env))
        else:
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

    # ----------------------------------------------------------------- compaction
    def _tail_start(self, keep: int) -> int | None:
        """Index where the kept recent history starts: roughly the last `keep` messages, moved
        back to an assistant message so no tool result loses the call it answers. None if
        there's nothing old enough to compact."""
        i = max(2, len(self.messages) - keep)
        while i > 2 and self.messages[i].get("role") != "assistant":
            i -= 1
        return i if i > 3 else None

    @staticmethod
    def _render(msgs: list[dict[str, Any]], max_chars: int = 120_000) -> str:
        parts = []
        for m in msgs:
            role = m.get("role")
            if role == "assistant":
                if m.get("content"):
                    parts.append(f"YOU: {m['content'][:2000]}")
                for tc in m.get("tool_calls") or []:
                    parts.append(f"YOU CALLED {tc['function']['name']}({tc['function']['arguments'][:600]})")
            elif role == "tool":
                parts.append(f"RESULT: {m.get('content', '')[:1500]}")
            else:
                parts.append(f"{str(role).upper()}: {str(m.get('content', ''))[:3000]}")
        text = "\n\n".join(parts)
        return text if len(text) <= max_chars else "...(earliest part cut)...\n" + text[-max_chars:]

    async def _maybe_compact(self, model: str) -> None:
        """Past agents.compact_at_tokens of prompt, replace the older history with a summary the
        agent writes itself. Costs one cache miss and one extra call; saves resending the whole
        history on every later step."""
        cfg = self.swarm.cfg.section("agents")
        at = int(self.h.role.compact_at or cfg.get("compact_at_tokens", 0) or 0)
        if at <= 0:
            return
        size = self.swarm.llm.last_prompt_tokens(self.h.id)
        if not size:  # provider didn't say: estimate ~4 chars per token
            size = sum(len(json.dumps(m, default=str)) for m in self.messages) // 4
        if size < max(at, self._compact_min):
            return
        cut = self._tail_start(int(cfg.get("compact_keep_recent", 8)))
        if cut is None:
            return
        old = self.messages[2:cut]
        ask = [self.messages[0], {"role": "user", "content": (
            "Your conversation is getting long, so gek is compacting it. Below is your history so far "
            "(after your spawn message). Write the notes you'll continue from; the older history will be "
            "replaced by them. Be concrete and complete, no padding:\n"
            "1. What you were asked to do, in a line.\n"
            "2. Done so far: commits (sha and what), files written or changed, decisions made.\n"
            "3. Current state: what works, what's failing (exact errors), what's uncommitted.\n"
            "4. Next steps, in order.\n"
            "5. Gotchas: commands that worked, paths, things that didn't work and why.\n\n"
            f"HISTORY:\n{self._render(old)}")}]
        try:
            msg = await self.swarm.llm.chat(self.h.id, model, ask, None)
            summary = (msg.get("content") or "").strip()
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001
            self.swarm.log.event("compact", self.h.id, ok=False, error=f"{type(e).__name__}: {e}",
                                 step=self.h.steps)
            self._elide(force=True)
            self._compact_min = size + at // 2  # try again once it's grown, not every step
            return
        if not summary:
            self.swarm.log.event("compact", self.h.id, ok=False, error="empty summary", step=self.h.steps)
            self._elide(force=True)
            self._compact_min = size + at // 2
            return
        note = {"role": "user", "content": (
            f"[gek: your history before step {self.h.steps} was compacted into these notes, which you "
            f"wrote. Files and commits are as they were; use read_file / read_log to check details.]\n\n"
            f"{summary}")}
        self.messages = self.messages[:2] + [note] + self.messages[cut:]
        self.compactions += 1
        after = sum(len(json.dumps(m, default=str)) for m in self.messages) // 4
        # a kept tail that's already near the threshold would otherwise compact every step
        self._compact_min = after + at // 2
        self.swarm.log.transcript(self.h.id, note)
        self.swarm.log.event("compact", self.h.id, ok=True, step=self.h.steps, before_tokens=size,
                             after_tokens_est=after, dropped_messages=len(old), summary=summary,
                             n=self.compactions)

    def _elide(self, force: bool = False) -> None:
        total = sum(len(json.dumps(m, default=str)) for m in self.messages)
        if total <= CONTEXT_SOFT_LIMIT and not force:
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
            await self.mcp.connect(h.role.powers.mcp, sw.cfg.raw.get("mcp_servers", {}),
                                   base_env=safe_env(sw.cfg))
            self._build_tools()
            h.status = "running"
            model = sw.cfg.model(h.role.model or None)
            max_tokens = h.role.max_tokens or 0
            limits = (f"{max_steps} steps"
                      + (f", {max_tokens:,} tokens" if max_tokens else "")
                      + f"; shell commands time out after {cfg.get('shell_timeout_s', 180)}s"
                      + (f"; {sw.sandbox.describe()}" if h.role.powers.shell else "")
                      + f"; tool output is cut at {cfg.get('max_tool_output_chars', 12000):,} chars")
            compact_at = int(h.role.compact_at or cfg.get("compact_at_tokens", 0) or 0)
            if compact_at > 0:
                limits += (f"; past ~{compact_at:,} prompt tokens your older history is replaced by notes "
                           "you write, so commit often and keep your chatter posts informative")
            self._record({"role": "system", "content": agent_system(
                h.role, h.id, h.branch, sw.goal, limits=limits, env_notes=env_notes_text(sw.cfg))})
            chatter = await sw.repo.log_text([CHATTER], limit=30, max_chars=8000)
            mainlog = await sw.repo.log_text([MAIN], limit=10, max_chars=3000)
            self._record({"role": "user", "content": (
                f"You have just been spawned. Note from the orchestrator: {h.note or '(none)'}\n\n"
                f"Recent chatter:\n{chatter}\n\nRecent main:\n{mainlog}\n\nBegin.")})
            tools = self.box.schemas()
            while True:
                # caps are re-read every step, so live config changes apply to running agents
                cfg = sw.cfg.section("agents")
                every = max(1, int(cfg.get("status_every_steps", 5)))
                max_steps = h.role.max_steps or cfg.get("max_steps", 60)
                if h.steps >= max_steps:
                    break
                await sw.resumed.wait()  # paused swarms hold here, between steps
                used = sw.llm.actor_tokens(h.id)
                if max_tokens and used >= max_tokens:
                    how = "token_cap"
                    h.summary = f"stopped at the role's token cap ({used:,} of {max_tokens:,})"
                    break
                h.steps += 1
                await self._maybe_compact(model)
                self._elide()
                msg = await sw.llm.chat(h.id, model, self.messages, tools)
                self._record(msg)
                calls = msg.get("tool_calls") or []
                if not calls:
                    self._record({"role": "user", "content": (
                        "Keep going with tools. If you are done, commit and call finish.")})
                    continue
                results = []
                for tc in calls:
                    out = await self.box.run(tc["function"]["name"], tc["function"]["arguments"])
                    results.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
                    self._shrink_written(tc)
                # status line: every few steps, and every step once near a limit
                used = sw.llm.actor_tokens(h.id)
                near = (h.steps >= 0.8 * max_steps) or bool(max_tokens and used >= 0.8 * max_tokens)
                if self.finished is None and (h.steps % every == 0 or near):
                    line = (f"\n\n[gek: step {h.steps}/{max_steps}, {used:,} tokens used"
                            + (f" of {max_tokens:,}" if max_tokens else "")
                            + (". Close to a limit: commit and finish soon." if near else "") + "]")
                    results[-1]["content"] += line
                for r in results:
                    self._record(r)
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
            if how == "max_steps" and not h.summary:
                h.summary = f"stopped at the step cap ({max_steps})"
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
