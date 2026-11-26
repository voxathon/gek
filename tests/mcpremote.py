"""MCPBridge against a *remote* MCP server: the config entry {url, transport, headers} really
connects over the wire, lists the granted tools, calls one, and closes. A bad entry (unreachable
url, a missing ${ENV:NAME} header variable) ends with no tools and nothing escaping -- and no
secret value anywhere in the log or the error text.

    uv run python tests/mcpremote.py

Uses the fake remote MCP server in tests/mcpserve.py (streamable-http and sse, 127.0.0.1 ephemeral
port, ASGI bearer gate so `headers` has to reach the wire).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from gek.logs import RunLog
from gek.mcp_bridge import MCPBridge

TOKEN = "s3cret-remote-token-9f83"
MISSING_VAR = "GEK_TEST_ABSENT_HEADER_VAR"
results: list[tuple[bool, str]] = []
LOG: RunLog | None = None


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")


async def call_tool(bridge: MCPBridge, name: str, args: dict) -> tuple[str, str]:
    """(text, escaped-error) -- a KeyError out of call() is a failed check, not a test crash."""
    try:
        return await bridge.call(name, args), ""
    except BaseException as e:  # noqa: BLE001
        return "", f"{type(e).__name__}: {e}"


async def with_bridge(grants: dict, servers: dict, timeout: float = 90.0) -> tuple[MCPBridge, str]:
    """Connect a bridge; returns (bridge, escaped-exception-text). Nothing should ever escape."""
    bridge = MCPBridge("tester", LOG)
    err = ""
    try:
        await asyncio.wait_for(bridge.connect(grants, servers), timeout)
    except BaseException as e:  # noqa: BLE001 - an escaped exception is exactly what we test for
        err = f"{type(e).__name__}: {e}"
    return bridge, err


def event_text() -> str:
    """Everything the bridge wrote about itself: event records + the human-readable log."""
    out = []
    for p in (LOG.dir / "events.jsonl", LOG.dir / "gek.log"):
        if p.exists():
            out.append(p.read_text(errors="replace"))
    for p in sorted(LOG.dir.glob("*.stderr.log")):  # a remote server writes nothing here, but be sure
        out.append(p.read_text(errors="replace"))
    return "\n".join(out)


def mcp_events(server: str = "") -> list[dict]:
    p = LOG.dir / "events.jsonl"
    if not p.exists():
        return []
    evs = [e for e in (json.loads(line) for line in p.read_text().splitlines() if line.strip())
           if e["kind"] == "mcp"]
    return [e for e in evs if not server or e["data"].get("server") == server]


def last(server: str) -> dict:
    evs = mcp_events(server)
    return evs[-1]["data"] if evs else {}


async def main() -> int:
    global LOG
    tmp = Path(tempfile.mkdtemp(prefix="gek-mcpremote-"))
    LOG = RunLog(tmp / "logs", echo=False)

    from mcpserve import RemoteMCPServer, free_port, remote_available

    why = remote_available()
    check(why is None, "a fake remote MCP server can run here"
                       + (f" -- missing dependency: {why}" if why else ""))
    if why:
        print(f"\n0/1 passed -- the installed mcp SDK has no server-side http transport: {why}")
        return 1

    os.environ["MCP_TOKEN"] = TOKEN  # what ${ENV:MCP_TOKEN} expands from, at connect time
    os.environ.pop(MISSING_VAR, None)

    # ---- the real path, per transport: url+headers entry -> connect, list, call, close ------
    for kind in ("streamable-http", "sse"):
        srv = await RemoteMCPServer.start(kind, token=TOKEN)
        try:
            spec = srv.spec  # {url, transport?, headers: {Authorization: "${ENV:MCP_TOKEN}"}}
            check(spec.get("url") == srv.url and "command" not in spec and "headers" in spec,
                  f"{kind}: a remote config entry is url + headers, with no command")
            bridge, err = await with_bridge({"calc": ["add"]}, {"calc": spec})
            try:
                names = [t.name for t in bridge.listed]
                check(not err, f"{kind}: connect() raised nothing" + (f" (got {err})" if err else ""))
                check(names == ["mcp__calc__add"],
                      f"{kind}: exactly the granted remote tools are listed ({names})")
                check(bridge.sessions.get("calc") is not None, f"{kind}: a live session is kept")
                got, cerr = await call_tool(bridge, "mcp__calc__add", {"a": 2, "b": 40})
                check(got.strip() == "42" and not cerr,
                      f"{kind}: calling the remote tool gives 42 (got {got!r} {cerr})")
            finally:
                await bridge.close()
            check(not bridge.sessions and not bridge.listed,
                  f"{kind}: close() leaves no session behind")
            # the server 401s every request without the configured bearer token, so a successful
            # connect proves the entry's headers reached the wire
            ev = last("calc")
            check(ev.get("ok") is True,
                  f"{kind}: the connect is logged ok, i.e. the headers reached the server ({ev})")
            check(srv.listening(), f"{kind}: the server is still up after close()")
        finally:
            await srv.stop()

    # ---- a broken entry next to a good one: the broken one doesn't take the good one down --
    good = await RemoteMCPServer.start("streamable-http", token=TOKEN)
    try:
        bridge, err = await with_bridge(
            {"calc": ["*"], "ghost": ["*"]},
            {"calc": good.spec,
             "ghost": {"url": f"http://127.0.0.1:{free_port()}/mcp", "timeout_s": 5}})
        try:
            names = [t.name for t in bridge.listed]
            check(not err and "mcp__calc__add" in names,
                  "a broken entry beside a good one still yields the good server's tools")
            check(not any(n.startswith("mcp__ghost__") for n in names),
                  "...and no tools from the broken one")
            got, cerr = await call_tool(bridge, "mcp__calc__add", {"a": 2, "b": 40})
            check(got.strip() == "42" and not cerr, "...and the good server is still callable")
        finally:
            await bridge.close()
    finally:
        await good.stop()

    # ---- an unreachable entry on its own: no tools, nothing escapes, logged as a failure ---
    bridge, err = await with_bridge(
        {"ghost": ["*"]}, {"ghost": {"url": f"http://127.0.0.1:{free_port()}/mcp", "timeout_s": 5}})
    await bridge.close()
    check(not err, "an unreachable remote server raises nothing out of connect()"
                   + (f" (got {err})" if err else ""))
    check(not bridge.listed and not bridge.sessions, "an unreachable remote server yields no tools")
    ev = last("ghost")
    check(ev.get("ok") is False and bool(ev.get("error")),
          f"the unreachable server is logged as a failure, not silently dropped ({ev})")
    check("Traceback (most recent call last)" not in event_text(),
          "the failure is logged as a message, not a traceback")

    # ---- a header whose ${ENV:NAME} variable is missing (against a *live* server, so the
    # failure can only be the header) ------------------------------------------------------
    srv = await RemoteMCPServer.start("streamable-http", token=TOKEN)
    try:
        spec = srv.spec
        spec["headers"] = {"Authorization": f"${{ENV:{MISSING_VAR}}}"}
        bridge, err = await with_bridge({"calc": ["add"]}, {"calc": spec})
        await bridge.close()
        check(not err, "a missing ${ENV:NAME} header variable raises nothing out of connect()"
                       + (f" (got {err})" if err else ""))
        check(not bridge.listed and not bridge.sessions,
              "a missing ${ENV:NAME} header variable yields no tools")
        ev = last("calc")
        check(ev.get("ok") is False, f"...logged as a failure ({ev})")
        text = event_text()
        check(MISSING_VAR in json.dumps(ev) or MISSING_VAR in text,
              f"the error names the missing env var ({MISSING_VAR}) so it can be fixed")
        check("calc" in json.dumps(ev), "...and the server it belongs to")
    finally:
        await srv.stop()

    # ---- secrets: no header value may reach the log, the events, or an error string --------
    text = event_text()
    check(TOKEN not in text, f"no header value appears in the log ({len(text)} chars scanned)")
    check("MCP_TOKEN" in text or MISSING_VAR in text,
          "header/env *names* are logged where useful (never values)")
    LOG.close()

    bad = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    if bad:
        print("FAILED: " + "; ".join(bad))
        print("---- log tail ----\n" + text[-2500:])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
