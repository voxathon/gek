"""Role templates and the grant ceiling.

A role describes a kind of worker, not a task. Templates are stored as
roles/<name>.json on the swarm/roles branch, so their history is in git too.

Powers a role can hold (all checked against config.ceiling):
  shell      bool               may run shell commands in its worktree
  merge      bool               may merge branches into main
  interrupt  bool               may post to swarm/inbox (wakes the orchestrator)
  kill       [role names]       may kill agents of these roles ("*" = any)
  mcp        {source: [globs]}  tool sources (MCP servers, tool-providing plugins) and which
                                of their tools it sees; `tools` is accepted as another name
Filesystem scope is `writes`: path prefixes it may commit changes under
("" or "*" = anywhere in its worktree).
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
RESERVED = {"orchestrator", "harness", "gek", "swarm"}


@dataclass
class Powers:
    shell: bool = False
    merge: bool = False
    interrupt: bool = False
    kill: list[str] = field(default_factory=list)
    mcp: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Role:
    name: str
    mandate: str
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    powers: Powers = field(default_factory=Powers)
    temperament: str = ""
    model: str = ""
    max_steps: int = 0
    max_tokens: int = 0  # per agent of this role; 0 = no cap beyond the swarm budget
    compact_at: int = 0  # prompt tokens before history compaction; 0 = agents.compact_at_tokens, -1 = never

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2) + "\n"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Role":
        p = d.get("powers") or {}
        powers = Powers(
            shell=bool(p.get("shell", False)),
            merge=bool(p.get("merge", False)),
            interrupt=bool(p.get("interrupt", False)),
            kill=[str(x) for x in (p.get("kill") or [])],
            mcp={str(k): [str(g) for g in (v or ["*"])]
                 for k, v in {**(p.get("tools") or {}), **(p.get("mcp") or {})}.items()},
        )

        def strs(v: Any) -> list[str]:
            if v is None:
                return []
            if isinstance(v, str):
                return [v]
            return [str(x) for x in v]

        return cls(
            name=str(d.get("name", "")).strip(),
            mandate=str(d.get("mandate", "")).strip(),
            reads=strs(d.get("reads")),
            writes=strs(d.get("writes")),
            powers=powers,
            temperament=str(d.get("temperament") or ""),
            model=str(d.get("model") or ""),
            max_steps=int(d.get("max_steps") or 0),
            max_tokens=int(d.get("max_tokens") or 0),
            compact_at=int(d.get("compact_at") or 0),
        )

    def may_write(self, path: str) -> bool:
        path = path.replace("\\", "/").lstrip("./")
        for w in self.writes:
            w = w.replace("\\", "/").strip()
            if w in ("", "*", "/", "."):
                return True
            if path == w.rstrip("/") or path.startswith(w.rstrip("/") + "/") or fnmatch.fnmatch(path, w):
                return True
        return False

    def may_kill(self, role: str) -> bool:
        return "*" in self.powers.kill or role in self.powers.kill


def _glob_covered(g: str, ceiling: list[str]) -> bool:
    for c in ceiling:
        if c == "*" or c == g:
            return True
        if not any(ch in g for ch in "*?[") and fnmatch.fnmatch(g, c):
            return True
        if g.endswith("*") and c.endswith("*") and g[:-1].startswith(c[:-1]):
            return True
    return False


def ceiling_tools(ceiling: dict[str, Any]) -> dict[str, list[str]]:
    """The ceiling's tool grants: `mcp` and its other name `tools`, merged."""
    merged = {**(ceiling.get("tools") or {}), **(ceiling.get("mcp") or {})}
    return {k: v for k, v in merged.items() if not k.startswith("_")}


def validate(role: Role, ceiling: dict[str, Any], mcp_servers: Any,
             model_ok: bool = True) -> list[str]:
    """mcp_servers: anything with `in` for the known tool sources (config.tool_sources())."""
    """Return a list of problems. Empty list = valid. Attenuation only: never above the ceiling."""
    errs: list[str] = []
    if not NAME_RE.match(role.name):
        errs.append(f"name {role.name!r} must match {NAME_RE.pattern}")
    if role.name in RESERVED:
        errs.append(f"name {role.name!r} is reserved")
    if not role.mandate:
        errs.append("mandate is required")
    p = role.powers
    for flag in ("shell", "merge", "interrupt"):
        if getattr(p, flag) and not ceiling.get(flag, False):
            errs.append(f"power {flag!r} is above the ceiling")
    ck = ceiling.get("kill", [])
    for r in p.kill:
        if "*" not in ck and r not in ck:
            errs.append(f"kill({r}) is above the ceiling")
    cm = ceiling_tools(ceiling)
    for server, globs in p.mcp.items():
        if server not in cm:
            errs.append(f"tool source {server!r} is not in the ceiling (ceiling has: {sorted(cm) or 'none'})")
            continue
        if server not in mcp_servers:
            errs.append(f"tool source {server!r} is neither in mcp_servers nor a configured plugin")
        for g in globs:
            if not _glob_covered(g, cm[server]):
                errs.append(f"mcp {server}:{g} is above the ceiling ({cm[server]})")
    if not model_ok:
        errs.append(f"model {role.model!r} is not a known alias or model")
    return errs
