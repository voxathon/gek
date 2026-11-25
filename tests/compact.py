"""Context compaction: past agents.compact_at_tokens of prompt, an agent's older history is
replaced by notes it writes itself. Scripted fake model, no API key.

    uv run python tests/compact.py
"""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
import tempfile
from pathlib import Path

from gek.config import load_config, write_default_config
from gek.gitrepo import Repo
from gek.logs import RunLog, feed_line
from gek.swarm import Swarm

_ids = itertools.count()
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")


def call(_tool: str, **args) -> dict:
    return {"id": f"c{next(_ids)}", "type": "function",
            "function": {"name": _tool, "arguments": json.dumps(args)}}


def size(messages) -> int:
    return sum(len(json.dumps(m)) for m in messages) // 4


def paired(messages) -> bool:
    """Every tool result answers a call made by the assistant message before it."""
    open_ids: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant":
            open_ids = {tc["id"] for tc in m.get("tool_calls") or []}
        elif m.get("role") == "tool":
            if m.get("tool_call_id") not in open_ids:
                return False
    return True


async def scenario(tmp: Path, roles: dict, fail_summary: bool = False):
    tmp.mkdir(parents=True)
    cfg_path = tmp / "config.json"
    write_default_config(cfg_path, "Test compaction.")
    raw = json.loads(cfg_path.read_text())
    raw["orchestrator"].update(min_sleep_s=0.5, debounce_s=0.2, inbox_poll_s=0.3)
    raw["agents"].update(compact_at_tokens=3000, compact_keep_recent=6, status_every_steps=100)
    cfg_path.write_text(json.dumps(raw, indent=2))
    cfg = load_config(cfg_path)
    await Repo.init(cfg.repo_dir, cfg.raw["goal"], None, None)
    log = RunLog(cfg.logs_dir, echo=False)
    swarm = Swarm(cfg, log)
    seen = {"summary_asks": [], "calls": [], "steps": 0}

    async def fake(model, messages, tools, actor):
        await asyncio.sleep(0.01)
        usage = {"prompt_tokens": size(messages), "completion_tokens": 50}
        usage["total_tokens"] = usage["prompt_tokens"] + 50
        if actor == "orchestrator":
            if not seen.get("spawned"):
                seen["spawned"] = True
                calls = [call("write_role", **spec) for spec in roles.values()]
                calls += [call("spawn", role=name, note="grind") for name in roles]
                calls.append(call("sleep", seconds=600, reason="wait"))
                return {"role": "assistant", "content": "", "tool_calls": calls}, usage
            alive = [a for a in swarm.agents.values() if a.status in ("starting", "running")]
            if not alive:
                return {"role": "assistant", "content": "", "tool_calls": [call("halt", reason="done")]}, usage
            return {"role": "assistant", "content": "", "tool_calls": [call("sleep", seconds=600, reason="w")]}, usage
        if tools is None:  # the compaction call
            seen["summary_asks"].append(messages)
            if fail_summary:
                raise RuntimeError("provider hiccup")
            return {"role": "assistant", "content": "NOTES: wrote f0..f9, all committed. Next: finish."}, usage
        seen["calls"].append((actor, [dict(m) for m in messages]))
        n = sum(1 for a, _ in seen["calls"] if a == actor)
        if n <= 14:
            return {"role": "assistant", "content": "", "tool_calls": [
                call("write_file", path=f"f{n}.txt", content="x" * 200),
                call("read_file", path=f"f{n}.txt")]}, usage
        return {"role": "assistant", "content": "", "tool_calls": [call("finish", summary="ground it out")]}, usage

    swarm.llm.fake = fake
    await asyncio.wait_for(swarm.run(), timeout=90)
    log.close()
    ev = [json.loads(l) for l in open(log.dir / "events.jsonl")]
    return swarm, seen, ev


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gek-compact-"))
    grinder = {"name": "grinder", "mandate": "write files", "writes": ["*"]}
    keeper = {"name": "keeper", "mandate": "write files, remember everything", "writes": ["*"],
              "compact_at": -1}

    swarm, seen, ev = await scenario(tmp / "a", {"grinder": grinder, "keeper": keeper})
    comp = [e for e in ev if e["kind"] == "compact"]
    g = [e for e in comp if e["actor"].startswith("grinder")]
    k = [e for e in comp if e["actor"].startswith("keeper")]
    check(len(g) >= 1 and all(e["data"]["ok"] for e in g), "a long agent gets compacted")
    check(not k, "a role with compact_at -1 never does")
    gid = g[0]["actor"] if g else ""
    after = [m for a, m in seen["calls"] if a == gid]
    first_after = next((m for m in after if any("was compacted" in str(x.get("content")) for x in m)), None)
    check(first_after is not None and "NOTES: wrote f0..f9" in json.dumps(first_after),
          "the next call carries the agent's own notes")
    check(first_after is not None and first_after[0]["role"] == "system"
          and "just been spawned" in first_after[1]["content"],
          "the system prompt and spawn message are kept")
    check(all(paired(m) for _, m in seen["calls"]), "no tool result is orphaned from its call")
    before = g[0]["data"]["before_tokens"] if g else 0
    check(first_after is not None and size(first_after) < before, "the prompt actually got smaller")
    ask = seen["summary_asks"][0] if seen["summary_asks"] else []
    check(len(ask) == 2 and "HISTORY:" in ask[1]["content"] and "f1.txt" in ask[1]["content"],
          "the summary call sees the old history, without tools")
    exits = {e["actor"].split("/")[0]: e["data"]["how"] for e in ev if e["kind"] == "agent_exit"}
    check(exits.get("grinder") == "finish" and exits.get("keeper") == "finish",
          "both agents carry on and finish")
    asks = sum(1 for e in ev if e["kind"] == "llm_request" and e["actor"] == gid and not e["data"]["tools"])
    check(asks == len(g), "the compaction call is billed to the agent")
    tfile = next((tmp / "a").rglob(f"agents/{gid.split('/')[0]}*{gid.split('/')[-1]}.jsonl"), None) if gid else None
    tr = [json.loads(l) for l in open(tfile)] if tfile else []
    check(any("was compacted" in str(t.get("content", "")) for t in tr),
          "the transcript records the compaction")
    fl = feed_line("compact", gid, g[0]["data"]) if g else ""
    check(bool(fl) and "compacted" in fl, "the feed shows it")
    sysmsg = after[0][0]["content"] if after else ""
    check("compacted" in sysmsg or "replaced by notes" in sysmsg, "agents are told about compaction up front")

    swarm2, seen2, ev2 = await scenario(tmp / "b", {"grinder": grinder}, fail_summary=True)
    comp2 = [e for e in ev2 if e["kind"] == "compact"]
    check(comp2 and not comp2[0]["data"]["ok"], "a failed summary is logged")
    exits2 = [e["data"]["how"] for e in ev2 if e["kind"] == "agent_exit"]
    check(exits2 == ["finish"], "...and the agent carries on anyway")

    bad = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
