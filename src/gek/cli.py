"""Command line.

  gek init NAME [--goal G] [--seed REPO]    new swarm under ~/.gek/swarms/NAME (becomes current)
  gek init --portable [--dir D] ...         old style: config.json + .env + swarm/ in D
  gek list                                  all swarms, current one marked
  gek use NAME                              switch the current swarm
  gek run | explain | inbox MSG | where     act on the current swarm
  gek push REMOTE [BRANCH] [--to D]         push swarm work to a git URL or path
  gek kill AGENT [REASON] | halt [REASON]   steer a running swarm (handled within seconds)
  gek spawn | role | config | mode | wake | pause | resume
                                            the rest of the control plane (docs/CONTROL.md)
  gek tui [--web]                           dashboard in the terminal, or in a browser
  gek tui                                   watch every swarm: graphs, agents, live feed

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
import re
import sys
import time
from pathlib import Path

from .config import (DEFAULT_CONFIG, ENV_TEMPLATE, NAME_OK, Config, current_swarm, gek_home,
                     is_gek_config, list_swarms, load_config, load_swarm, set_current_swarm,
                     swarms_dir, write_default_config, write_home_defaults, write_swarm_config)
from .gitrepo import INBOX, GitError, Identity, Repo


def subprocess_list2cmd(cmd: list[str]) -> str:
    import subprocess

    return subprocess.list2cmdline(cmd)


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


_URLISH = re.compile(r"^([a-z][a-z0-9+.-]*://|[^/\\\s]+@[^/\\\s]+:)", re.I)


def _seed_source(seed: str) -> str:
    """A git URL (https://, ssh://, git@host:path, file://) passes through; a path is resolved."""
    if _URLISH.match(seed) and not Path(seed).exists():
        return seed
    return str(Path(seed).expanduser().resolve())


# ------------------------------------------------------------------ commands
async def _init(args: argparse.Namespace) -> int:
    seed = _seed_source(args.seed) if args.seed else None
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
    if cfg.repo_dir.exists():
        print(f"swarm repo already exists at {cfg.repo_dir}")
        return 0
    try:
        await Repo.init(cfg.repo_dir, cfg.raw.get("goal", ""), seed, None)
    except BaseException:
        # don't leave a half-made swarm behind that blocks a retry with the same name
        import shutil

        shutil.rmtree(cfg.swarm_dir if not cfg.portable else cfg.repo_dir, ignore_errors=True)
        raise
    if not cfg.portable:
        set_current_swarm(args.name)
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


async def _push(args: argparse.Namespace) -> int:
    """Push swarm work out: main (or another branch) to REMOTE as gek/<swarm>/<branch>,
    or every branch with --everything. Everything lands under one prefix, so it never
    touches the remote's own branches."""
    cfg = resolve(args)
    repo = Repo(cfg.repo_dir)
    name = cfg.name if not cfg.portable else Path(cfg.name).name
    prefix = args.to or f"gek/{name}"
    if args.everything:
        refspecs = [f"+refs/heads/*:refs/heads/{prefix}/*"]
        what = f"all branches as {prefix}/*"
    else:
        if not await repo.tip(args.branch):
            raise UserError(f"no branch {args.branch!r} in swarm {cfg.name!r}")
        refspecs = [f"+refs/heads/{args.branch}:refs/heads/{prefix}/{args.branch}"]
        what = f"{args.branch} as {prefix}/{args.branch}"
    out = await repo.git("push", args.remote, *refspecs)
    print(f"pushed {what} to {args.remote}" + (f"\n{out.strip()}" if out.strip() else ""))
    return 0


def control_message(who: str, control: str, reason: str = "", payload: str = "") -> str:
    """The commit message format of docs/CONTROL.md: subject, optional payload, trailers."""
    reason = " ".join(reason.split())
    subject = f"{control}: {reason}" if reason else control
    if len(subject) > 72:
        subject = subject[:69] + "..."
    parts = [subject]
    if payload.strip():
        parts.append(payload.strip())
    trailers = [f"Control: {control}"] + ([f"Reason: {reason}"] if reason else []) + [f"From: {who}"]
    parts.append("\n".join(trailers))
    return "\n\n".join(parts) + "\n"


async def send_control(cfg: Config, who: str, control: str, reason: str = "", payload: str = "") -> str:
    """Post a control commit to swarm/inbox; a running swarm acts on it within inbox_poll_s."""
    repo = Repo(cfg.repo_dir)
    return await repo.empty_commit(INBOX, control_message(who, control, reason, payload),
                                   Identity(who, f"{who}@outside.gek"), actor=who)


def is_running(cfg: Config) -> bool:
    info = _lock_info(cfg)
    return bool(info and info.startswith("running"))


async def _needs_running(cfg: Config, what: str) -> None:
    if not is_running(cfg):
        raise UserError(f"{cfg.name!r} isn't running; {what} needs a running swarm (gek run).")


async def _role_offline(cfg: Config, who: str, data: dict | None, retire: str = "") -> str:
    """Apply a role change straight to swarm/roles when no swarm is running."""
    from .gitrepo import ROLES
    from .roles import Role, validate

    repo = Repo(cfg.repo_dir)
    ident = Identity(who, f"{who}@outside.gek")
    if retire:
        if not await repo.read_file_at(ROLES, f"roles/{retire}.json"):
            raise UserError(f"no role {retire!r}")
        await repo.write_files_commit(ROLES, {f"roles/{retire}.json": None},
                                      f"{who}: retire role {retire}\n\nRetired: {retire}\n", ident, actor=who)
        return f"role {retire} retired"
    role = Role.from_dict(data or {})
    errs = validate(role, cfg.raw.get("ceiling", {}), cfg.raw.get("mcp_servers", {}))
    if errs:
        raise UserError("role rejected:\n- " + "\n- ".join(errs))
    await repo.write_files_commit(ROLES, {f"roles/{role.name}.json": role.to_json()},
                                  f"{who}: set role {role.name}\n\nRole-Template: {role.name}\n",
                                  ident, actor=who)
    return f"role {role.name} written"


async def _kill(args: argparse.Namespace) -> int:
    cfg = resolve(args)
    sha = await send_control(cfg, args.sender, f"kill {args.agent}", args.reason)
    note = "" if _lock_info(cfg) else " (swarm isn't running; it will act on this next run)"
    print(f"sent kill {args.agent} to {cfg.name}: {sha[:10]}{note}")
    return 0


async def _halt(args: argparse.Namespace) -> int:
    cfg = resolve(args)
    sha = await send_control(cfg, args.sender, "halt", args.reason)
    note = "" if _lock_info(cfg) else " (swarm isn't running)"
    print(f"sent halt to {cfg.name}: {sha[:10]}{note}")
    return 0


def _sent(cfg: Config, what: str, sha: str) -> None:
    print(f"sent {what} to {cfg.name} ({sha[:10]}); the running swarm acts on it within a few seconds")


async def _spawn(args: argparse.Namespace) -> int:
    cfg = resolve(args)
    await _needs_running(cfg, "spawn")
    control = f"spawn {args.role}" + (f" from={args.from_branch}" if args.from_branch else "")
    _sent(cfg, control, await send_control(cfg, args.sender, control, "", args.note or ""))
    return 0


async def _wake(args: argparse.Namespace) -> int:
    cfg = resolve(args)
    await _needs_running(cfg, "wake")
    _sent(cfg, "wake", await send_control(cfg, args.sender, "wake", args.reason or "woken by hand"))
    return 0


async def _pause(args: argparse.Namespace) -> int:
    cfg = resolve(args)
    verb = args.cmd
    await _needs_running(cfg, verb)
    _sent(cfg, verb, await send_control(cfg, args.sender, verb, args.reason or ""))
    return 0


async def _mode(args: argparse.Namespace) -> int:
    from .config import persist_key

    cfg = resolve(args)
    if is_running(cfg):
        _sent(cfg, f"mode {args.mode}", await send_control(cfg, args.sender, f"mode {args.mode}", args.reason or ""))
    else:
        persist_key(cfg.path, "orchestrator.mode", args.mode)
        print(f"{cfg.name}: orchestrator.mode = {args.mode} (applies on the next run)")
    return 0


async def _config(args: argparse.Namespace) -> int:
    from .config import flatten, get_path, is_hot, parse_value, persist_key

    cfg = resolve(args)
    if args.action == "get":
        if args.key:
            print(json.dumps(get_path(cfg.raw, args.key), indent=2))
        else:
            for k, v in sorted(flatten(cfg.raw).items()):
                tag = "" if is_hot(k) else "   (restart)"
                print(f"{k} = {json.dumps(v)}{tag}")
        return 0
    if not args.key or args.value is None:
        raise UserError("usage: gek config set KEY VALUE   (VALUE is JSON: 3, true, \"text\", [..], {..})")
    if not is_hot(args.key):
        raise UserError(f"{args.key} needs a restart; edit {cfg.path} and restart the swarm")
    value = parse_value(args.value)
    old = get_path(cfg.raw, args.key)
    if args.key.startswith("ceiling") and not args.yes:
        prompt = (f"Change the ceiling {args.key}: {json.dumps(old)} -> {json.dumps(value)}? "
                  "This changes what the swarm may grant. [y/N] ")
        if not sys.stdin.isatty() or input(prompt).strip().lower() not in ("y", "yes"):
            raise UserError("not changed (pass --yes to skip the question)")
    if is_running(cfg):
        _sent(cfg, f"config set {args.key}", await send_control(
            cfg, args.sender, f"config set {args.key}", args.reason or "", json.dumps(value)))
    else:
        persist_key(cfg.path, args.key, value)
        print(f"{cfg.name}: {args.key} = {json.dumps(value)} (was {json.dumps(old)}; applies on the next run)")
    return 0


async def _role(args: argparse.Namespace) -> int:
    import os as _os
    import subprocess
    import tempfile

    from .gitrepo import ROLES
    from .roles import Role, validate

    cfg = resolve(args)
    repo = Repo(cfg.repo_dir)
    if args.action == "list":
        names = [Path(p).stem for p in await repo.list_files_at(ROLES, "roles")]
        print("\n".join(names) or "(no roles)")
        return 0
    if args.action == "show":
        text = await repo.read_file_at(ROLES, f"roles/{args.name}.json")
        if not text:
            raise UserError(f"no role {args.name!r}")
        print(text)
        return 0
    if args.action == "retire":
        if not args.name:
            raise UserError("usage: gek role retire NAME [REASON]")
        if is_running(cfg):
            _sent(cfg, f"role retire {args.name}",
                  await send_control(cfg, args.sender, f"role retire {args.name}", args.reason or ""))
        else:
            print(await _role_offline(cfg, args.sender, None, retire=args.name))
        return 0
    # set (from a file or stdin) or edit (in $EDITOR)
    if args.action == "set":
        src = args.name or "-"
        text = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
    else:
        if not args.name:
            raise UserError("usage: gek role edit NAME")
        current = await repo.read_file_at(ROLES, f"roles/{args.name}.json")
        start = current or Role(name=args.name, mandate="what this role is for").to_json()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write(start)
            tmp = f.name
        editor = _os.environ.get("VISUAL") or _os.environ.get("EDITOR") or (
            "notepad" if _os.name == "nt" else "nano")
        subprocess.call(f'{editor} "{tmp}"', shell=True)
        text = Path(tmp).read_text(encoding="utf-8")
        _os.unlink(tmp)
        if text.strip() == start.strip():
            print("unchanged")
            return 0
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise UserError(f"not valid JSON: {e}")
    errs = validate(Role.from_dict(data), cfg.raw.get("ceiling", {}), cfg.raw.get("mcp_servers", {}))
    if errs:
        raise UserError("role rejected:\n- " + "\n- ".join(errs))
    if is_running(cfg):
        _sent(cfg, f"role set {data.get('name')}",
              await send_control(cfg, args.sender, "role set", args.reason or f"set {data.get('name')}",
                                 json.dumps(data, indent=2)))
    else:
        print(await _role_offline(cfg, args.sender, data))
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

    s = sub.add_parser("push", help="push swarm work to a git remote (URL or path)")
    s.add_argument("remote", help="git URL or path, e.g. /mnt/shared/gek or git@github.com:you/repo")
    s.add_argument("branch", nargs="?", default="main", help="swarm branch to push (default main)")
    s.add_argument("--to", default=None, help="destination prefix (default gek/<swarm>); "
                                              "branches land at <prefix>/<branch>")
    s.add_argument("--everything", action="store_true",
                   help="push every branch (main, role/*, swarm/*) under the prefix")
    s.set_defaults(fn=_push)

    s = sub.add_parser("inbox", help="send a message to the orchestrator's inbox")
    s.add_argument("message")
    s.add_argument("--as", dest="sender", default="human", help="sender name (default human)")
    s.set_defaults(fn=_inbox)

    s = sub.add_parser("tui", help="watch swarms: graphs, agents, live feed")
    s.add_argument("--web", action="store_true", help="serve the TUI in a browser (textual-serve)")
    s.add_argument("--host", default="localhost", help="web host (0.0.0.0 to reach it from other machines)")
    s.add_argument("--port", type=int, default=8000, help="web port")
    s.add_argument("--public-url", default=None,
                   help="URL the browser uses, e.g. http://10.0.0.15:8000 (needed with --host 0.0.0.0)")
    s.set_defaults(fn=None)

    s = sub.add_parser("spawn", help="spawn an agent into a role (running swarm)")
    s.add_argument("role")
    s.add_argument("note", nargs="?", default="", help="what the agent should focus on")
    s.add_argument("--from", dest="from_branch", default="", help="start from this branch, e.g. a dead agent's")
    s.add_argument("--as", dest="sender", default="human")
    s.set_defaults(fn=_spawn)

    s = sub.add_parser("role", help="list, show, edit, set or retire role templates")
    s.add_argument("action", choices=["list", "show", "edit", "set", "retire"])
    s.add_argument("name", nargs="?", default="", help="role name (for set: a JSON file, or - for stdin)")
    s.add_argument("reason", nargs="?", default="")
    s.add_argument("--as", dest="sender", default="human")
    s.set_defaults(fn=_role)

    s = sub.add_parser("config", help="get or set live settings (applied without restarting)")
    s.add_argument("action", choices=["get", "set"])
    s.add_argument("key", nargs="?", default="")
    s.add_argument("value", nargs="?", default=None, help="JSON: 3, true, \"text\", [..], {..}")
    s.add_argument("--yes", action="store_true", help="don't ask before changing the ceiling")
    s.add_argument("--reason", default="")
    s.add_argument("--as", dest="sender", default="human")
    s.set_defaults(fn=_config)

    s = sub.add_parser("mode", help="who orchestrates: llm or human")
    s.add_argument("mode", choices=["llm", "human"])
    s.add_argument("reason", nargs="?", default="")
    s.add_argument("--as", dest="sender", default="human")
    s.set_defaults(fn=_mode)

    s = sub.add_parser("wake", help="wake the orchestrator now")
    s.add_argument("reason", nargs="?", default="")
    s.add_argument("--as", dest="sender", default="human")
    s.set_defaults(fn=_wake)

    for verb, helptext in (("pause", "freeze the swarm (nothing is killed)"), ("resume", "unfreeze the swarm")):
        s = sub.add_parser(verb, help=helptext)
        s.add_argument("reason", nargs="?", default="")
        s.add_argument("--as", dest="sender", default="human")
        s.set_defaults(fn=_pause)

    s = sub.add_parser("kill", help="kill a live agent of the running swarm, on the record")
    s.add_argument("agent", help="agent id, e.g. coder/ab12")
    s.add_argument("reason", nargs="?", default="", help="why (goes on the record)")
    s.add_argument("--as", dest="sender", default="human")
    s.set_defaults(fn=_kill)

    s = sub.add_parser("halt", help="stop the running swarm (live agents are killed)")
    s.add_argument("reason", nargs="?", default="", help="why (goes on the record)")
    s.add_argument("--as", dest="sender", default="human")
    s.set_defaults(fn=_halt)

    s = sub.add_parser("explain", help="summarize the latest run and the repo")
    s.add_argument("--run", default=None, help="run id (prefix ok)")
    s.add_argument("--chatter", type=int, default=25, help="how many chatter commits to show")
    s.set_defaults(fn=_explain)

    args = p.parse_args(argv)
    if args.cmd == "tui" and args.web:
        import shlex

        from textual_serve.server import Server

        cmd = [sys.executable, "-m", "gek"]
        if args.config:
            cmd += ["-c", args.config]
        if args.swarm:
            cmd += ["-s", args.swarm]
        cmd.append("tui")
        command = " ".join(shlex.quote(c) for c in cmd) if os.name != "nt" else subprocess_list2cmd(cmd)
        public = args.public_url
        if args.host in ("0.0.0.0", "::") and not public:
            print("gek: with --host 0.0.0.0 the page needs to know its real address; pass "
                  "--public-url http://<this-machine's-ip>:%d" % args.port, file=sys.stderr)
            sys.exit(2)
        print(f"gek tui on {public or f'http://{args.host}:{args.port}'}  "
              "(the app runs here; the browser only sees the app)")
        Server(command, host=args.host, port=args.port, title="gek", public_url=public).serve()
        sys.exit(0)
    if args.cmd == "tui":  # Textual runs its own event loop
        from .tui import run_tui

        try:
            initial = resolve(args)
        except (UserError, FileNotFoundError):
            initial = None
        run_tui(initial)
        sys.exit(0)
    try:
        code = asyncio.run(args.fn(args))
    except (UserError, FileNotFoundError) as e:
        print(f"gek: {e}", file=sys.stderr)
        code = 2
    except GitError as e:
        print(f"gek: git {' '.join(e.args_list)} failed (exit {e.code}):\n{(e.err or e.out).strip()}",
              file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
