"""Overly extensive logging.

Every run gets its own directory: <swarm>/logs/<run_id>/
  events.jsonl         one JSON object per line, every event the harness sees
  gek.log              the same events as human-readable text
  agents/<id>.jsonl    full transcript of each agent (and the orchestrator),
                       every message appended exactly once
  meta.json            run id, start time, version, config snapshot

Every event line has: ts (ISO, UTC), t (unix float), run, seq, kind, actor, data.
The list of event kinds is in EVENT_KINDS below and in logs/README.txt.
Designed to be grep-able, jq-able, and graphable later (the TUI reads these).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVENT_KINDS = {
    "run_start": "harness started; data has config snapshot and versions",
    "run_end": "harness stopped; data has reason and totals",
    "orch_wake": "orchestrator woke; data.reason says why (timer, inbox, agent_exit, start)",
    "orch_sleep": "orchestrator went to sleep; data has seconds and its stated reason",
    "llm_request": "a model call started; data has model, message count, tool names",
    "llm_response": "a model call finished; data has content, tool calls, usage, latency, cost",
    "llm_error": "a model call failed after retries",
    "tool_call": "an actor called a tool; data has name and args",
    "tool_result": "a tool returned; data has a preview of the output and ok flag",
    "role_written": "a role template was created or changed; data has the template",
    "role_retired": "a role template was retired",
    "role_rejected": "a role template failed validation against the ceiling",
    "agent_spawn": "an agent was spawned; data has id, role, branch, worktree",
    "agent_exit": "an agent ended; data.how is finish, killed, max_steps, error, budget",
    "agent_kill": "an agent was killed by another actor; data has killer and reason",
    "git": "a git command ran; data has args, exit code, timing",
    "commit": "a commit landed on some ref; data has ref, sha, message",
    "merge": "a branch was merged to main, or a merge hit conflicts",
    "inbox": "a message arrived on swarm/inbox",
    "control": "a command arrived on swarm/inbox (docs/CONTROL.md); data has verb, arg, reason, ok, result",
    "config": "a live setting changed; data has key, old, new (or refused)",
    "mode": "who orchestrates changed: llm or human",
    "pause": "the swarm was paused or resumed",
    "compact": "an agent's older history was replaced by notes it wrote (or compaction failed)",
    "sandbox": "how agent shell commands are sandboxed this run (bwrap or none, and why)",
    "resume": "this run resumes an earlier one; data has how it ended, interrupted agents, salvaged work",
    "human_wake": "a wake in human mode: no model call; data has reasons and outside actions",
    "mcp": "an MCP server connected, failed, or listed tools",
    "tools": "the tools an agent was given, by source (gek, an MCP server, a plugin)",
    "plugin": "a plugin loaded, failed, was disabled, or logged something itself",
    "budget": "token totals crossed a threshold or the budget ran out",
    "note": "anything else worth knowing",
}

_LOGS_README = """\
gek logs
========

One directory per run, named by start time (UTC): YYYYmmdd-HHMMSS-<rand>.

  events.jsonl       every event, one JSON object per line
  gek.log            the same events as readable text
  agents/<id>.jsonl  each agent's full conversation, message by message
                     ('/' in agent ids becomes '__' in file names)
  meta.json          run id, start time, config snapshot

Each events.jsonl line:
  {"ts": "...", "t": 1795..., "run": "...", "seq": 12, "kind": "...",
   "actor": "orchestrator" | "<role>/<hex>" | "harness", "data": {...}}

Event kinds:
{kinds}

Quick looks:
  gek explain                 summary of the latest run (and the repo)
  gek explain --run <id>      summary of a specific run
  jq -c 'select(.kind=="agent_exit")' events.jsonl
  jq -r 'select(.kind=="llm_response") | [.t, .data.usage.total_tokens] | @tsv' events.jsonl
"""


def list_runs(logs_dir: Path) -> list[Path]:
    """Run directories, oldest first. Ordered by when each run actually started (the first
    event's timestamp), not by name: two runs started in the same second would otherwise sort
    by their random suffix."""
    if not logs_dir.exists():
        return []

    def started(p: Path) -> tuple[float, str]:
        try:
            with open(p / "events.jsonl", encoding="utf-8") as f:
                return float(json.loads(f.readline()).get("t", 0)), p.name
        except Exception:
            return (p / "events.jsonl").stat().st_mtime, p.name

    return sorted((p for p in logs_dir.iterdir() if (p / "events.jsonl").exists()), key=started)


def _now() -> tuple[str, float]:
    t = time.time()
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds"), t


def _short(v: Any, n: int = 300) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[:n] + f"...(+{len(s) - n} chars)"


def _clip(s: Any, n: int = 140) -> str:
    s = str(s or "").strip().replace("\r", "").split("\n")[0]
    return s if len(s) <= n else s[: n - 3] + "..."


def feed_line(kind: str, actor: str, d: dict[str, Any]) -> str | None:
    """The curated console feed: what a human watching wants. None = file-only.
    Plain ASCII on purpose (Windows consoles and cp1252 don't love emoji)."""
    if kind == "run_start":
        return f"== run started. goal: {_clip(d.get('goal'), 200)}"
    if kind == "run_end":
        return (f"== run ended ({d.get('reason')}). wakes={d.get('wakes')} agents={d.get('agents')} "
                f"tokens={d.get('tokens')} cost=${d.get('cost', 0):.4f}")
    if kind == "orch_wake":
        return f"[orchestrator] wakes (#{d.get('n')}): {', '.join(d.get('reasons') or [])}"
    if kind == "orch_sleep":
        return f"[orchestrator] sleeps {d.get('seconds')}s: {_clip(d.get('reason'))}"
    if kind == "role_written":
        r = d.get("role") or {}
        p = r.get("powers") or {}
        pw = [k for k in ("shell", "merge", "interrupt") if p.get(k)]
        if p.get("kill"):
            pw.append("kill:" + ",".join(p["kill"]))
        if p.get("mcp"):
            pw.append("mcp:" + ",".join(p["mcp"]))
        return f"  + role {r.get('name')} ({d.get('verb')}) powers=[{' '.join(pw) or 'none'}]: {_clip(r.get('mandate'), 100)}"
    if kind == "role_rejected":
        name = (d.get("role") or {}).get("name")
        return f"  x role {name} rejected: {_clip('; '.join(d.get('errors') or []), 160)}"
    if kind == "role_retired":
        return f"  - role {d.get('role')} retired: {_clip(d.get('reason'))}"
    if kind == "agent_spawn":
        return f"  -> {d.get('id')} spawned" + (f": {_clip(d.get('note'), 110)}" if d.get("note") else "")
    if kind == "agent_exit":
        lost = d.get("uncommitted_lost") or []
        extra = f" (lost {len(lost)} uncommitted files)" if lost else ""
        return f"  <- {actor} {d.get('how')} after {d.get('steps')} steps{extra}: {_clip(d.get('summary'), 120)}"
    if kind == "agent_kill":
        return f"  XX {actor} killed {d.get('target')}: {_clip(d.get('reason'))}"
    if kind == "merge":
        if d.get("ok"):
            return f"  >> {actor} merged {d.get('branch')} into main: {d.get('result')}"
        return f"  !! {actor} hit conflicts merging {d.get('branch')}"
    if kind == "commit" and not str(d.get("ref", "")).startswith("swarm/"):
        return f"  *  {actor} committed on {d.get('ref')}: {_clip(d.get('message'), 100)}"
    if kind == "tool_call" and d.get("name") in ("post", "interrupt"):
        tag = "says" if d.get("name") == "post" else "interrupts"
        return f"  ~  {actor} {tag}: {_clip((d.get('args') or {}).get('message'), 150)}"
    if kind == "control":
        mark = "!!" if d.get("ok", True) else "XX"
        what = " ".join(x for x in (d.get("verb"), d.get("arg")) if x)
        return f"  {mark} {actor} sent {what}: {_clip(d.get('reason'), 80)} -> {_clip(d.get('result'), 100)}"
    if kind == "config":
        if d.get("refused"):
            return f"  x  config {d.get('key')} refused: {_clip(d.get('refused'))}"
        return f"  =  {actor} set {d.get('key')}: {_clip(json.dumps(d.get('old')), 40)} -> {_clip(json.dumps(d.get('new')), 60)}"
    if kind == "compact":
        if not d.get("ok"):
            return f"  !  {actor} compaction failed: {_clip(d.get('error'), 80)}"
        return (f"  ~  {actor} compacted its history at step {d.get('step')}: "
                f"{d.get('before_tokens', 0):,} -> ~{d.get('after_tokens_est', 0):,} tokens")
    if kind == "sandbox":
        if d.get("using") == "bwrap":
            return f"   sandbox: bwrap (network {'on' if d.get('network') else 'off'})"
        if d.get("warning") and d.get("shell_allowed"):
            return f"!! WARNING: {d.get('warning')}"
        return None
    if kind == "resume":
        return (f"== resuming after {d.get('run')} ({_clip(d.get('ended'), 60)}): "
                f"{len(d.get('interrupted') or [])} interrupted agents, {len(d.get('salvaged') or [])} salvaged, "
                f"{d.get('tokens', 0):,} tokens carried")
    if kind == "mode":
        return f"[orchestrator] mode is now {d.get('mode')} (by {actor})"
    if kind == "pause":
        return f"  || swarm {'paused' if d.get('paused') else 'resumed'} by {actor}: {_clip(d.get('reason'))}"
    if kind == "human_wake":
        return f"[you] the orchestrator would wake (human mode): {', '.join(d.get('reasons') or [])}"
    if kind == "inbox" and d.get("external"):
        return f"  @  inbox from {actor}: {_clip(d.get('message'), 150)}"
    if kind == "llm_error":
        return f"  !  {actor} model error: {_clip(d.get('error'), 180)}"
    if kind == "note" and d.get("error"):
        return f"  !  {actor}: {_clip(d.get('error'), 180)}"
    if kind == "note" and d.get("model_rewritten"):
        m = d["model_rewritten"]
        return f"  i  model {m.get('from')} -> {m.get('to')}"
    if kind == "mcp" and not d.get("ok") and not d.get("closing"):
        return f"  !  {actor} MCP {d.get('server')} failed: {_clip(d.get('error'), 150)}"
    if kind == "budget":
        if d.get("exhausted"):
            return f"  $  token budget spent: {_clip(d.get('detail'))}"
        return f"  $  {d.get('total_tokens')} tokens so far (cap {d.get('cap') or 'none'})"
    return None


class RunLog:
    """echo: "feed" (curated console lines, default), "all" (every event), or "none"."""

    def __init__(self, logs_dir: Path, run_id: str | None = None, echo: str | bool = "feed"):
        logs_dir.mkdir(parents=True, exist_ok=True)
        readme = logs_dir / "README.txt"
        if not readme.exists():
            kinds = "\n".join(f"  {k:<14} {v}" for k, v in EVENT_KINDS.items())
            readme.write_text(_LOGS_README.replace("{kinds}", kinds), encoding="utf-8")
        if run_id is None:
            import secrets

            run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        self.run_id = run_id
        self.dir = logs_dir / run_id
        (self.dir / "agents").mkdir(parents=True, exist_ok=True)
        self._events = open(self.dir / "events.jsonl", "a", encoding="utf-8")
        self._text = open(self.dir / "gek.log", "a", encoding="utf-8")
        self._agent_files: dict[str, Any] = {}
        self._seq = 0
        self._lock = threading.Lock()
        self.listeners: list[Any] = []  # called with each event record (plugins)
        self.echo = {True: "feed", False: "none"}.get(echo, echo) if isinstance(echo, bool) else echo

    def write_meta(self, meta: dict[str, Any]) -> None:
        with open(self.dir / "meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, default=str)

    def event(self, kind: str, actor: str = "harness", **data: Any) -> None:
        ts, t = _now()
        with self._lock:
            self._seq += 1
            rec = {"ts": ts, "t": t, "run": self.run_id, "seq": self._seq,
                   "kind": kind, "actor": actor, "data": data}
            self._events.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            self._events.flush()
            line = f"{ts} #{self._seq:<5} {kind:<13} {actor:<22} {_short(data)}"
            self._text.write(line + "\n")
            self._text.flush()
        for fn in list(self.listeners):
            try:
                fn(rec)
            except Exception:
                pass
        if self.echo == "all":
            print(line[11:23] + " " + f"{kind:<13} {actor:<22} {_short(data, 160)}",
                  file=sys.stderr, flush=True)
        elif self.echo == "feed":
            try:
                fl = feed_line(kind, actor, data)
            except Exception:
                fl = None
            if fl:
                print(f"{ts[11:19]} {fl}", flush=True)

    def transcript(self, actor: str, message: dict[str, Any]) -> None:
        """Append one conversation message for an actor, exactly once."""
        ts, t = _now()
        with self._lock:
            f = self._agent_files.get(actor)
            if f is None:
                name = actor.replace("/", "__") + ".jsonl"
                f = open(self.dir / "agents" / name, "a", encoding="utf-8")
                self._agent_files[actor] = f
            f.write(json.dumps({"ts": ts, "t": t, **message}, ensure_ascii=False, default=str) + "\n")
            f.flush()

    def stderr_file(self, name: str):
        """A file for a subprocess's stderr (MCP servers), so it never hits the console."""
        f = open(self.dir / f"{name.replace('/', '__')}.stderr.log", "a", encoding="utf-8")
        with self._lock:
            self._agent_files[f"__stderr__{name}"] = f
        return f

    def close(self) -> None:
        with self._lock:
            for f in [self._events, self._text, *self._agent_files.values()]:
                try:
                    f.close()
                except Exception:
                    pass
