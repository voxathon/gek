"""`gek tui --web`: the TUI in a browser (textual-serve), behind a password.

Browsers can't put headers on a websocket, so the password becomes a session cookie: a login
page takes it once, the server answers with an HttpOnly, SameSite=Strict cookie, and every
route checks it, including the /ws websocket the TUI runs over. Plain HTTP on a LAN still
sends the cookie in the clear: use Tailscale or an HTTPS proxy for anything past your LAN.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import secrets
import time

from aiohttp import web
from textual_serve.server import Server

COOKIE = "gek_session"

LOGIN_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>gek: log in</title>
<style>
  body {{ background:#0c181f; color:#d8dee9; font-family: ui-monospace, Menlo, Consolas, monospace;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
  form {{ background:#13232d; padding:28px 32px; border:1px solid #2e4a5a; border-radius:8px; width:min(340px, 86vw); }}
  h1 {{ font-size:18px; margin:0 0 14px; }}
  input {{ width:100%; box-sizing:border-box; padding:10px; margin:6px 0 14px; font:inherit;
          background:#0c181f; color:#d8dee9; border:1px solid #2e4a5a; border-radius:4px; }}
  button {{ width:100%; padding:10px; font:inherit; background:#3b6e8f; color:white; border:0; border-radius:4px; }}
  .err {{ color:#e06c75; margin:0 0 10px; }}
</style></head>
<body><form method="post" action="/login">
  <h1>gek</h1>
  {error}
  <label for="pw">password</label>
  <input id="pw" name="password" type="password" autofocus autocomplete="current-password">
  <button type="submit">log in</button>
</form></body></html>"""


class GekWebServer(Server):
    """textual-serve with a password gate in front of every route."""

    def __init__(self, command: str, password: str | None, **kw):
        super().__init__(command, **kw)
        self.password = password
        self.session = secrets.token_urlsafe(32)  # new each start: restarting logs everyone out
        self._fails: list[float] = []

    def _authed(self, request: web.Request) -> bool:
        return hmac.compare_digest(request.cookies.get(COOKIE, ""), self.session)

    @web.middleware
    async def _gate(self, request: web.Request, handler):
        if request.path == "/login" or self._authed(request):
            return await handler(request)
        if request.path == "/ws":
            raise web.HTTPUnauthorized(text="log in first")
        raise web.HTTPFound("/login")

    async def _login_page(self, request: web.Request) -> web.Response:
        return web.Response(text=LOGIN_PAGE.format(error=""), content_type="text/html")

    async def _login(self, request: web.Request) -> web.Response:
        now = time.time()
        self._fails = [t for t in self._fails if now - t < 60]
        if len(self._fails) >= 10:  # crude brake on guessing
            await asyncio.sleep(5)
        data = await request.post()
        given = str(data.get("password", ""))
        if self.password and hmac.compare_digest(given.encode(), self.password.encode()):
            resp = web.HTTPFound("/")
            resp.set_cookie(COOKIE, self.session, httponly=True, samesite="Strict",
                            secure=request.secure, max_age=30 * 24 * 3600, path="/")
            raise resp
        self._fails.append(now)
        await asyncio.sleep(1)
        page = LOGIN_PAGE.format(error=f'<p class="err">{html.escape("wrong password")}</p>')
        return web.Response(text=page, content_type="text/html", status=401)

    async def _logout(self, request: web.Request) -> web.Response:
        resp = web.HTTPFound("/login")
        resp.del_cookie(COOKIE, path="/")
        raise resp

    async def _make_app(self) -> web.Application:
        app = await super()._make_app()
        if self.password:
            app.middlewares.append(self._gate)
            app.router.add_get("/login", self._login_page)
            app.router.add_post("/login", self._login)
            app.router.add_get("/logout", self._logout)
        return app


def is_loopback(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1")
