"""Git as the shared medium.

Layout (all inside <swarm>/repo.git, a bare repo):
  refs/heads/main                    the work; only roles with `merge` move it
  refs/heads/role/<role>/<hex>       one branch per agent
  refs/heads/swarm/chatter           status/handoffs/kill records, empty commits
  refs/heads/swarm/roles             role templates as roles/<name>.json
  refs/heads/swarm/inbox             messages for the orchestrator

Swarm refs are written with plumbing (hash-object, mktree, commit-tree,
update-ref with the old value) so they never need a worktree.
Agents work in linked worktrees under <swarm>/wt/.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .logs import RunLog

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
CHATTER = "swarm/chatter"
ROLES = "swarm/roles"
INBOX = "swarm/inbox"
MAIN = "main"

LOG_FORMAT = "%x1e%H%x1f%an%x1f%aI%x1f%D%x1f%B"


class GitError(RuntimeError):
    def __init__(self, args: list[str], code: int, out: str, err: str):
        super().__init__(f"git {' '.join(args)} failed ({code}): {err.strip() or out.strip()}")
        self.code, self.out, self.err = code, out, err
        self.args_list = list(args)


@dataclass
class Identity:
    name: str
    email: str

    @classmethod
    def for_actor(cls, actor: str) -> "Identity":
        safe = actor.replace("/", ".")
        return cls(name=actor, email=f"{safe}@swarm.gek")


class Repo:
    def __init__(self, path: Path, log: RunLog | None = None):
        self.path = path
        self.log = log
        self.ref_lock = asyncio.Lock()

    # ------------------------------------------------------------------ core
    async def git(self, *args: str, cwd: Path | None = None, stdin: str | None = None,
                  identity: Identity | None = None, check: bool = True,
                  actor: str = "harness") -> str:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        if identity:
            env.update(GIT_AUTHOR_NAME=identity.name, GIT_AUTHOR_EMAIL=identity.email,
                       GIT_COMMITTER_NAME=identity.name, GIT_COMMITTER_EMAIL=identity.email)
        full = ["git"] + (["--git-dir", str(self.path)] if cwd is None else ["-C", str(cwd)]) + list(args)
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *full, stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        out_b, err_b = await proc.communicate(stdin.encode("utf-8") if stdin is not None else None)
        out = out_b.decode("utf-8", "replace")
        err = err_b.decode("utf-8", "replace")
        if self.log:
            self.log.event("git", actor, args=list(args), cwd=str(cwd) if cwd else None,
                           code=proc.returncode, ms=round((time.monotonic() - t0) * 1000),
                           stderr=err[-2000:] if proc.returncode else "")
        if check and proc.returncode != 0:
            raise GitError(list(args), proc.returncode, out, err)
        return out

    async def git_rc(self, *args: str, actor: str = "harness") -> tuple[int, str]:
        """Like git() but returns (exit code, stdout) instead of raising."""
        try:
            return 0, await self.git(*args, actor=actor)
        except GitError as e:
            return e.code, e.out

    async def tip(self, ref: str) -> str | None:
        out = await self.git("rev-parse", "--verify", "-q", f"refs/heads/{ref}", check=False)
        return out.strip() or None

    # ----------------------------------------------------------------- setup
    @classmethod
    async def init(cls, path: Path, goal: str, seed_from: Path | None, log: RunLog | None) -> "Repo":
        path.parent.mkdir(parents=True, exist_ok=True)
        repo = cls(path, log)
        if seed_from:
            await repo._run_plain("clone", "--bare", str(seed_from), str(path))
            # make sure there is a main branch
            if not await repo.tip(MAIN):
                head = (await repo.git("symbolic-ref", "--short", "HEAD")).strip()
                await repo.git("branch", "-m", head, MAIN)
        else:
            await repo._run_plain("init", "--bare", "-b", MAIN, str(path))
            ident = Identity("gek", "harness@swarm.gek")
            readme = f"# Swarm workspace\n\nGoal:\n\n{goal}\n"
            blob = (await repo.git("hash-object", "-w", "--stdin", stdin=readme)).strip()
            tree = (await repo.git("mktree", stdin=f"100644 blob {blob}\tGOAL.md\n")).strip()
            sha = (await repo.git("commit-tree", tree, "-m", "gek: initial commit", identity=ident)).strip()
            await repo.git("update-ref", "refs/heads/main", sha)
        await repo.git("symbolic-ref", "HEAD", "refs/heads/main")
        await repo.git("config", "core.autocrlf", "false")
        for ref in (CHATTER, ROLES, INBOX):
            if not await repo.tip(ref):
                await repo.empty_commit(ref, f"gek: start {ref}", Identity("gek", "harness@swarm.gek"))
        return repo

    async def _run_plain(self, *args: str) -> None:
        proc = await asyncio.create_subprocess_exec("git", *args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode:
            raise GitError(list(args), proc.returncode, out.decode(), err.decode())

    # ------------------------------------------------------------ swarm refs
    async def empty_commit(self, ref: str, message: str, ident: Identity, actor: str = "harness") -> str:
        """Append an empty-tree commit to a swarm ref (chatter, inbox). Compare-and-swap."""
        async with self.ref_lock:
            old = await self.tip(ref)
            args = ["commit-tree", EMPTY_TREE, "-m", message]
            if old:
                args[2:2] = ["-p", old]
            sha = (await self.git(*args, identity=ident, actor=actor)).strip()
            await self.git("update-ref", f"refs/heads/{ref}", sha, old or "", actor=actor)
        if self.log:
            self.log.event("commit", actor, ref=ref, sha=sha, message=message)
        return sha

    async def write_files_commit(self, ref: str, files: dict[str, str | None], message: str,
                                 ident: Identity, actor: str = "harness") -> str:
        """Commit a set of file changes to a ref without a worktree. None deletes."""
        async with self.ref_lock:
            old = await self.tip(ref)
            entries: dict[str, str] = {}
            if old:
                for line in (await self.git("ls-tree", "-r", old)).splitlines():
                    meta, p = line.split("\t", 1)
                    entries[p] = meta
            for p, content in files.items():
                if content is None:
                    entries.pop(p, None)
                else:
                    blob = (await self.git("hash-object", "-w", "--stdin", stdin=content)).strip()
                    entries[p] = f"100644 blob {blob}"
            tree = await self._build_tree(entries)
            args = ["commit-tree", tree, "-m", message] + (["-p", old] if old else [])
            sha = (await self.git(*args, identity=ident, actor=actor)).strip()
            await self.git("update-ref", f"refs/heads/{ref}", sha, old or "", actor=actor)
        if self.log:
            self.log.event("commit", actor, ref=ref, sha=sha, message=message, files=list(files))
        return sha

    async def _build_tree(self, entries: dict[str, str]) -> str:
        """entries: path -> 'mode type sha'. Builds nested trees bottom-up with mktree."""
        root: dict = {}
        for p, meta in entries.items():
            node = root
            parts = p.split("/")
            for d in parts[:-1]:
                node = node.setdefault(d, {})
            node[parts[-1]] = meta

        async def build(node: dict) -> str:
            lines = []
            for name, v in sorted(node.items()):
                if isinstance(v, dict):
                    lines.append(f"040000 tree {await build(v)}\t{name}\n")
                else:
                    lines.append(f"{v}\t{name}\n")
            return (await self.git("mktree", stdin="".join(lines))).strip()

        return await build(root)

    async def read_file_at(self, ref: str, path: str) -> str | None:
        out = await self.git("show", f"refs/heads/{ref}:{path}", check=False)
        return out if out else None

    async def list_files_at(self, ref: str, path: str = "") -> list[str]:
        args = ["ls-tree", "-r", "--name-only", f"refs/heads/{ref}"]
        if path:
            args += ["--", path]
        out = await self.git(*args, check=False)
        return [l for l in out.splitlines() if l]

    # -------------------------------------------------------------------- log
    async def log_text(self, refs: list[str] | None = None, since: float | None = None,
                       limit: int = 200, max_chars: int = 30000) -> str:
        args = ["log", f"--format={LOG_FORMAT}", f"-n{limit}", "--date-order"]
        if since:
            args.append(f"--since=@{int(since)}")
        if refs:
            args += [f"refs/heads/{r}" for r in refs]
        else:
            args.append("--all")
        out = await self.git(*args, check=False)
        lines = []
        for rec in out.split("\x1e"):
            if not rec.strip():
                continue
            sha, author, date, decor, body = (rec.split("\x1f") + [""] * 5)[:5]
            decor = f" ({decor.strip()})" if decor.strip() else ""
            body = body.strip().replace("\n", "\n    ")
            lines.append(f"{sha[:10]} {date} {author}{decor}\n    {body}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n...(truncated, {len(text) - max_chars} more chars; ask for a narrower log)"
        return text or "(no commits)"

    async def branches(self, prefix: str = "role/") -> list[str]:
        out = await self.git("for-each-ref", "--format=%(refname:short)", f"refs/heads/{prefix}")
        return [l for l in out.splitlines() if l]

    # ------------------------------------------------------------- worktrees
    async def add_worktree(self, path: Path, branch: str, base: str = MAIN, existing: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        async with self.ref_lock:
            if existing:
                await self.git("worktree", "add", str(path), branch)
            else:
                await self.git("worktree", "add", "-b", branch, str(path), base)

    async def remove_worktree(self, path: Path) -> None:
        await self.git("worktree", "remove", "--force", str(path), check=False)
        await self.git("worktree", "prune", check=False)

    # ------------------------------------------------------------------ merge
    async def merge_into_main(self, branch: str, message: str, ident: Identity,
                              actor: str) -> tuple[bool, str]:
        """Merge a branch into main with merge-tree (no worktree). Returns (ok, sha-or-conflicts)."""
        wl = await self.git("worktree", "list", "--porcelain", check=False)
        holders = []
        cur = None
        for line in wl.splitlines():
            if line.startswith("worktree "):
                cur = line[9:]
            elif line.strip() == f"branch refs/heads/{MAIN}":
                holders.append(cur)
        if holders:
            return False, (f"{MAIN} is checked out in a worktree ({', '.join(map(str, holders))}). "
                           f"Moving the {MAIN} ref under a checkout makes that worktree look dirty. "
                           "Switch that worktree back to its own branch (git checkout <your branch>) "
                           f"and retry. Never check out {MAIN} in a worktree.")
        async with self.ref_lock:
            main = await self.tip(MAIN)
            src = await self.tip(branch)
            if not src:
                return False, f"no such branch: {branch}"
            base = (await self.git("merge-base", main, src, check=False)).strip()
            if base == src:
                return True, f"already merged ({main[:10]})"
            if base == main:
                await self.git("update-ref", "refs/heads/main", src, main, actor=actor)
                result = f"fast-forward to {src[:10]}"
                sha = src
            else:
                code, out = await self.git_rc("merge-tree", "--write-tree", "--name-only",
                                              main, src, actor=actor)
                lines = out.splitlines()
                if code != 0:
                    conflicts = "\n".join(lines[1:]).strip()
                    if self.log:
                        self.log.event("merge", actor, branch=branch, ok=False, conflicts=conflicts)
                    return False, (f"conflicts merging {branch} into main (main untouched):\n"
                                   f"{conflicts}")
                tree = lines[0].strip()
                sha = (await self.git("commit-tree", tree, "-p", main, "-p", src, "-m", message,
                                      identity=ident, actor=actor)).strip()
                await self.git("update-ref", "refs/heads/main", sha, main, actor=actor)
                result = f"merged as {sha[:10]}"
        if self.log:
            self.log.event("merge", actor, branch=branch, ok=True, sha=sha, result=result)
        return True, (result + f". Only the {MAIN} ref moved; worktrees are untouched (that is "
                      f"expected). Check it with read_log refs=[{MAIN}] or read_file ref={MAIN}.")
