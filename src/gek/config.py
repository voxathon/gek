"""Configuration: .env for secrets, config.json for everything else.

Two layouts:

  home (default)   everything under one folder, GEK_HOME or ~/.gek:
                     ~/.gek/config.json          shared settings (models, api, ceiling...)
                     ~/.gek/.env                 shared secrets
                     ~/.gek/current              name of the current swarm
                     ~/.gek/swarms/<name>/       one folder per swarm:
                         config.json             per-swarm overrides (goal at least)
                         repo.git/ wt/ logs/     its repo, worktrees, logs
                         run.lock                present while `gek run` is going
  portable         config.json + .env + swarm/ in whatever folder you chose
                   (`gek init --portable`); any folder with a gek config.json works.

Config layering: built-in defaults <- ~/.gek/config.json <- the swarm's config.json
(portable swarms skip the shared layer). .env files: the swarm folder, then GEK_HOME
(home swarms only), then the current directory;
variables already set in the environment always win.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

DEFAULT_CONFIG: dict[str, Any] = {
    "_comment": "gek config. Secrets go in .env, not here. See README.md for every key.",
    "swarm_dir": "swarm",
    "goal": "Describe what the swarm should work on. The orchestrator reads this every wake.",
    "api": {
        "api_base_env": "GEK_API_BASE",
        "api_key_env": "GEK_API_KEY",
    },
    "models": {
        "orchestrator": "openai/auto",
        "default": "openai/auto",
        "aliases": {
            "smart": "openai/auto",
            "cheap": "openai/auto",
        },
        "pricing": {},
    },
    "llm": {
        "temperature": None,
        "timeout_s": 600,
        "num_retries": 3,
        "extra_params": {},
    },
    "orchestrator": {
        "default_sleep_s": 300,
        "min_sleep_s": 20,
        "max_sleep_s": 3600,
        "debounce_s": 10,
        "inbox_poll_s": 5,
        "max_steps_per_wake": 30,
        "log_chars_per_wake": 30000,
        "wake_on_agent_exit": True,
        "mode": "llm",
    },
    "agents": {
        "max_alive": 6,
        "max_steps": 60,
        "max_tool_output_chars": 12000,
        "shell_timeout_s": 180,
        "env_passthrough": [],
        "env_notes": [],
        "status_every_steps": 5,
        "compact_at_tokens": 50_000,
        "compact_keep_recent": 8,
        "sandbox": "auto",
        "sandbox_network": True,
        "sandbox_hide": [],
        "sandbox_writable": [],
    },
    "budget": {
        "max_total_tokens": 5_000_000,
    },
    "ceiling": {
        "_comment": "The most power the orchestrator may grant to any role.",
        "shell": True,
        "merge": True,
        "interrupt": True,
        "kill": ["*"],
        "mcp": {},
    },
    "mcp_servers": {
        "_comment": "name -> {command, args, env, cwd, timeout_s, enabled}. Grant them to "
                    "roles via ceiling.mcp. Live: edits apply to a running swarm (agents "
                    "reconnect between steps). Never put secrets here.",
    },
    "plugins": {
        "_comment": "name -> its config; only plugins named here load. Bundled: notify (see README).",
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def gek_home() -> Path:
    return Path(os.environ.get("GEK_HOME") or (Path.home() / ".gek")).expanduser().resolve()


def swarms_dir() -> Path:
    return gek_home() / "swarms"


def current_swarm() -> str | None:
    f = gek_home() / "current"
    if f.exists():
        name = f.read_text(encoding="utf-8").strip()
        return name or None
    return None


def set_current_swarm(name: str) -> None:
    gek_home().mkdir(parents=True, exist_ok=True)
    (gek_home() / "current").write_text(name + "\n", encoding="utf-8")


def list_swarms() -> list[str]:
    d = swarms_dir()
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if (p / "config.json").exists())


def is_gek_config(path: Path) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return isinstance(data, dict) and "swarm_dir" in data
    except Exception:
        return False


@dataclass
class Config:
    path: Path
    raw: dict[str, Any] = field(default_factory=dict)
    name: str = ""
    portable: bool = False

    # convenience accessors -------------------------------------------------
    @property
    def root(self) -> Path:
        return self.path.parent

    @property
    def swarm_dir(self) -> Path:
        p = Path(self.raw["swarm_dir"])
        return p if p.is_absolute() else (self.root / p)

    @property
    def repo_dir(self) -> Path:
        return self.swarm_dir / "repo.git"

    @property
    def worktrees_dir(self) -> Path:
        return self.swarm_dir / "wt"

    def tool_sources(self) -> set[str]:
        """Names a role may be granted tools from: MCP servers and configured plugins."""
        names = set(self.raw.get("mcp_servers") or {}) | set(self.raw.get("plugins") or {})
        return {n for n in names if not n.startswith("_")}

    @property
    def logs_dir(self) -> Path:
        return self.swarm_dir / "logs"

    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {})

    def model(self, name: str | None) -> str:
        models = self.raw["models"]
        if not name:
            return models["default"]
        return models.get("aliases", {}).get(name, name)

    def api_kwargs(self) -> dict[str, Any]:
        api = self.raw.get("api", {})
        kw: dict[str, Any] = {}
        base = os.environ.get(api.get("api_base_env", "GEK_API_BASE"))
        key = os.environ.get(api.get("api_key_env", "GEK_API_KEY"))
        if base:
            kw["api_base"] = base
        if key:
            kw["api_key"] = key
        return kw

    def redacted(self) -> dict[str, Any]:
        """Config as logged/displayed: config.json never holds secrets, but env names are noted.
        MCP server `env` values are masked (display only; self.raw keeps them)."""
        api = self.raw.get("api", {})
        return {
            **self.raw,
            "mcp_servers": redact_mcp(self.raw.get("mcp_servers")),
            "_env_present": {
                v: bool(os.environ.get(v)) for v in api.values() if isinstance(v, str)
            },
        }


# ------------------------------------------------------------- mcp servers
# mcp_servers = {name: {command, args, env, cwd, timeout_s, enabled}}; `_`-prefixed keys are
# comments (see write_default_config). Every entry is hot: it is applied to a running swarm and
# live agents reconnect their MCP bridge between steps. Never put secrets in config.json: env
# values are redacted for display/logs (redacted()), names stay.
MCP_FIELDS = ("command", "args", "env", "cwd", "timeout_s", "enabled")
MCP_ACTIONS = ("set", "rm", "enable", "disable")


def mcp_names(servers: Any) -> list[str]:
    """Server names in config order, `_`-prefixed comment keys removed."""
    return [str(k) for k in (servers or {}) if not str(k).startswith("_")]


def mcp_entry(spec: Any) -> dict[str, Any]:
    """One server's normalized entry (defaults filled in). enabled defaults to True."""
    spec = spec if isinstance(spec, dict) else {}
    out: dict[str, Any] = {}
    if spec.get("command"):
        out["command"] = str(spec["command"])
    if spec.get("args"):
        out["args"] = [str(a) for a in spec["args"]]
    if spec.get("env"):
        out["env"] = {str(k): str(v) for k, v in spec["env"].items()}
    if spec.get("cwd"):
        out["cwd"] = str(spec["cwd"])
    if spec.get("timeout_s"):
        out["timeout_s"] = int(spec["timeout_s"])
    else:
        out["timeout_s"] = 60
    out["enabled"] = bool(spec.get("enabled", True))
    return out


def shape_mcp_entry(spec: Any, name: str = "") -> dict[str, Any]:
    """Validate one mcp_servers entry; raises ValueError. stdio only (a command to launch)."""
    label = f"mcp server {name!r}" if name else "mcp server"
    if not isinstance(spec, dict):
        raise ValueError(f"{label} must be an object like "
                         '{"command": "npx", "args": ["-y", "some-server"]}')
    if spec.get("url") and not spec.get("command"):
        raise ValueError(f"{label}: url/http servers aren't supported yet; give a command (stdio)")
    unknown = [k for k in spec if str(k) not in MCP_FIELDS and k != "url"]
    if unknown:
        raise ValueError(f"{label}: unknown field(s) {', '.join(map(str, unknown))}; "
                         f"allowed: {', '.join(MCP_FIELDS)} (stdio: command+args)")
    if not str(spec.get("command") or "").strip():
        raise ValueError(f"{label} needs a command (the program to launch, e.g. npx)")
    out = mcp_entry(spec)
    if not isinstance(spec.get("args", []), list):
        raise ValueError(f"{label}: args must be a list of strings")
    if not isinstance(spec.get("env", {}), dict):
        raise ValueError(f"{label}: env must be an object of NAME -> value")
    try:
        out["timeout_s"] = int(out["timeout_s"])
    except (TypeError, ValueError):
        raise ValueError(f"{label}: timeout_s must be a whole number of seconds")
    if out["timeout_s"] <= 0:
        raise ValueError(f"{label}: timeout_s must be positive")
    out["enabled"] = bool(spec.get("enabled", True))
    return out


def mcp_servers(raw: Any, only_enabled: bool = True) -> dict[str, Any]:
    """The servers a bridge should connect to (enabled ones by default)."""
    out = {}
    for name in mcp_names(raw):
        spec = (raw or {}).get(name)
        if not isinstance(spec, dict):
            continue
        if only_enabled and not spec.get("enabled", True):
            continue
        out[name] = spec
    return out


def mcp_edit(raw_servers: Any, action: str, name: str, entry: Any = None) -> dict[str, Any]:
    """Return a NEW mcp_servers dict with one change applied (used by `mcp set|rm|enable|disable`).

    Keeps `_`-prefixed comment keys and every other server untouched, so this can be persisted
    as a whole with persist_key(path, "mcp_servers", result)."""
    if not NAME_OK.match(str(name or "")):
        raise ValueError(f"{name!r} isn't a valid server name (letters, digits, _.-; "
                         "no dots-only/comment names)")
    cur = dict(raw_servers or {})
    action = (action or "").strip()
    if action not in MCP_ACTIONS:
        raise ValueError(f"mcp {action or '?'}: use one of {', '.join(MCP_ACTIONS)}")
    if action == "set":
        cur[name] = shape_mcp_entry(entry, name)
        return cur
    if name not in cur or str(name).startswith("_"):
        raise ValueError(f"no mcp server {name!r} in mcp_servers")
    if action == "rm":
        del cur[name]
    else:
        spec = cur[name] if isinstance(cur[name], dict) else {"command": ""}
        cur[name] = {**spec, "enabled": action == "enable"}
    return cur


def redact_mcp(servers: Any) -> dict[str, Any]:
    """A copy of mcp_servers with env VALUES masked (names kept) for display and logs."""
    out: dict[str, Any] = {}
    for name, spec in (servers or {}).items():
        if isinstance(spec, dict) and isinstance(spec.get("env"), dict):
            out[name] = {**spec, "env": {str(k): "***" for k in spec["env"]}}
        else:
            out[name] = spec
    return out


def _read_json(p: Path) -> dict[str, Any]:
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_config(path: str | os.PathLike | None = None, name: str = "",
                portable: bool | None = None) -> Config:
    """Load one swarm's config. path = that swarm's config.json."""
    p = (Path(path) if path else Path.cwd() / "config.json").resolve()
    home = gek_home()
    if portable is None:
        portable = home not in p.parents
    load_dotenv(p.parent / ".env", override=False)
    if not portable:
        load_dotenv(home / ".env", override=False)
    load_dotenv(Path.cwd() / ".env", override=False)
    if not p.exists():
        raise FileNotFoundError(f"No config at {p}. Run `gek init NAME` (or `gek init --portable`).")
    raw = DEFAULT_CONFIG
    glob = home / "config.json"
    if not portable and glob.exists() and glob.resolve() != p:  # portable swarms are self-contained
        raw = _deep_merge(raw, _read_json(glob))
    raw = _deep_merge(raw, _read_json(p))
    return Config(path=p, raw=raw, name=name or (p.parent.name if not portable else str(p.parent)),
                  portable=portable)


def load_swarm(name: str) -> Config:
    p = swarms_dir() / name / "config.json"
    if not p.exists():
        have = ", ".join(list_swarms()) or "none"
        raise FileNotFoundError(f"No swarm named {name!r} in {swarms_dir()} (have: {have}).")
    return load_config(p, name=name, portable=False)


def write_home_defaults() -> list[Path]:
    """Create ~/.gek/config.json and ~/.gek/.env if missing. Returns what was written."""
    home = gek_home()
    home.mkdir(parents=True, exist_ok=True)
    wrote = []
    if not (home / "config.json").exists():
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["_comment"] = ("gek shared settings for every swarm under this folder. A swarm's own "
                           "config.json (swarms/<name>/config.json) overrides any key. Secrets go in .env.")
        cfg.pop("goal", None)
        cfg.pop("swarm_dir", None)
        with open(home / "config.json", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        wrote.append(home / "config.json")
    if not (home / ".env").exists():
        (home / ".env").write_text(ENV_TEMPLATE, encoding="utf-8")
        wrote.append(home / ".env")
    return wrote


def write_swarm_config(path: Path, goal: str | None) -> None:
    """A home swarm's own config: just what differs from the shared one."""
    cfg = {
        "_comment": "This swarm's settings. Anything not here comes from ../../config.json.",
        "swarm_dir": ".",
        "goal": goal or DEFAULT_CONFIG["goal"],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def write_default_config(path: Path, goal: str | None) -> None:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if goal:
        cfg["goal"] = goal
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


ENV_TEMPLATE = """\
# gek secrets. Never commit this file.
GEK_API_BASE=https://app.vllm-sr.ai/v1
GEK_API_KEY=put-your-key-here
# password for `gek tui --web` (needed when serving beyond localhost)
# GEK_WEB_PASSWORD=
"""


# ---------------------------------------------------------------- live config
HOT_PREFIXES = (
    "goal", "budget.", "ceiling.", "ceiling", "models.default", "models.orchestrator",
    "models.aliases", "models.pricing", "mcp_servers", "llm.temperature", "llm.timeout_s",
    "llm.num_retries", "llm.extra_params", "agents.", "orchestrator.",
)
COLD_PREFIXES = ("swarm_dir", "api")


def is_hot(key: str) -> bool:
    if any(key == c or key.startswith(c + ".") for c in COLD_PREFIXES):
        return False
    return any(key == p.rstrip(".") or key.startswith(p if p.endswith(".") else p + ".")
               or key == p for p in HOT_PREFIXES)


def get_path(raw: dict[str, Any], key: str, default: Any = None) -> Any:
    node: Any = raw
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_path(raw: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    node = raw
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def flatten(raw: Any, prefix: str = "") -> dict[str, Any]:
    """Leaf values by dotted key (dicts are walked; lists and scalars are leaves)."""
    if not isinstance(raw, dict) or not raw:
        return {prefix: raw} if prefix else {}
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if str(k).startswith("_"):
            continue
        out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    return out


def parse_value(text: str) -> Any:
    """CLI/control values are JSON (3, true, "x", [..], {..}); anything else is a plain string."""
    try:
        return json.loads(text)
    except Exception:
        return text


def persist_key(path: Path, key: str, value: Any) -> None:
    """Write one key into a config.json file, keeping everything else in it."""
    data: dict[str, Any] = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    set_path(data, key, value)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
