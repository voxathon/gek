"""The mcp_servers config schema for *remote* servers, end to end through a real config.json:
mixed stdio + remote + comment entries round-trip through JSON and through load_config/persist_key,
mcp_edit set/rm/enable/disable on a remote entry, redacted() masks header AND env values while
config.raw keeps them, and an entry with both url and command (or neither) is refused with a
message that names the server.

    uv run python tests/mcpconfig.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from gek.config import (load_config, mcp_edit, mcp_header_env_names, mcp_kind, mcp_resolve_headers,
                        mcp_transport, persist_key, redact_mcp, shape_mcp_entry,
                        write_default_config)

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")


def refuses(fn, *needles: str) -> tuple[bool, str]:
    """(refused with a ValueError mentioning every needle, the message)."""
    try:
        fn()
    except ValueError as e:
        msg = str(e)
        return all(n in msg for n in needles), msg
    except Exception as e:  # noqa: BLE001 - a wrong exception type is a failure, not a crash
        return False, f"{type(e).__name__}: {e}"
    return False, "no error"


TOKEN = "s3cret-header-value-77aa"
ENV_SECRET = "s3cret-env-value-11bb"
CALC = {"command": "npx", "args": ["-y", "some-mcp-calc"],
        "env": {"CALC_KEY": ENV_SECRET}}
WEB = {"url": "https://mcp.example.com/mcp", "transport": "sse",
       "headers": {"Authorization": "${ENV:MCP_TOKEN}", "X-Api-Key": "${ENV:MCP_API_KEY}"},
       "timeout_s": 30}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gek-mcpconfig-"))
    cfg_path = tmp / "config.json"
    write_default_config(cfg_path, "Test remote mcp config.")

    # ---- stdio + remote + comment entries round-trip --------------------------------------
    servers = {"_comment": "local and remote side by side", "_note": "secrets are placeholders",
               "calc": CALC, "web": WEB}
    persist_key(cfg_path, "mcp_servers", servers)
    disk = json.loads(cfg_path.read_text())["mcp_servers"]
    check(disk == servers, "stdio + remote + comment entries survive a write/read of config.json")
    cfg = load_config(cfg_path)
    check(cfg.raw["mcp_servers"] == servers, "load_config gives the entries back unchanged")
    check(cfg.raw["mcp_servers"]["_comment"] == servers["_comment"],
          "`_`-prefixed comments are still there (not turned into servers)")
    check("web" in cfg.tool_sources() and "_comment" not in cfg.tool_sources(),
          "a remote server is a grantable tool source; comments are not")
    check(mcp_kind(cfg.raw["mcp_servers"]["web"]) == "remote"
          and mcp_kind(cfg.raw["mcp_servers"]["calc"]) == "stdio",
          "mcp_kind tells the two kinds apart")
    check(mcp_transport(cfg.raw["mcp_servers"]["web"]) == "sse"
          and mcp_transport(cfg.raw["mcp_servers"]["calc"]) == "stdio",
          "mcp_transport reads the entry (stdio has none)")

    # what a server started from the default template looks like: the other keys are untouched
    other = {k: v for k, v in cfg.raw.items() if k != "mcp_servers"}
    check(other["goal"] == "Test remote mcp config." and "ceiling" in other,
          "writing mcp_servers keeps the rest of config.json")
    # and a second write doesn't lose the first entry
    persist_key(cfg_path, "mcp_servers", mcp_edit(disk, "set", "other", {"url": "http://h/mcp"}))
    disk2 = json.loads(cfg_path.read_text())["mcp_servers"]
    check(set(disk2) >= {"calc", "web", "other"}, "a second persist keeps the earlier servers")

    # ---- mcp_edit on a REMOTE entry: set / disable / enable / rm -------------------------
    out = mcp_edit(disk, "set", "fetch",
                   {"url": "https://mcp.example.com/sse", "headers": {"X-Key": "${ENV:MCP_TOKEN}"}})
    ent = out["fetch"]
    check(ent["url"] == "https://mcp.example.com/sse" and ent["transport"] == "streamable-http"
          and ent["enabled"] is True and ent["timeout_s"] == 60,
          "mcp_edit set fills in transport + timeout + enabled defaults for a remote entry")
    check("command" not in ent, "...and never invents a command for a remote entry")
    check(out["web"] == WEB and out["calc"] == CALC and out["_comment"] == servers["_comment"],
          "mcp_edit set keeps the other servers and the comments")
    check("fetch" not in disk, "mcp_edit returns a new dict (the old one is untouched)")

    off = mcp_edit(out, "disable", "web")
    check(off["web"]["enabled"] is False and off["web"]["url"] == WEB["url"]
          and off["web"]["transport"] == "sse", "mcp_edit disable works on a remote entry")
    on = mcp_edit(off, "enable", "web")
    check(on["web"]["enabled"] is True and on["web"]["headers"] == WEB["headers"],
          "mcp_edit enable restores it with its headers")
    rm = mcp_edit(on, "rm", "fetch")
    check("fetch" not in rm and "web" in rm and "_comment" in rm,
          "mcp_edit rm removes only that remote entry")
    for action in ("enable", "disable", "rm"):
        ok, msg = refuses(lambda a=action: mcp_edit(disk, a, "nope"), "nope")
        check(ok, f"mcp_edit {action} of a server that isn't there refuses clearly ({msg[:60]})")

    # ---- persisted after an edit: a round-trip keeps the edit ----------------------------
    persist_key(cfg_path, "mcp_servers", off)
    back = load_config(cfg_path).raw["mcp_servers"]
    check(back["web"]["enabled"] is False and back["web"]["headers"] == WEB["headers"],
          "a disabled remote entry round-trips through config.json")

    # ---- redacted(): header AND env values masked, raw untouched ------------------------
    red = cfg.redacted()
    rs = red["mcp_servers"]
    check(rs["web"]["headers"] == {"Authorization": "***", "X-Api-Key": "***"},
          "redacted() masks every remote header value")
    check(list(rs["web"]["headers"]) == ["Authorization", "X-Api-Key"],
          "...keeping the header names (so a log says which secret is needed)")
    check(rs["calc"]["env"] == {"CALC_KEY": "***"}, "redacted() masks a stdio server's env values")
    check(rs["web"]["url"] == WEB["url"] and rs["web"]["transport"] == "sse"
          and rs["calc"]["command"] == "npx", "...and leaves urls/transport/commands readable")
    check(mcp_header_env_names(cfg.raw["mcp_servers"]["web"]) == ["MCP_API_KEY", "MCP_TOKEN"],
          "...and mcp_header_env_names still says which variables the entry needs")
    check(cfg.raw["mcp_servers"] == servers, "config.raw still holds the real values (display copy only)")
    check(TOKEN not in json.dumps(red) and ENV_SECRET not in json.dumps(red),
          f"no secret value survives redacted() ({len(json.dumps(red))} chars scanned)")
    check(redact_mcp({}) == {} and redact_mcp({"plain": {"command": "x"}})["plain"]["command"] == "x",
          "redact_mcp is a no-op for entries with no secrets")

    # ---- header placeholders resolve at connect time, from the environment --------------
    check(mcp_header_env_names(WEB) == ["MCP_API_KEY", "MCP_TOKEN"],
          "mcp_header_env_names lists the variables a remote entry needs")
    resolved = mcp_resolve_headers(WEB, environ={"MCP_TOKEN": TOKEN, "MCP_API_KEY": "k2"},
                                   name="web")
    check(resolved == {"Authorization": TOKEN, "X-Api-Key": "k2"},
          "mcp_resolve_headers expands the placeholders")
    ok, msg = refuses(lambda: mcp_resolve_headers(WEB, environ={}, name="web"), "web", "MCP_TOKEN")
    check(ok, f"a missing header variable refuses with the server and the var name ({msg[:70]})")
    check(TOKEN not in msg, "...and never a value")
    check(mcp_resolve_headers({"command": "npx"}, name="calc") == {},
          "a stdio entry has no headers to resolve")

    # ---- url WITH command (and neither) is refused --------------------------------------
    both = {"url": "https://example.com/mcp", "command": "npx", "args": ["-y", "x"]}
    ok, msg = refuses(lambda: shape_mcp_entry(both, "web"), "web", "not both")
    check(ok, f"an entry with both url and command is refused, naming it ({msg[:80]})")
    ok, msg = refuses(lambda: mcp_edit(disk, "set", "web2", both), "web2", "not both")
    check(ok, "mcp_edit set refuses it too")
    ok, msg = refuses(lambda: shape_mcp_entry({}, "ghost"), "ghost", "command", "url")
    check(ok, f"an entry with neither refuses and says what to give ({msg[:70]})")
    ok, msg = refuses(lambda: shape_mcp_entry({"url": "ftp://x/mcp"}, "w"), "w", "http")
    check(ok, f"a non-http(s) url is refused ({msg[:60]})")
    ok, msg = refuses(lambda: shape_mcp_entry({"url": "https://x/mcp", "transport": "ws"}, "w"),
                      "w", "transport", "ws")
    check(ok, f"an unknown transport is refused ({msg[:60]})")
    ok, msg = refuses(lambda: shape_mcp_entry({"url": "https://x/mcp", "command2": "npx"}, "w"),
                      "w", "command2")
    check(ok, f"a stdio-only field on a remote entry is refused ({msg[:70]})")

    # ---- a broken remote entry can't be enabled -----------------------------------------
    broke = {"_old": "was a url", "url": "not-a-url"}
    ok, msg = refuses(lambda: mcp_edit({"b": broke}, "enable", "b"), "b", "enable")
    check(ok, f"enabling a broken remote entry refuses with a clear message ({msg[:70]})")

    bad = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    if bad:
        print("FAILED: " + "; ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
