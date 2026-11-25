"""notify: push chosen events to ntfy or any webhook, so you find out the swarm halted without
watching it. Also gives agents a `send` tool for paging you, if a role is granted it.

    "plugins": {
      "notify": {
        "url": "https://ntfy.sh/my-secret-topic",   // or any URL that takes a POST
        "on": ["run_end", "merge", "llm_error"],    // event kinds (default shown)
        "format": "ntfy",                           // ntfy (plain text + headers) or json; guessed from the url
        "cooldown_s": 60,                           // at most one push per kind per this long (run_end always goes)
        "headers": {"Authorization": "Bearer ..."}  // optional
      }
    }

Grant agents the tool:  powers.mcp {"notify": ["send"]}  (within ceiling.mcp {"notify": ["*"]}).
Standard library only.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from typing import Any

from gek.logs import feed_line
from gek.plugin import Caller, Plugin, PluginTool

DEFAULT_ON = ["run_end", "merge", "llm_error"]


class Notify(Plugin):
    def __init__(self, ctx, config):
        super().__init__(ctx, config)
        self.url = str(config.get("url") or "")
        if not self.url:
            raise ValueError('notify needs "url" (an ntfy topic URL or a webhook)')
        self.on = set(config.get("on") or DEFAULT_ON)
        self.format = config.get("format") or ("ntfy" if "ntfy" in self.url else "json")
        self.cooldown = float(config.get("cooldown_s", 60))
        self.headers = dict(config.get("headers") or {})
        self.last: dict[str, float] = {}
        self.sent = 0

    def _text(self, ev: dict[str, Any]) -> str:
        d = ev.get("data") or {}
        if ev["kind"] == "run_end":
            return (f"swarm {self.ctx.cfg.name} stopped: {d.get('reason')} "
                    f"({d.get('tokens', 0):,} tokens, {d.get('agents', 0)} agents)")
        try:
            line = feed_line(ev["kind"], ev.get("actor", ""), d)
        except Exception:  # noqa: BLE001
            line = None
        return (line or f"{ev['kind']} {ev.get('actor', '')}").strip()

    def _post(self, title: str, text: str, tags: str = "") -> int:
        if self.format == "ntfy":
            body = text.encode()
            headers = {"Title": title, **({"Tags": tags} if tags else {}), **self.headers}
        else:
            body = json.dumps({"title": title, "text": text, "swarm": self.ctx.cfg.name}).encode()
            headers = {"Content-Type": "application/json", **self.headers}
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status

    async def send(self, title: str, text: str, tags: str = "") -> int:
        status = await asyncio.to_thread(self._post, title, text, tags)
        self.sent += 1
        self.ctx.log(sent=title, text=text[:300], status=status)
        return status

    async def on_event(self, ev: dict[str, Any]) -> None:
        kind = ev.get("kind")
        if kind not in self.on:
            return
        now = time.time()
        if kind != "run_end" and now - self.last.get(kind, 0) < self.cooldown:
            return
        self.last[kind] = now
        tags = {"run_end": "checkered_flag", "merge": "twisted_rightwards_arrows",
                "llm_error": "warning"}.get(kind, "")
        await self.send(f"gek {self.ctx.cfg.name}: {kind}", self._text(ev), tags)

    def tools(self) -> list[PluginTool]:
        async def send(caller: Caller, message: str) -> str:
            status = await self.send(f"gek {self.ctx.cfg.name}: {caller.id}", message, "speech_balloon")
            return f"sent (HTTP {status})"

        return [PluginTool("send", "Push a short message to the human's phone. Only for things they "
                           "need to know now (blocked, needs a decision, something broke).",
                           {"type": "object", "properties": {"message": {"type": "string"}},
                            "required": ["message"]}, send)]


gek_plugin = Notify
