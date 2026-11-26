"""`gek mcp-serve`: a running swarm (scripted fake model) spawns agents; an outside MCP client
launches `gek mcp-serve --agent ...` for one of them and works through it: sees only that
role's tools, writes and commits as the agent, posts, uses an MCP tool and a plugin tool
through gek, and kills another agent via the inbox.

    uv run python tests/mcpserve.py
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from gek.config import load_config, write_default_config
from gek.gitrepo import CHATTER, Repo
from gek.logs import RunLog
from gek.swarm import Swarm

_ids = itertools.count()
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")


def call(_tool: str, **args) -> dict:
    return {"id": f"c{next(_ids)}", "type": "function",
            "function": {"name": _tool, "arguments": json.dumps(args)}}


def reply(*calls):
    return ({"role": "assistant", "content": "", "tool_calls": list(calls)},
            {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})


PLUG = '''
from gek.plugin import Plugin, PluginTool
class Shout(Plugin):
    def tools(self):
        async def shout(caller, text):
            return f"{caller.id} SHOUTS {text.upper()}"
        return [PluginTool("shout", "Shout.", {"type": "object", "properties": {"text": {"type": "string"}},
                           "required": ["text"]}, shout)]
gek_plugin = Shout
'''


def text_of(res) -> str:
    return "\n".join(getattr(c, "text", "") for c in res.content)



async def remote_probe(srv: "RemoteMCPServer", token: str = "") -> dict:
    """Drive the fake remote server with the plain MCP SDK client: {"tools": [...], "add": "42"}."""
    from mcp import ClientSession

    out: dict = {"tools": []}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        if srv.kind == "sse":
            from mcp.client.sse import sse_client

            transport = sse_client(srv.url, headers=headers, timeout=10.0)
        else:
            from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

            client = create_mcp_http_client(headers=headers, timeout=10.0)
            transport = _with_client(client, streamable_http_client(srv.url, http_client=client))
        async with transport as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as s:
                await s.initialize()
                out["tools"] = sorted(t.name for t in (await s.list_tools()).tools)
                res = await s.call_tool("add", {"a": 2, "b": 40})
                out["add"] = text_of(res).strip()
    except BaseException as e:  # noqa: BLE001 - a refused client is a result, not a test crash
        out["error"] = f"{type(e).__name__}: {e}"
    return out


@asynccontextmanager
async def _with_client(client, cm):
    """the streamable-http transport needs its httpx client open for the whole session"""
    async with client:
        async with cm as streams:
            yield streams


async def client_session(cfg_path: Path, agent_id: str, work):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "gek", "--config", str(cfg_path), "mcp-serve", "--agent", agent_id],
                                   env={**os.environ})
    with open(cfg_path.parent / "serve-stderr.txt", "w") as err:
        async with stdio_client(params, errlog=err) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await work(s)


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gek-mcpserve-"))
    os.environ["GEK_HOME"] = str(tmp / "gekhome")
    cfg_path = tmp / "config.json"
    write_default_config(cfg_path, "Test mcp-serve.")
    raw = json.loads(cfg_path.read_text())
    raw["orchestrator"].update(min_sleep_s=0.5, debounce_s=0.2, inbox_poll_s=0.3)
    srv = tmp / "calc_server.py"
    srv.write_text("from mcp.server.mcpserver import MCPServer\nm = MCPServer('calc')\n"
                   "@m.tool()\ndef add(a: int, b: int) -> int:\n    return a + b\n"
                   "@m.tool()\ndef secret() -> str:\n    return 'hidden'\nm.run()\n")
    raw["mcp_servers"] = {"calc": {"command": sys.executable, "args": [str(srv)]}}
    raw["plugins"] = {"shout": {}}
    raw["ceiling"]["mcp"] = {"calc": ["*"], "shout": ["*"]}
    (tmp / "plugins").mkdir()
    (tmp / "plugins" / "shout.py").write_text(PLUG)
    cfg_path.write_text(json.dumps(raw, indent=2))
    cfg = load_config(cfg_path)
    await Repo.init(cfg.repo_dir, cfg.raw["goal"], None, None)

    log = RunLog(cfg.logs_dir, echo=False)
    swarm = Swarm(cfg, log)
    st: dict = {"spawned": False, "done": asyncio.Event()}

    async def fake(model, messages, tools, actor):
        await asyncio.sleep(0.05)
        if actor == "orchestrator":
            if not st["spawned"]:
                st["spawned"] = True
                return reply(call("write_role", name="outsider", mandate="work over MCP", writes=["src/"],
                                  powers={"kill": ["victim"], "mcp": {"calc": ["add"], "shout": ["shout"]}}),
                             call("write_role", name="victim", mandate="wait around", writes=["*"]),
                             call("spawn", role="outsider", note="driven from outside"),
                             call("spawn", role="victim", note="be killed"),
                             call("sleep", seconds=600, reason="wait"))
            if st["done"].is_set():
                return reply(call("halt", reason="done"))
            return reply(call("sleep", seconds=600, reason="wait"))
        await asyncio.sleep(0.3)  # in-harness agents just idle
        return reply(call("read_log", refs=["swarm/chatter"], limit=1))

    swarm.llm.fake = fake
    run = asyncio.create_task(swarm.run())
    for _ in range(200):
        if len(swarm.alive()) == 2:
            break
        await asyncio.sleep(0.1)
    outsider = next(a.id for a in swarm.agents.values() if a.role.name == "outsider")
    victim = next(a.id for a in swarm.agents.values() if a.role.name == "victim")
    # the harness's own copy of the outsider would race us; stop it, keep its worktree
    out: dict = {}

    async def work(s):
        listed = await s.list_tools()
        out["tools"] = sorted(t.name for t in listed.tools)
        out["write"] = text_of(await s.call_tool("write_file", {"path": "src/x.py", "content": "X = 1\n"}))
        out["commit"] = text_of(await s.call_tool("commit", {"message": "from outside"}))
        out["post"] = text_of(await s.call_tool("post", {"message": "hello over mcp"}))
        out["add"] = text_of(await s.call_tool("mcp__calc__add", {"a": 2, "b": 40}))
        out["shout"] = text_of(await s.call_tool("shout__shout", {"text": "hi"}))
        out["agents"] = text_of(await s.call_tool("list_agents", {}))
        out["kill"] = text_of(await s.call_tool("kill", {"agent_id": victim, "reason": "test"}))
        res = await s.call_tool("merge", {"branch": "main"})
        out["merge_err"] = (res.is_error, text_of(res))
        res = await s.call_tool("read_file", {"path": "../../config.json"})
        out["escape"] = (res.is_error, text_of(res))
        return out

    await client_session(cfg_path, outsider, work)
    for _ in range(100):
        if swarm.agents[victim].status == "killed":
            break
        await asyncio.sleep(0.1)
    st["done"].set()
    swarm.wake.set()
    await asyncio.wait_for(run, timeout=60)
    log.close()

    t = out.get("tools", [])
    check("write_file" in t and "commit" in t and "kill" in t and "list_agents" in t,
          "the client sees the role's core tools")
    check("merge" not in t and "run" not in t and "interrupt" not in t, "...and not the powers it lacks")
    check("mcp__calc__add" in t and "mcp__calc__secret" not in t, "granted MCP tools only")
    check("shout__shout" in t, "plugin tools too")
    repo = Repo(cfg.repo_dir)
    branch = f"role/{outsider.replace('/', '/')}"
    logline = await repo.git("log", "-1", "--format=%an|%s", f"refs/heads/{branch}")
    check("committed" in out.get("commit", "") and logline.strip().startswith(f"{outsider}|from outside"),
          "commits land on the agent's branch, as the agent")
    chatter = await repo.log_text([CHATTER], limit=20)
    check("hello over mcp" in chatter, "posts reach chatter")
    check(out.get("add", "").strip() == "42", "MCP tools work through gek")
    check(f"{outsider} SHOUTS HI" in out.get("shout", ""), "plugin tools know the caller")
    check(victim in out.get("agents", ""), "list_agents reads the running swarm")
    check(swarm.agents[victim].status == "killed", "kill goes through the inbox and the harness carries it out")
    check(out.get("merge_err", (False,))[0], "unknown tools come back as MCP errors")
    check(out.get("escape", (False,))[0] and "outside your worktree" in out["escape"][1],
          "paths outside the worktree are refused")
    ev_dirs = [d for d in (cfg.logs_dir / "mcp-serve").iterdir() if d.is_dir()]
    sev = [json.loads(l) for d in ev_dirs for l in open(d / "events.jsonl")]
    check(any(e["kind"] == "tool_call" and e["data"]["name"] == "commit" for e in sev),
          "serve logs every call under logs/mcp-serve/")
    from gek.logs import list_runs

    check(all("mcp-serve" not in str(p) for p in list_runs(cfg.logs_dir)), "...which isn't mistaken for a run")
    import subprocess

    r = subprocess.run([sys.executable, "-m", "gek", "--config", str(cfg_path), "mcp-serve", "--agent",
                        outsider, "--print-config"], capture_output=True, text=True)
    conf = json.loads(r.stdout or "{}")
    check("mcp-serve" in json.dumps(conf) and outsider in json.dumps(conf), "--print-config gives a client snippet")
    r = subprocess.run([sys.executable, "-m", "gek", "--config", str(cfg_path), "mcp-serve", "--agent", "ghost/0000"],
                       capture_output=True, text=True)
    check(r.returncode == 2 and "no worktree" in r.stderr, "an agent with no worktree is a clear error")

    # the same calc server, but remote: streamable-http then sse, on localhost ephemeral ports,
    # each shut down cleanly (the bridge/config tests in tests/mcpremote.py + tests/mcpconfig.py
    # import these helpers)
    why = remote_available()
    check(why is None, f"a fake remote MCP server can run here (mcp {_mcp_version()})"
                       + (f": {why}" if why else ""))
    for kind in ("streamable-http", "sse"):
        rsrv = await RemoteMCPServer.start(kind, token="s3cret")
        try:
            check(rsrv.url.endswith(rsrv.path) and rsrv.listening(),
                  f"fake {kind} MCP server listens on its ephemeral port ({rsrv.port})")
            got = await remote_probe(rsrv, token="s3cret")
            check(got.get("tools") == ["add", "secret"] and got.get("add") == "42",
                  f"fake {kind} server lists and calls its tools over the wire")
            got_bad = await remote_probe(rsrv, token="wrong")
            check(not got_bad.get("tools"),
                  f"fake {kind} server refuses a client without the configured header")
        finally:
            code = await rsrv.stop()
        check(code is not None and not rsrv.listening(),
              f"fake {kind} MCP server shuts down cleanly (exit {code}, port free)")

    bad = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    if bad:
        print(json.dumps(out, indent=1, default=str))
        print((tmp / "serve-stderr.txt").read_text()[-3000:])
    return 1 if bad else 0



# --------------------------------------------------------------- a fake *remote* MCP server
# The same little `calc` server tests above drive over stdio, speaking streamable-http (or sse) on a
# localhost ephemeral port, with an optional bearer-token gate so a test can prove that a config
# entry's `headers` actually reached the wire. Each server is its own subprocess (uvicorn), so it
# needs no event loop of its own and shutting down is terminate + wait: nothing is left listening.
#
#   srv = await RemoteMCPServer.start("streamable-http", token="s3cret")
#   srv.url, srv.port, srv.kind, srv.path, srv.spec   (spec = the mcp_servers entry for it)
#   await srv.stop()
#
# Requires the `mcp` SDK's server-side http transports (MCPServer.streamable_http_app / sse_app) and
# uvicorn; remote_available() says whether this install has them, and says why not if it doesn't.

import asyncio
import dataclasses
import socket
import subprocess
import sys
import tempfile
from pathlib import Path


def remote_available() -> str | None:
    """None when the fake remote server can run here, else a one-line reason (for chatter/FAIL)."""
    try:
        import uvicorn  # noqa: F401
        from mcp.server.mcpserver import MCPServer

        for app in ("streamable_http_app", "sse_app"):
            if not hasattr(MCPServer, app):
                return f"mcp {_mcp_version()} has no MCPServer.{app}"
    except Exception as e:  # noqa: BLE001 - the reason is the point
        return f"{type(e).__name__}: {e}"
    return None


def _mcp_version() -> str:
    try:
        from importlib.metadata import version

        return version("mcp")
    except Exception:  # noqa: BLE001
        return "?"


def free_port() -> int:
    """An ephemeral port that was free a moment ago (the server binds it right after)."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


@dataclasses.dataclass
class RemoteMCPServer:
    """A running fake remote MCP server. Start with `await RemoteMCPServer.start()`."""

    kind: str  # "streamable-http" | "sse"
    port: int
    proc: subprocess.Popen
    token: str = ""
    errlog: Path | None = None

    @property
    def path(self) -> str:
        return "/sse" if self.kind == "sse" else "/mcp"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.path}"

    @property
    def spec(self) -> dict:
        """The mcp_servers entry a test would put in config.json for this server."""
        spec: dict = {"url": self.url}
        if self.kind != "streamable-http":
            spec["transport"] = self.kind
        if self.token:
            spec["headers"] = {"Authorization": "Bearer ${ENV:MCP_TOKEN}"}
        return spec

    @classmethod
    async def start(cls, kind: str = "streamable-http", token: str = "",
                    timeout: float = 30.0) -> "RemoteMCPServer":
        why = remote_available()
        if why:
            raise RuntimeError(f"no server-side http transport here: {why}")
        port = free_port()
        err = tempfile.NamedTemporaryFile("w+", prefix="remotemcp-", suffix=".log", delete=False)
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve-remote", kind,
                                 str(port), token], stdout=subprocess.PIPE, stderr=err,
                                text=True)
        srv = cls(kind=kind, port=port, proc=proc, token=token, errlog=Path(err.name))
        try:
            line = await asyncio.wait_for(asyncio.to_thread(proc.stdout.readline), timeout)
        except asyncio.TimeoutError:
            srv._kill()
            raise RuntimeError(f"fake MCP server ({kind}) didn't come up in {timeout}s:\n"
                               f"{srv.stderr()[-2000:]}")
        if "ready" not in line:
            srv._kill()
            raise RuntimeError(f"fake MCP server ({kind}) didn't start: {line!r}\n{srv.stderr()[-2000:]}")
        return srv

    def stderr(self) -> str:
        if not self.errlog or not self.errlog.exists():
            return ""
        return self.errlog.read_text(errors="replace")

    def _kill(self) -> None:
        try:
            self.proc.kill()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass

    def listening(self) -> bool:
        with socket.socket() as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    async def stop(self, timeout: float = 10.0) -> int:
        """Shut the server down and wait for it: returns the exit code (no port left open)."""
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            code = await asyncio.to_thread(self.proc.wait, timeout)
        except subprocess.TimeoutExpired:
            self._kill()
            code = self.proc.returncode or -9
        if self.errlog:
            self.errlog.unlink(missing_ok=True)
        return int(code)


def serve_remote(kind: str, port: int, token: str = "") -> None:
    """Run one fake server (what the subprocess does). Prints `ready <path>` once uvicorn is up."""
    import uvicorn
    from mcp.server.mcpserver import MCPServer

    m = MCPServer("calc")

    @m.tool()
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @m.tool()
    def secret() -> str:
        """A tool nobody is granted (tests keep only what a role was granted)."""
        return "hidden"

    app = m.sse_app() if kind == "sse" else m.streamable_http_app()

    class Gate:
        """ASGI wrapper: without the configured bearer token, every request is a 401."""

        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http" or not token:
                return await self.inner(scope, receive, send)
            got = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
            if got != f"Bearer {token}":
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return None
            return await self.inner(scope, receive, send)

    if token:
        app.add_middleware(Gate)
    path = "/sse" if kind == "sse" else "/mcp"
    # run uvicorn in a thread so `ready` means the socket is actually bound (a client may connect
    # the instant it reads the line)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(1000):
        if server.started or not th.is_alive():
            break
        time.sleep(0.02)
    if not server.started:
        print("failed to start " + path, flush=True)
        sys.exit(1)
    print(f"ready {path}", flush=True)
    th.join()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "serve-remote":
        _kind = sys.argv[2] if len(sys.argv) > 2 else "streamable-http"
        _port = int(sys.argv[3]) if len(sys.argv) > 3 else free_port()
        serve_remote(_kind, _port, sys.argv[4] if len(sys.argv) > 4 else "")
    else:
        sys.exit(asyncio.run(main()))
