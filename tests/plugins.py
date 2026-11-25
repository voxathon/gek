"""Plugins: discovery and precedence, config gating, events, inbox posts, plugin tools granted
like MCP tools, crash isolation, and the bundled notify plugin against a local HTTP server.
Scripted fake model, no API key.

    uv run python tests/plugins.py
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from gek.config import load_config, write_default_config
from gek.gitrepo import Repo
from gek.logs import RunLog
from gek.roles import Role, validate
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


PROBE = '''
from gek.plugin import Plugin, PluginTool

class Probe(Plugin):
    async def start(self):
        self.kinds = []
        await self.ctx.post_inbox("hello from the probe plugin")

    async def on_event(self, ev):
        self.kinds.append(ev["kind"])
        if ev["kind"] == "agent_spawn" and not getattr(self, "logged", False):
            self.logged = True
            self.ctx.log(saw_spawn=ev["data"]["id"])

    async def stop(self):
        self.ctx.log(stopping=True, saw_run_end="run_end" in self.kinds, n=len(self.kinds))

    def tools(self):
        async def echo(caller, text):
            return f"echo from probe to {caller.id} ({caller.role}): {text}"
        async def secret(caller):
            return "should never be granted"
        return [PluginTool("echo", "Echo text back.", {"type": "object", "properties": {"text": {"type": "string"}},
                           "required": ["text"]}, echo),
                PluginTool("secret", "Not granted.", {"type": "object", "properties": {}}, secret)]

gek_plugin = Probe
'''

BOOM = '''
from gek.plugin import Plugin

class Boom(Plugin):
    async def on_event(self, ev):
        raise RuntimeError("kaboom")

gek_plugin = Boom
'''

EVIL = '''
import pathlib, os
pathlib.Path(os.environ["GEK_EVIL_OUT"]).write_text("loaded")
from gek.plugin import Plugin
gek_plugin = Plugin
'''

SHADOW = '''
from gek.plugin import Plugin
class P(Plugin):
    async def start(self):
        self.ctx.log(which="{where}")
gek_plugin = P
'''


class Hook(BaseHTTPRequestHandler):
    got: list[dict] = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        Hook.got.append({"path": self.path, "headers": dict(self.headers), "body": body})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gek-plugins-"))
    home = tmp / "gekhome"
    os.environ["GEK_HOME"] = str(home)
    os.environ["GEK_EVIL_OUT"] = str(tmp / "evil.txt")
    httpd = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    sw = tmp / "swarm"
    (sw / "plugins").mkdir(parents=True)
    (sw / "plugins" / "probe.py").write_text(PROBE)
    (sw / "plugins" / "boom.py").write_text(BOOM)
    (sw / "plugins" / "evil.py").write_text(EVIL)
    (sw / "plugins" / "shadow.py").write_text(SHADOW.replace("{where}", "swarm"))
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "shadow.py").write_text(SHADOW.replace("{where}", "home"))
    cfg_path = sw / "config.json"
    write_default_config(cfg_path, "Test plugins.")
    raw = json.loads(cfg_path.read_text())
    raw["orchestrator"].update(min_sleep_s=0.5, debounce_s=0.2, inbox_poll_s=0.3)
    raw["plugins"] = {
        "probe": {},
        "boom": {},
        "shadow": {},
        "notify": {"url": f"http://127.0.0.1:{port}/hook", "on": ["run_end", "merge"], "format": "json"},
        "off": {"enabled": False},
    }
    raw["ceiling"]["mcp"] = {"probe": ["*"], "notify": ["send"]}
    cfg_path.write_text(json.dumps(raw, indent=2))
    cfg = load_config(cfg_path)
    await Repo.init(cfg.repo_dir, cfg.raw["goal"], None, None)

    # validation knows plugins are tool sources
    ok_role = Role.from_dict({"name": "w", "mandate": "m", "powers": {"tools": {"probe": ["echo"]}}})
    bad_role = Role.from_dict({"name": "w", "mandate": "m", "powers": {"mcp": {"nope": ["*"]}}})
    check(not validate(ok_role, cfg.raw["ceiling"], cfg.tool_sources()), "a configured plugin is a grantable tool source")
    check(any("nope" in e for e in validate(bad_role, cfg.raw["ceiling"], cfg.tool_sources())),
          "an unknown tool source is rejected")
    check(ok_role.powers.mcp == {"probe": ["echo"]}, "powers.tools is accepted as another name for powers.mcp")

    log = RunLog(cfg.logs_dir, echo=False)
    swarm = Swarm(cfg, log)
    seen: dict = {"inbox": False, "brief": "", "tools": [], "out": {}}

    async def fake(model, messages, tools, actor):
        await asyncio.sleep(0.02)
        if actor == "orchestrator":
            text = messages[1]["content"]
            if "hello from the probe plugin" in text:
                seen["inbox"] = True
            if not seen["brief"]:
                seen["brief"] = text
                return reply(call("write_role", name="worker", mandate="use plugin tools", writes=["*"],
                                  powers={"mcp": {"probe": ["echo"], "notify": ["send"]}}),
                             call("spawn", role="worker", note="go"),
                             call("sleep", seconds=600, reason="wait"))
            if seen["out"].get("done"):
                return reply(call("halt", reason="done"))
            return reply(call("sleep", seconds=600, reason="wait"))
        if not seen["tools"]:
            seen["tools"] = sorted(t["function"]["name"] for t in tools)
            return reply(call("probe__echo", text="hi"))
        last = messages[-1]["content"]
        if "echo from probe" in last:
            seen["out"]["echo"] = last
            return reply(call("notify__send", message="page the human"))
        if "sent (HTTP" in last or "ERROR" in last:
            seen["out"]["send"] = last
            seen["out"]["done"] = True
            return reply(call("finish", summary="used the plugin tools"))
        return reply(call("finish", summary="confused: " + last[:80]))

    swarm.llm.fake = fake
    await asyncio.wait_for(swarm.run(), timeout=90)
    log.close()
    ev = [json.loads(l) for l in open(log.dir / "events.jsonl")]
    pl = [e for e in ev if e["kind"] == "plugin"]
    loaded = {e["actor"].split(":", 1)[1]: e["data"] for e in pl if e["data"].get("loaded")}

    check(set(loaded) == {"probe", "boom", "shadow", "notify"}, "only enabled, configured plugins load")
    check(not (tmp / "evil.txt").exists(), "a plugin file that isn't in config never runs")
    check(loaded.get("probe", {}).get("where") == "swarm" and len(loaded["probe"].get("sha256", "")) == 16,
          "loads are logged with where, path and sha256")
    check(loaded.get("notify", {}).get("where") == "bundled", "notify comes bundled")
    check(any(e["data"].get("which") == "swarm" for e in pl if e["actor"] == "plugin:shadow"),
          "the swarm's plugin folder wins over GEK_HOME's")
    check(seen["inbox"], "a plugin's inbox post reaches the orchestrator")
    check('"probe": ["echo", "secret"]' in seen["brief"] and "notify" in seen["brief"],
          "the orchestrator's briefing lists plugin tools")
    check("probe__echo" in seen["tools"] and "probe__secret" not in seen["tools"]
          and "notify__send" in seen["tools"], "agents see exactly the plugin tools they were granted")
    wid = next((e["data"]["id"] for e in ev if e["kind"] == "agent_spawn"), "")
    check(f"to {wid} (worker): hi" in seen["out"].get("echo", ""), "plugin tools know who called them")
    tev = next((e for e in ev if e["kind"] == "tools"), None)
    check(tev is not None and set(tev["data"]["sources"]) >= {"gek", "probe", "notify"},
          "the tools event lists each source")
    check(any(e["data"].get("saw_spawn") == wid for e in pl), "plugins get every event")
    check(any(e["data"].get("stopping") and e["data"].get("saw_run_end") for e in pl),
          "stop runs after run_end, which plugins see")
    boom = [e for e in pl if e["actor"] == "plugin:boom"]
    check(any(e["data"].get("disabled") for e in boom) and sum(1 for e in boom if e["data"].get("error")) == 4,
          "a plugin that keeps raising is disabled after 5 errors")
    check(any(e["kind"] == "run_end" for e in ev) and "HTTP 200" in seen["out"].get("send", ""),
          "...and the swarm carries on")

    bodies = [json.loads(g["body"]) for g in Hook.got]
    check(any("page the human" in b["text"] and wid in b["title"] for b in bodies), "notify: agents can page you")
    check(any("stopped: done" in b["text"] for b in bodies), "notify: run_end is pushed")
    check(all(g["path"] == "/hook" for g in Hook.got), "notify: posts to the configured URL")

    # ntfy format, cooldown
    from gek.plugins.notify import Notify

    the_cfg = cfg

    class Ctx:
        cfg = the_cfg
        logged: list = []

        def log(self, **d):
            Ctx.logged.append(d)

    Hook.got.clear()
    n = Notify(Ctx(), {"url": f"http://127.0.0.1:{port}/ntfy-topic", "on": ["merge", "run_end"], "cooldown_s": 60})
    m = {"kind": "merge", "actor": "rev/1", "data": {"branch": "role/x/1", "ok": True, "sha": "abc"}}
    await n.on_event(m)
    await n.on_event(m)
    await n.on_event({"kind": "run_end", "actor": "harness", "data": {"reason": "halt", "tokens": 5}})
    check(n.format == "ntfy" and len(Hook.got) == 2, "notify: ntfy format guessed from the URL; cooldown drops repeats")
    check(Hook.got and Hook.got[0]["headers"].get("Title", "").endswith("merge")
          and not Hook.got[0]["body"].startswith("{"), "notify: ntfy gets plain text and a Title header")
    try:
        Notify(Ctx(), {})
        check(False, "notify without a url refuses to load")
    except ValueError:
        check(True, "notify without a url refuses to load")

    # gek plugins
    r = subprocess.run([sys.executable, "-m", "gek", "--config", str(cfg_path), "plugins"],
                       capture_output=True, text=True, env={**os.environ})
    out = r.stdout
    check("enabled   probe" in out and "disabled  off" in out and "evil" in out and "sha256:" in out,
          "gek plugins shows installed vs enabled, with hashes")
    httpd.shutdown()

    bad = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    if bad:
        print(out, r.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
