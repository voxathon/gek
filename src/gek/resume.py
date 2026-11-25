"""`gek resume`: pick a swarm back up after a halt, Ctrl-C, budget stop or crash.

What a dead run leaves behind, and what happens to it:
  - a stale run.lock (crash)            -> detected (pid gone) and cleared
  - worktrees with uncommitted work     -> salvaged: committed onto the agent's branch, then removed
  - agents that were alive at the end   -> listed for the orchestrator (or respawned with --respawn)
  - tokens spent                        -> carried into the budget unless --fresh-budget
  - how it ended, the orchestrator's    -> put in the first wake's briefing
    last note to itself
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .gitrepo import Identity, Repo

SALVAGE_IDENT = Identity("gek", "harness@swarm.gek")


def pid_alive(pid: int | None) -> bool:
    """Is this process still running? Works on Linux, macOS and Windows. Unknown pid = assume alive."""
    if not pid:
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False  # no such process (or not ours to see; a gek run is ours)
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Interrupted:
    id: str
    role: str
    branch: str
    steps: int = 0
    last: str = ""       # its last summary or status
    how: str = "crashed"  # crashed (no exit recorded) or killed (stopped with the swarm)


@dataclass
class ResumeInfo:
    run: str | None = None
    ended: str = "never ran"
    crashed: bool = False
    tokens: int = 0              # cumulative over the run chain
    cost: float = 0.0
    last_note: str = ""          # the orchestrator's last sleep reason
    interrupted: list[Interrupted] = field(default_factory=list)
    salvaged: list[str] = field(default_factory=list)
    stale_lock: bool = False

    def briefing(self) -> str:
        lines = [f"This run RESUMES the previous one ({self.run}), which "
                 + ("CRASHED (no clean shutdown)." if self.crashed else f"ended: {self.ended}.")]
        if self.last_note:
            lines.append(f"Your last note to yourself before it ended: {self.last_note}")
        if self.interrupted:
            lines.append("Agents that were mid-work when it ended (their branches survive; "
                         "spawn with resume_branch to continue one):")
            for a in self.interrupted:
                lines.append(f"- {a.id} ({a.how}, {a.steps} steps) on {a.branch}: {a.last or 'no summary'}")
        else:
            lines.append("No agents were mid-work when it ended.")
        if self.salvaged:
            lines.append("Uncommitted work found in their worktrees was salvaged as commits:")
            lines += [f"- {s}" for s in self.salvaged]
        lines.append(f"Tokens spent before this run: {self.tokens:,}.")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"run": self.run, "ended": self.ended, "crashed": self.crashed, "tokens": self.tokens,
                "cost": self.cost, "last_note": self.last_note, "salvaged": self.salvaged,
                "stale_lock": self.stale_lock,
                "interrupted": [a.__dict__ for a in self.interrupted]}


def last_run_dir(cfg: Config) -> Path | None:
    from .logs import list_runs

    runs = list_runs(cfg.logs_dir)
    return runs[-1] if runs else None


def read_resume_info(cfg: Config) -> ResumeInfo:
    """What the last run left behind, from its events.jsonl."""
    info = ResumeInfo()
    run = last_run_dir(cfg)
    if run is None:
        return info
    info.run = run.name
    spawns: dict[str, dict] = {}
    exits: dict[str, dict] = {}
    end = None
    tokens = 0
    prior = 0
    cost = 0.0
    last_tool: dict[str, str] = {}
    with open(run / "events.jsonl", encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue  # a crash can leave a torn last line
            k, d = e.get("kind"), e.get("data", {})
            if k == "run_start":
                prior = int((d.get("resume") or {}).get("tokens", 0) or 0)
            elif k == "agent_spawn":
                spawns[d.get("id")] = d
            elif k == "agent_exit":
                exits[e.get("actor")] = d
            elif k == "llm_response":
                tokens += int((d.get("usage") or {}).get("total_tokens", 0) or 0)
                cost += float(d.get("cost") or 0)
            elif k == "orch_sleep":
                info.last_note = str(d.get("reason") or "")
            elif k == "tool_result" and str(d.get("output", "")).find("[gek: step") >= 0:
                out = str(d["output"])
                last_tool[e.get("actor")] = out[out.find("[gek: step"):][:120]
            elif k == "run_end":
                end = d
    info.tokens = int((end or {}).get("tokens_cumulative") or (prior + tokens))
    info.cost = cost
    if end is None:
        info.crashed, info.ended = True, "crashed (no run_end)"
    else:
        info.ended = str(end.get("reason") or "?")
    for aid, sp in spawns.items():
        ex = exits.get(aid)
        stopped_with_swarm = bool(ex) and ex.get("how") == "killed" and \
            "killed by harness: swarm stopping" in str(ex.get("summary", ""))
        if ex is None or stopped_with_swarm:
            info.interrupted.append(Interrupted(
                id=aid, role=sp.get("role", aid.split("/")[0]), branch=sp.get("branch", ""),
                steps=int((ex or {}).get("steps") or 0),
                last=str((ex or {}).get("summary") or last_tool.get(aid, "")),
                how="killed" if ex else "crashed"))
    return info


async def salvage_worktrees(cfg: Config, repo: Repo) -> list[str]:
    """Commit any uncommitted work left in agent worktrees (a crash skips their cleanup) onto
    the agent's branch, then remove the worktrees. Returns one line per salvage."""
    out: list[str] = []
    wt_root = cfg.worktrees_dir
    if not wt_root.exists():
        await repo.git("worktree", "prune", check=False)
        return out
    for wt in sorted(p for p in wt_root.iterdir() if p.is_dir()):
        try:
            branch = (await repo.git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt, check=False)).strip()
            status = await repo.git("status", "--porcelain", "--untracked-files=all", cwd=wt, check=False)
            if status.strip() and branch and branch != "HEAD":
                await repo.git("add", "-A", cwd=wt)
                files = len(status.strip().splitlines())
                await repo.git("commit", "-q", "-m",
                               f"gek: salvaged uncommitted work from {wt.name}\n\n"
                               f"The swarm stopped without this agent cleaning up; gek resume\n"
                               f"committed what was in its worktree.\n\nSalvaged: {files} files\n",
                               cwd=wt, identity=SALVAGE_IDENT)
                sha = (await repo.git("rev-parse", "--short", "HEAD", cwd=wt)).strip()
                out.append(f"{files} files from {wt.name} -> {branch} @ {sha}")
        except Exception as e:  # a broken worktree must not block the resume
            out.append(f"could not salvage {wt.name}: {e}")
        await repo.remove_worktree(wt)
    await repo.git("worktree", "prune", check=False)
    return out
