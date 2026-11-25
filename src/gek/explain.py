"""`gek explain`: what happened, for future-you who forgot everything."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import Config
from .gitrepo import CHATTER, INBOX, MAIN, ROLES, Repo


def _runs(logs_dir: Path) -> list[Path]:
    if not logs_dir.exists():
        return []
    from .logs import list_runs

    return list_runs(logs_dir)


def _load(run: Path) -> list[dict]:
    out = []
    with open(run / "events.jsonl", encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _fmt_t(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")


async def explain(cfg: Config, run_id: str | None = None, chatter_n: int = 25) -> str:
    out: list[str] = []
    w = out.append
    w("gek: an asynchronous swarm harness. An LLM orchestrator wakes up, reads the git log,")
    w("writes role templates, spawns/kills agents, and goes back to sleep. Agents work on their")
    w("own branches in a shared git repo and talk through the swarm/chatter branch.\n")
    w(f"swarm:     {cfg.name} ({'portable' if cfg.portable else 'home'})")
    w(f"config:    {cfg.path}")
    w(f"swarm dir: {cfg.swarm_dir}")
    w(f"goal:      {cfg.raw.get('goal', '').strip()}\n")

    runs = _runs(cfg.logs_dir)
    w(f"runs logged: {len(runs)}" + (f"  (first {runs[0].name}, last {runs[-1].name})" if runs else ""))
    run = None
    if run_id:
        run = next((r for r in runs if r.name.startswith(run_id)), None)
        if not run:
            w(f"no run matching {run_id!r}")
    elif runs:
        run = runs[-1]
    if run:
        ev = _load(run)
        kinds = Counter(e["kind"] for e in ev)
        start = ev[0]["t"] if ev else 0
        end = ev[-1]["t"] if ev else 0
        endrec = next((e for e in reversed(ev) if e["kind"] == "run_end"), None)
        w(f"\n=== run {run.name} ===")
        w(f"from {_fmt_t(start)} to {_fmt_t(end)} ({round((end - start) / 60, 1)} min), {len(ev)} events")
        w("ended: " + (endrec["data"].get("reason", "?") if endrec else "no run_end event (crashed or still running?)"))
        toks = sum((e["data"].get("usage") or {}).get("total_tokens", 0) for e in ev if e["kind"] == "llm_response")
        cached = sum((e["data"].get("usage") or {}).get("cached_prompt_tokens", 0) or 0
                     for e in ev if e["kind"] == "llm_response")
        cost = sum(e["data"].get("cost") or 0 for e in ev if e["kind"] == "llm_response")
        w(f"model calls: {kinds['llm_response']} ok, {kinds['llm_error']} failed; tokens {toks}"
          f" ({cached} prompt tokens served from cache); cost ${cost:.4f}")
        w(f"orchestrator wakes: {kinds['orch_wake']}")
        per: dict[str, list[int]] = {}
        for e in ev:
            if e["kind"] == "llm_response":
                u = e["data"].get("usage") or {}
                r = per.setdefault(e["actor"], [0, 0, 0])
                r[0] += 1
                r[1] += u.get("prompt_tokens", 0) or 0
                r[2] += u.get("completion_tokens", 0) or 0
        if per:
            w("tokens by actor (calls, prompt, completion, avg prompt/call):")
            for a, (n, pt, ct) in sorted(per.items(), key=lambda kv: -(kv[1][1] + kv[1][2])):
                w(f"  {a:<24} {n:>4} {pt:>10} {ct:>9} {pt // max(n, 1):>9}")
        spawns = [e for e in ev if e["kind"] == "agent_spawn"]
        exits = {e["actor"]: e for e in ev if e["kind"] == "agent_exit"}
        w(f"agents: {len(spawns)} spawned; exits: " +
          (", ".join(f"{k}={v}" for k, v in Counter(e["data"]["how"] for e in exits.values()).items()) or "none"))
        for e in ev:
            if e["kind"] in ("role_written", "role_retired", "role_rejected"):
                d = e["data"]
                name = (d.get("role") or {}).get("name") if isinstance(d.get("role"), dict) else d.get("role")
                extra = f" ({'; '.join(d.get('errors', []))})" if e["kind"] == "role_rejected" else ""
                w(f"  {_fmt_t(e['t'])} {e['kind']:<13} {name}{extra}")
        if spawns:
            w("\nagent  ->  how it ended")
            for s in spawns:
                aid = s["data"]["id"]
                x = exits.get(aid)
                how = x["data"]["how"] if x else "still alive / no exit logged"
                summ = (x["data"].get("summary") or "")[:110] if x else ""
                w(f"  {aid:<24} {how:<10} {summ}")
        kills = [e for e in ev if e["kind"] == "agent_kill"]
        if kills:
            w("\nkills:")
            for k in kills:
                w(f"  {k['actor']} killed {k['data']['target']}: {k['data']['reason'][:120]}")
        merges = [e for e in ev if e["kind"] == "merge"]
        if merges:
            w("\nmerges:")
            for m in merges:
                d = m["data"]
                w(f"  {m['actor']} {d['branch']}: " + (d.get("result", "") if d.get("ok") else "CONFLICT"))
        errs = [e for e in ev if e["kind"] == "llm_error" or (e["kind"] == "note" and "error" in e["data"])]
        if errs:
            w(f"\nerrors ({len(errs)}, last 5):")
            for e in errs[-5:]:
                w(f"  {_fmt_t(e['t'])} {e['actor']}: {str(e['data'].get('error'))[:200]}")
        w(f"\nfull detail: {run / 'gek.log'}  (and events.jsonl, agents/*.jsonl)")

    if cfg.repo_dir.exists():
        repo = Repo(cfg.repo_dir)
        w("\n=== repo now ===")
        roles = await repo.list_files_at(ROLES, "roles")
        w("roles: " + (", ".join(Path(r).stem for r in roles) or "none"))
        branches = await repo.branches("role/")
        w(f"agent branches: {len(branches)}")
        w("\nmain (last 8):")
        w(await repo.log_text([MAIN], limit=8, max_chars=4000))
        w(f"\nchatter (last {chatter_n}):")
        w(await repo.log_text([CHATTER], limit=chatter_n, max_chars=12000))
        w("\ninbox (last 5):")
        w(await repo.log_text([INBOX], limit=5, max_chars=3000))
    else:
        w("\n(no swarm repo yet; run `gek init`)")
    return "\n".join(out)
