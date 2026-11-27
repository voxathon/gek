"""The TUI across `gek run --resume`, headless. A resumed run continues the run it picks up:
one view with both runs' agents, tokens, wakes and feed, the idle time between them cut from the
time axis, the resume marked, and transcripts that span both. A run that resumes the one you are
watching joins the view instead of replacing it with an empty run.

    uv run python tests/tui_resume_chain.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="gek-tuichain-"))
os.environ["GEK_HOME"] = str(HOME)

from textual.widgets import DataTable, RichLog  # noqa: E402

from gek.cli import init_home_swarm  # noqa: E402
from gek.tui import GekTUI, RunData, chains_of, resume_parent  # noqa: E402

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")


def screen_text(app) -> str:
    return re.sub(r"<[^>]+>", "", app.export_screenshot()).replace("&#160;", " ")


def log_text(log: RichLog) -> str:
    return "\n".join("".join(seg.text for seg in strip) for strip in log.lines)


def write_run(logs: Path, name: str, events: list[tuple[float, str, str, dict]],
              transcripts: dict[str, list[dict]] | None = None) -> Path:
    d = logs / name
    (d / "agents").mkdir(parents=True, exist_ok=True)
    with open(d / "events.jsonl", "w", encoding="utf-8") as f:
        for t, kind, actor, data in events:
            f.write(json.dumps({"t": t, "kind": kind, "actor": actor, "data": data}) + "\n")
    for actor, msgs in (transcripts or {}).items():
        with open(d / "agents" / (actor.replace("/", "__") + ".jsonl"), "w", encoding="utf-8") as f:
            for m in msgs:
                f.write(json.dumps(m) + "\n")
    return d


def usage(n: int) -> dict:
    return {"usage": {"total_tokens": n, "prompt_tokens": n - 10, "completion_tokens": 10}, "cost": 0.0}


async def main() -> int:
    cfg, _ = await init_home_swarm("chain", "Reverse the plugin.", None)
    logs = cfg.logs_dir
    T = time.time() - 6 * 3600

    # run A: crashes with an agent mid-work (no agent_exit, no run_end)
    a = write_run(logs, "20261127-100000-aaaa", [
        (T, "run_start", "harness", {"goal": "Reverse the plugin.", "resume": None}),
        (T + 1, "orch_wake", "orchestrator", {"n": 1, "reasons": ["start"]}),
        (T + 2, "llm_response", "orchestrator", usage(1000)),
        (T + 3, "agent_spawn", "orchestrator", {"id": "fixer/0001", "role": "fixer", "branch": "role/fixer/0001"}),
        (T + 60, "llm_response", "fixer/0001", usage(500)),
        (T + 90, "commit", "fixer/0001", {"ref": "role/fixer/0001"}),
    ], {"orchestrator": [{"ts": "2026-11-27T10:00:01", "role": "user", "content": "FIRST RUN BRIEFING", "wake": 1}]})

    # run B: resumes A an hour later, respawns the fixer, ends cleanly
    R = T + 3600
    b = write_run(logs, "20261127-110000-bbbb", [
        (R, "run_start", "harness", {"goal": "Reverse the plugin.",
                                     "resume": {"run": a.name, "tokens": 1500, "crashed": True}}),
        (R + 0.5, "resume", "harness", {"run": a.name, "crashed": True}),
        (R + 1, "orch_wake", "orchestrator", {"n": 1, "reasons": ["start"]}),
        (R + 2, "llm_response", "orchestrator", usage(2000)),
        (R + 3, "agent_spawn", "orchestrator", {"id": "fixer/0002", "role": "fixer", "branch": "role/fixer/0001"}),
        (R + 30, "llm_response", "fixer/0002", usage(700)),
        (R + 40, "agent_exit", "fixer/0002", {"how": "finished", "summary": "done"}),
        (R + 50, "orch_wake", "orchestrator", {"n": 2, "reasons": ["agent finished"]}),
        (R + 60, "run_end", "harness", {"reason": "halted: done"}),
    ], {"orchestrator": [{"ts": "2026-11-27T11:00:01", "role": "user", "content": "RESUMED BRIEFING", "wake": 1}]})

    # run C: a fresh run later on (no resume)
    C = T + 7200
    c = write_run(logs, "20261127-120000-cccc", [
        (C, "run_start", "harness", {"goal": "Something else.", "resume": None}),
        (C + 1, "orch_wake", "orchestrator", {"n": 1, "reasons": ["start"]}),
        (C + 5, "run_end", "harness", {"reason": "halted: nothing to do"}),
    ])

    # --- the data model on its own
    check(resume_parent(b) == a.name and resume_parent(a) is None, "run_start says which run a run resumes")
    chains, pending = chains_of([a, b, c])
    check(chains == [[a, b], [c]] and not pending, "a resumed run joins the chain; a fresh run starts one")
    rd = RunData(b / "events.jsonl", segments=[a / "events.jsonl", b / "events.jsonl"])
    rd.poll()
    check(rd.tokens == 1000 + 500 + 2000 + 700, f"tokens add up across the chain ({rd.tokens})")
    check(rd.wakes == 3 and [n for _, n, _ in rd.wake_list] == [1, 2, 3], "wakes number on across the chain")
    check(rd.agents["fixer/0001"].status == "interrupted", "an agent alive when the run crashed shows interrupted")
    check(rd.agents["fixer/0002"].status == "finished", "the respawned agent carries on in the same view")
    check(rd.ended == "halted: done", "the chain ends how its newest run ended")
    span = rd.rel(rd.t_last) / 60
    check(span < 3, f"the hour between runs is cut from the time axis ({span:.1f} min)")
    check(len(rd.resume_list) == 1 and "crashed" in rd.resume_list[0][2], "the resume is marked, with how A ended")
    check(rd.agents["fixer/0001"].run_dir == a and rd.agents["orchestrator"].run_dir == b,
          "each agent remembers the run it was last seen in")

    # --- the TUI
    app = GekTUI(cfg)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.pause(1.0)
        txt = screen_text(app)
        check("cccc" in txt and "(2/2)" in txt, "two chains; it opens the newest (the fresh run)")

        await pilot.press("[")
        await pilot.pause(1.0)
        txt = screen_text(app)
        check("bbbb" in txt and "resumes 1 earlier" in txt and "(1/2)" in txt,
              "[ goes to the resumed run, shown as one with the run it resumes")
        check("tokens 4,200" in txt and "wakes 3" in txt, "header totals cover both runs")
        check("magenta | resume" in txt, "graphs mark the resume")
        feed = log_text(app.query_one("#feed", RichLog))
        check("resumed: run 20261127-110000-bbbb picks up 20261127-100000-aaaa" in feed
              and "1 agents interrupted" in feed, "feed says where it resumed and who was cut off")
        t = app.query_one("#agents", DataTable)
        ids = {str(k.value) for k in t.rows}
        check({"fixer/0001", "fixer/0002", "orchestrator"} <= ids, "agents from both runs are in the table")
        check(str(t.get_row("fixer/0001")[1]) == "interrupted", "...the crashed run's agent as interrupted")

        # the orchestrator's transcript spans both runs
        t.focus()
        t.move_cursor(row=t.get_row_index("orchestrator"))
        await pilot.press("enter")
        await pilot.pause(1.0)
        body = log_text(app.screen.query_one("#t-log", RichLog))
        check("FIRST RUN BRIEFING" in body and "RESUMED BRIEFING" in body
              and body.index("FIRST RUN") < body.index("RESUMED"), "orchestrator transcript spans both runs, in order")
        check("resumed in run 20261127-110000-bbbb" in body, "...with a divider where it resumed")
        await pilot.press("escape")
        await pilot.pause(0.3)

        # live: a new run resumes C while we watch C
        await pilot.press("]")
        await pilot.pause(0.5)
        check(app.follow_latest and app.run_idx == 1, "back on the newest chain, following it")
        d = logs / "20261127-130000-dddd"
        (d / "agents").mkdir(parents=True)
        (d / "events.jsonl").write_text("")  # RunLog creates it empty; run_start comes a moment later
        await pilot.pause(1.5)
        check(len(app.runs) == 2 and app.cur is not None and app.cur.path.parent == c,
              "a brand-new empty run isn't shown on its own while it can't say what it resumes")
        now = time.time()
        with open(d / "events.jsonl", "w", encoding="utf-8") as f:
            for e in [{"t": now, "kind": "run_start", "actor": "harness",
                       "data": {"goal": "Something else.", "resume": {"run": c.name, "tokens": 0}}},
                      {"t": now + 0.1, "kind": "orch_wake", "actor": "orchestrator", "data": {"n": 1, "reasons": ["start"]}}]:
                f.write(json.dumps(e) + "\n")
        await pilot.pause(1.5)
        txt = screen_text(app)
        check(len(app.runs) == 2 and app.cur is not None and len(app.cur.segments) == 2,
              "once it says it resumes C, it joins C's view (no new 'run')")
        check("dddd" in txt and "resumes 1 earlier" in txt and "(2/2)" in txt and "wakes 2" in txt,
              "the header follows the resumed run, totals included")
        feed = log_text(app.query_one("#feed", RichLog))
        check("halted: nothing to do" in feed and "resumed: run 20261127-130000-dddd" in feed,
              "the feed keeps C's history and marks the resume")

    passed = sum(ok for ok, _ in results)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
