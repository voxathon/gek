"""Command line: gek init | run | inbox | explain"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .config import ENV_TEMPLATE, load_config, write_default_config
from .gitrepo import INBOX, Identity, Repo


async def _init(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    cfg_path = root / "config.json"
    if not cfg_path.exists():
        write_default_config(cfg_path, args.goal)
        print(f"wrote {cfg_path}")
    else:
        print(f"kept existing {cfg_path}")
    env_path = root / ".env"
    if not env_path.exists():
        env_path.write_text(ENV_TEMPLATE, encoding="utf-8")
        print(f"wrote {env_path} (put your API key in it)")
    cfg = load_config(cfg_path)
    if cfg.repo_dir.exists():
        print(f"swarm repo already exists at {cfg.repo_dir}")
        return 0
    seed = Path(args.seed).resolve() if args.seed else None
    await Repo.init(cfg.repo_dir, cfg.raw.get("goal", ""), seed, None)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    print(f"created swarm repo at {cfg.repo_dir}" + (f" (seeded from {seed})" if seed else ""))
    print("next: edit config.json (goal, models) and .env, then `gek run`")
    return 0


async def _run(args: argparse.Namespace) -> int:
    from .logs import RunLog
    from .swarm import Swarm

    cfg = load_config(args.config)
    if args.goal:
        cfg.raw["goal"] = args.goal
    if not cfg.repo_dir.exists():
        print(f"no swarm repo at {cfg.repo_dir}; run `gek init` first", file=sys.stderr)
        return 2
    log = RunLog(cfg.logs_dir, echo="none" if args.quiet else ("all" if args.verbose else "feed"))
    print(f"gek run {log.run_id}; logs in {log.dir}", file=sys.stderr)
    swarm = Swarm(cfg, log)
    try:
        reason = await swarm.run()
    finally:
        log.close()
    print(f"swarm stopped: {reason}", file=sys.stderr)
    return 0


async def _inbox(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    repo = Repo(cfg.repo_dir)
    who = args.sender
    from .swarm import _message

    text = _message(args.message, {"From": who})
    sha = await repo.empty_commit(INBOX, text, Identity(who, f"{who}@outside.gek"), actor=who)
    print(f"posted to swarm/inbox as {who}: {sha[:10]}")
    return 0


async def _explain(args: argparse.Namespace) -> int:
    from .explain import explain

    cfg = load_config(args.config)
    print(await explain(cfg, args.run, args.chatter))
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="gek", description="Asynchronous agent swarm harness over git.")
    p.add_argument("--config", "-c", default=None, help="path to config.json (default ./config.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create config.json, .env and the swarm repo")
    s.add_argument("--dir", default=".", help="where config.json lives (default .)")
    s.add_argument("--goal", default=None, help="goal text for a new config")
    s.add_argument("--seed", default=None, help="existing git repo to clone as the starting main")
    s.set_defaults(fn=_init)

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
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
