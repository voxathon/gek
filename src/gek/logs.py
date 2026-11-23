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
    "mcp": "an MCP server connected, failed, or listed tools",
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


def _now() -> tuple[str, float]:
    t = time.time()
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds"), t


def _short(v: Any, n: int = 300) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[:n] + f"...(+{len(s) - n} chars)"


class RunLog:
    def __init__(self, logs_dir: Path, run_id: str | None = None, echo: bool = True):
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
        self.echo = echo

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
        if self.echo and kind not in ("git", "llm_request"):
            print(line[11:23] + " " + f"{kind:<13} {actor:<22} {_short(data, 160)}",
                  file=sys.stderr, flush=True)

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

    def close(self) -> None:
        with self._lock:
            for f in [self._events, self._text, *self._agent_files.values()]:
                try:
                    f.close()
                except Exception:
                    pass
