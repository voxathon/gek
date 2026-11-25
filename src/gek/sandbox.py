"""Sandboxing for agents' `run` commands.

On Linux with a working bubblewrap (bwrap), every `run` command is wrapped so that:

  - the whole filesystem is visible but read-only,
  - the agent's own worktree (and the swarm's git objects, so `git commit` works) is writable,
  - /tmp is a fresh tmpfs per command,
  - a per-swarm cache folder is writable and persists (XDG_CACHE_HOME, UV_CACHE_DIR, ...), so
    `uv sync` / `pip install` don't re-download everything every command,
  - gek's home, the swarm folder (config.json, .env, other agents' worktrees, logs) and the usual
    credential spots in $HOME (~/.ssh, ~/.aws, ~/.netrc, ...) are hidden,
  - network is on unless agents.sandbox_network is false.

agents.sandbox:
  "auto"  (default) use bwrap when it works; otherwise log one warning per run and carry on
  "bwrap" refuse to start the run unless bwrap works
  "off"   never sandbox, no warning

Not Linux, or no working bwrap: commands run unsandboxed, exactly as before, with one warning per
run. Cross-platform first; the sandbox is a bonus where the OS has one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, gek_home

# credential spots under $HOME hidden from sandboxed commands (dirs get an empty tmpfs, files /dev/null)
DEFAULT_HIDE = [
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config/gcloud", ".config/gh",
    ".config/hub", ".netrc", ".git-credentials", ".pypirc", ".npmrc", ".password-store",
    ".local/share/keyrings", ".gek",
]

INSTALL_HINT = ("install bubblewrap to sandbox agent shells (apt install bubblewrap / "
                "dnf install bubblewrap / pacman -S bubblewrap / apk add bubblewrap)")


class SandboxError(RuntimeError):
    pass


@dataclass
class Sandbox:
    kind: str                      # "bwrap" or "none"
    reason: str = ""               # why it is "none" (empty when off by config)
    mode: str = "auto"
    network: bool = True
    cache: Path | None = None
    hide: list[Path] = field(default_factory=list)
    writable: list[Path] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.kind == "bwrap"

    def warning(self) -> str | None:
        """The once-per-run warning, or None if there's nothing to warn about."""
        if self.active or self.mode == "off":
            return None
        return f"agent shell commands are NOT sandboxed: {self.reason}"

    def describe(self) -> str:
        """One line for agent and orchestrator prompts."""
        if not self.active:
            return "shell commands are not sandboxed (be careful outside your worktree)"
        net = "on" if self.network else "off"
        return ("shell commands run in a sandbox: your worktree, /tmp (wiped per command) and "
                "$XDG_CACHE_HOME are writable, everything else is read-only, the swarm's own files "
                f"are hidden; network is {net}")

    def env(self, env: dict[str, str]) -> dict[str, str]:
        if not self.active or not self.cache:
            return env
        c = str(self.cache)
        env = dict(env)
        env.update(XDG_CACHE_HOME=c, UV_CACHE_DIR=os.path.join(c, "uv"),
                   PIP_CACHE_DIR=os.path.join(c, "pip"), npm_config_cache=os.path.join(c, "npm"),
                   TMPDIR="/tmp")
        return env

    def argv(self, command: str, worktree: Path, git_dir: Path, hidden_roots: list[Path],
             branch: str = "") -> list[str]:
        """The full argv for one sandboxed `run` command. The swarm's git repo is read-only
        except what this agent's commits need, so a shell can't forge commits on swarm/inbox
        (Control: commands) or move other agents' branches."""
        a = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
             "--tmpfs", "/tmp", "--unshare-all", "--die-with-parent", "--new-session"]
        if self.network:
            a.append("--share-net")
        for p in self.hide:
            if p.is_dir():
                a += ["--tmpfs", str(p)]
            elif p.exists():
                a += ["--ro-bind", "/dev/null", str(p)]
        for p in hidden_roots:          # gek home, the swarm folder: tmpfs over them...
            if p.exists():
                a += ["--tmpfs", str(p)]
        if git_dir.exists():             # ...then the bits the agent needs, bound back on top
            a += ["--ro-bind", str(git_dir), str(git_dir)]
        for p in [*git_writable(worktree, git_dir, branch), worktree,
                  *([self.cache] if self.cache else []), *self.writable]:
            if p.exists():
                a += ["--bind", str(p), str(p)]
        a += ["--chdir", str(worktree), "--", "/bin/sh", "-c", command]
        return a


def git_writable(worktree: Path, git_dir: Path, branch: str) -> list[Path]:
    """What a commit on `branch` from `worktree` writes inside the bare repo: objects, the
    worktree's admin dir (HEAD, index), and the folder holding the branch's ref and reflog.
    Folders are created up front so they can be bound."""
    out = [git_dir / "objects"]
    try:
        line = (worktree / ".git").read_text(encoding="utf-8").strip()
        if line.startswith("gitdir:"):
            out.append(Path(line.split(":", 1)[1].strip()))
    except OSError:
        pass
    if branch:
        parent = Path(*(["refs", "heads"] + branch.split("/")[:-1]))
        for base in (git_dir / parent, git_dir / "logs" / parent):
            try:
                base.mkdir(parents=True, exist_ok=True)
                out.append(base)
            except OSError:
                pass
    return out


def _probe(network: bool) -> str:
    """'' if bwrap works here, else why not."""
    exe = shutil.which("bwrap")
    if not exe:
        return f"bwrap not found; {INSTALL_HINT}"
    cmd = [exe, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
           "--unshare-all", "--die-with-parent", "--new-session"] + (["--share-net"] if network else []) + ["true"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception as e:  # noqa: BLE001
        return f"bwrap failed to start ({e})"
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        return ("bwrap is installed but doesn't work here (" + (msg[-1] if msg else f"exit {r.returncode}")
                + "); unprivileged user namespaces may be disabled, or this is a container")
    return ""


def detect(cfg: Config) -> Sandbox:
    """Decide how this run sandboxes shells. Raises SandboxError when agents.sandbox is "bwrap"
    and it can't be had."""
    a = cfg.section("agents")
    mode = str(a.get("sandbox", "auto")).lower()
    network = bool(a.get("sandbox_network", True))
    if mode not in ("auto", "bwrap", "off"):
        raise SandboxError(f'agents.sandbox must be "auto", "bwrap" or "off", not {mode!r}')
    if mode == "off":
        return Sandbox("none", mode=mode, network=network)
    if not sys.platform.startswith("linux"):
        why = f"no bwrap on {sys.platform} (sandboxing is Linux-only)"
    else:
        why = _probe(network)
    if why:
        if mode == "bwrap":
            raise SandboxError(f'agents.sandbox is "bwrap" but {why}')
        return Sandbox("none", reason=why, mode=mode, network=network)
    home = Path.home()
    hide = [home / h for h in DEFAULT_HIDE] + [Path(p).expanduser() for p in a.get("sandbox_hide", [])]
    writable = [Path(p).expanduser().resolve() for p in a.get("sandbox_writable", [])]
    cache = cfg.swarm_dir / "sandbox-cache"
    cache.mkdir(parents=True, exist_ok=True)
    return Sandbox("bwrap", mode=mode, network=network, cache=cache, hide=hide, writable=writable)


def hidden_roots(cfg: Config) -> list[Path]:
    """Folders tmpfs'd over inside the sandbox: gek's home and this swarm's folder (config.json,
    .env, logs, other worktrees)."""
    roots = {gek_home(), cfg.root.resolve(), cfg.swarm_dir.resolve()}
    # outermost first so inner binds land on top of them
    return sorted(roots, key=lambda p: len(p.parts))
