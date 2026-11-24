"""Command line.

  gek init NAME [--goal G] [--seed REPO]    new swarm under ~/.gek/swarms/NAME (becomes current)
  gek init --portable [--dir D] ...         old style: config.json + .env + swarm/ in D
  gek list                                  all swarms, current one marked
  gek use NAME                              switch the current swarm
  gek run | explain | inbox MSG | where     act on the current swarm

Which swarm a command acts on:
  -c/--config PATH  that config.json (portable)
  -s/--swarm NAME   that home swarm
  otherwise         ./config.json if it is a gek config (portable), else the current swarm
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from .config import (DEFAULT_CONFIG, ENV_TEMPLATE, NAME_OK, Config, current_swarm, gek_home,
                     is_gek_config, list_swarms, load_config, load_swarm, set_current_swarm,
                     swarms_dir, write_default_config, write_home_defaults, write_swarm_config)
from .gitrepo import INBOX, Identity, Repo


class UserError(Exception):
    pass


def resolve(args: argparse.Namespace) -> Config:
    if args.config:
        return load_config(args.config, portable=True)
    if args.swarm:
        return load_swarm(args.swarm)
    here = Path.cwd() / "config.json"
    if here.exists() and is_gek_config(here):
        return load_config(here, portable=True)
    name = current_swarm()
    if not name:
        raise UserError("No swarm selected. Create one with `gek init NAME`, pick one with "
                        "`gek use NAME`, or pass -s NAME.")
    return load_swarm(name)


# ------------------------------------------------------------------ commands
async def _init(args: argparse.Namespace) -> int:
    seed = Path(args.seed).resolve() if args.seed else None
    if args.portable:
        root = Path(args.dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        cfg_path = root / "config.json"
        if not cfg_path.exists():
            write_default_config(cfg_path, args.goal)
            print(f"wrote {cfg_path}")
        if not (root / ".env").exists():
            (root / ".env").write_text(ENV_TEMPLATE, encoding="utf-8")
            print(f"wrote {root / '.env'} (put your API key in it)")
        cfg = load_config(cfg_path, portable=True)
    else:
        if not args.name:
            raise UserError("Give the swarm a name: `gek init NAME` (or use --portable).")
        if not NAME_OK.match(args.name):
            raise UserError(f"Bad swarm name {args.name!r}: letters, digits, - _ . only.")
        for p in write_home_defaults():
            print(f"wrote {p}" + (" (put your API key in it)" if p.name == ".env" else ""))
        d = swarms_dir() / args.name
        if (d / "config.json").exists():
            raise UserError(f"Swarm {args.name!r} already exists at {d}.")
        d.mkdir(parents=True, exist_ok=True)
        write_swarm_config(d / "config.json", args.goal)
        cfg = load_swarm(args.name)
        set_current_swarm(args.name)
    if cfg.repo_dir.exists():
        print(f"swarm repo already exists at {cfg.repo_dir}")
        return 0
    await Repo.init(cfg.repo_dir, cfg.raw.get("goal", ""), seed, None)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    print(f"created swarm {cfg.name!r} at {cfg.swarm_dir}" + (f" (seeded from {seed})" if seed else ""))
    if not cfg.portable:
        print(f"it is now the current swarm. shared settings: {gek_home() / 'config.json'}; "
              f"this swarm's: {cfg.path}")
    if cfg.raw.get("goal") == DEFAULT_CONFIG["goal"]:
        print("set a goal in its config.json (or pass --goal to run)")
    return 0


def _lock_info(cfg: Config) -> str | None:
    lock = cfg.swarm_dir / "run.lock"
    if not lock.exists():
        return None
    try:
        info = json.loads(lock.read_text(encoding="utf-8"))
    except Exception:
        return "running? (unreadable run.lock)"
    pid = info.get("pid")
    if os.name == "posix" and pid:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return f"stale run.lock (pid {pid} gone)"
        except PermissionError:
            pass
    return f"running (pid {pid}, run {info.get('run')})"


def _last_run(cfg: Config) -> str:
    if not cfg.logs_dir.exists():
        return "never run"
    runs = sorted(p for p in cfg.logs_dir.iterdir() if (p / "events.jsonl").exists())
    if not runs:
        return "never run"
    last = runs[-1]
    end = None
    try:
        with open(last / "events.jsonl", encoding="utf-8") as f:
            for line in f:
                if '"run_end"' in line:
                    end = json.loads(line)
    except Exception:
        pass
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime((last / "events.jsonl").stat().st_mtime))
    if end:
        d = end["data"]
        return f"{len(runs)} runs, last {when}: {d.get('reason')} ({d.get('tokens')} tokens)"
    return f"{len(runs)} runs, last {when}: no run_end (crashed or running)"


async def _list(args: argparse.Namespace) -> int:
    cur = current_swarm()
    names = list_swarms()
    print(f"gek home: {gek_home()}")
    if not names:
        print("no swarms yet. `gek init NAME --goal \"...\"`")
        return 0
    for n in names:
        try:
            cfg = load_swarm(n)
        except Exception as e:
            print(f"  {n}: broken config ({e})")
            continue
        mark = "*" if n == cur else " "
        goal = " ".join(str(cfg.raw.get("goal", "")).split())
        goal = goal if len(goal) <= 70 else goal[:67] + "..."
        state = _lock_info(cfg) or _last_run(cfg)
        print(f"{mark} {n:<20} {state}\n    goal: {goal}")
    return 0


async def _use(args: argparse.Namespace) -> int:
    load_swarm(args.name)  # raises if missing
    set_current_swarm(args.name)
    print(f"current swarm: {args.name}")
    return 0


async def _where(args: argparse.Namespace) -> int:
    cfg = resolve(args)
    print(f"swarm:     {cfg.name} ({'portable' if cfg.portable else 'home'})")
    print(f"config:    {cfg.path}")
    if not cfg.portable:
        print(f"shared:    {gek_home() / 'config.json'}")
        print(f"secrets:   {gek_home() / '.env'}")
    print(f"repo:      {cfg.repo_dir}")
    print(f"worktrees: {cfg.worktrees_dir}")
    print(f"logs:      {cfg.logs_dir}")
    return 0


async def _run(args: argparse.Namespace) -> int:
    from .logs import RunLog
    from .swarm import Swarm

    cfg = resolve(args)
    if args.goal:
        cfg.raw["goal"] = args.goal
    if not cfg.repo_dir.exists():
        raise UserError(f"No swarm repo at {cfg.repo_dir}; run `gek init` first.")
    busy = _lock_info(cfg)
    if busy and busy.startswith("running"):
        raise UserError(f"Swarm {cfg.name!r} looks busy: {busy}. If that's wrong, delete "
                        f"{cfg.swarm_dir / 'run.lock'}.")
    log = RunLog(cfg.logs_dir, echo="none" if args.quiet else ("all" if args.verbose else "feed"))
    lock = cfg.swarm_dir / "run.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "run": log.run_id, "started": time.time()}),
                    encoding="utf-8")
    print(f"gek run {log.run_id} on swarm {cfg.name!r}; logs in {log.dir}", file=sys.stderr)
    swarm = Swarm(cfg, log)
    try:
        reason = await swarm.run()
    finally:
        log.close()
        try:
            lock.unlink()
        except OSError:
            pass
    print(f"swarm stopped: {reason}", file=sys.stderr)
    return 0


async def _inbox(args: argparse.Namespace) -> int:
    from .swarm import _message

    cfg = resolve(args)
    repo = Repo(cfg.repo_dir)
    who = args.sender
    sha = await repo.empty_commit(INBOX, _message(args.message, {"From": who}),
                                  Identity(who, f"{who}@outside.gek"), actor=who)
    print(f"posted to {cfg.name}'s inbox as {who}: {sha[:10]}")
    return 0


async def _explain(args: argparse.Namespace) -> int:
    from .explain import explain

    cfg = resolve(args)
    print(await explain(cfg, args.run, args.chatter))
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="gek", description="Asynchronous agent swarm harness over git.",
                                epilog=f"Swarms live under GEK_HOME (now {gek_home()}).")
    p.add_argument("--config", "-c", default=None, help="act on this config.json (portable swarm)")
    p.add_argument("--swarm", "-s", default=None, help="act on this home swarm instead of the current one")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create a swarm (under GEK_HOME, or --portable here)")
    s.add_argument("name", nargs="?", help="swarm name (home swarms)")
    s.add_argument("--goal", default=None, help="goal text")
    s.add_argument("--seed", default=None, help="existing git repo to clone as the starting main")
    s.add_argument("--portable", action="store_true", help="config.json, .env and swarm/ in --dir instead")
    s.add_argument("--dir", default=".", help="where a portable swarm goes (default .)")
    s.set_defaults(fn=_init)

    s = sub.add_parser("list", aliases=["ls"], help="list swarms under GEK_HOME")
    s.set_defaults(fn=_list)

    s = sub.add_parser("use", help="switch the current swarm")
    s.add_argument("name")
    s.set_defaults(fn=_use)

    s = sub.add_parser("where", help="show where the current swarm's files are")
    s.set_defaults(fn=_where)

    s = sub.add_parser("run", help="run the swarm until halt, budget or Ctrl-C")
    s.add_argument("--goal", default=None, help="override the goal for this run")
    s.add_argument("--quiet", "-q", action="store_true", help="print nothing; everything still goes to the logs")
    s.add_argument("--verbose", "-v", action="store_true", help="echo every event (the firehose) to stderr")
    s.set_defaults(fn=_run)

    s = sub.add_parser("inbox", help="send a message to the orchestrator's inbox")
    s.add_argument("message")
    s.add_argument("--as", dest="sender", default="human", help="sender name (default human)")
    s.set_defaults(fn=_inbox)

    s = sub.add_parser("explain", help="summarize the latest run and the repo")
    s.add_argument("--run", default=None, help="run id (prefix ok)")
    s.add_argument("--chatter", type=int, default=25, help="how many chatter commits to show")
    s.set_defaults(fn=_explain)

    args = p.parse_args(argv)
    try:
        code = asyncio.run(args.fn(args))
    except (UserError, FileNotFoundError) as e:
        print(f"gek: {e}", file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
