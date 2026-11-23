"""Configuration: .env for secrets, config.json for everything else.

Lookup order for config.json: --config flag, then ./config.json.
The .env file is loaded from the same directory as config.json (and then
from the current directory), without overriding variables already set.
"""

from __future__ import annotations

import json
import os
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
    },
    "agents": {
        "max_alive": 6,
        "max_steps": 60,
        "max_tool_output_chars": 12000,
        "shell_timeout_s": 180,
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
        "_comment": "name -> {command, args, env}. Grant them to roles via ceiling.mcp.",
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


@dataclass
class Config:
    path: Path
    raw: dict[str, Any] = field(default_factory=dict)

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
        """Config as logged: config.json never holds secrets, but env names are noted."""
        api = self.raw.get("api", {})
        return {
            **self.raw,
            "_env_present": {
                v: bool(os.environ.get(v)) for v in api.values() if isinstance(v, str)
            },
        }


def load_config(path: str | os.PathLike | None = None) -> Config:
    p = Path(path) if path else Path.cwd() / "config.json"
    p = p.resolve()
    load_dotenv(p.parent / ".env", override=False)
    load_dotenv(Path.cwd() / ".env", override=False)
    if not p.exists():
        raise FileNotFoundError(
            f"No config at {p}. Run `gek init` to create config.json and .env."
        )
    with open(p, encoding="utf-8") as f:
        user = json.load(f)
    return Config(path=p, raw=_deep_merge(DEFAULT_CONFIG, user))


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
"""
