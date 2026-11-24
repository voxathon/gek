"""`gek tui --web` login: refuses to serve beyond localhost without a password, redirects to
the login page, rejects wrong passwords, sets a session cookie, and gates the /ws websocket.

    uv run python tests/web_login.py
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="gek-web-"))
    env = {**os.environ, "GEK_HOME": str(home)}
    env.pop("GEK_WEB_PASSWORD", None)
    checks = {}

    # no password + 0.0.0.0 = refused
    r = subprocess.run([sys.executable, "-m", "gek", "tui", "--web", "--host", "0.0.0.0",
                        "--public-url", "http://x:1"], capture_output=True, text=True, env=env, timeout=60)
    checks["refuses 0.0.0.0 without a password"] = r.returncode == 2 and "GEK_WEB_PASSWORD" in r.stderr

    port = free_port()
    env["GEK_WEB_PASSWORD"] = "correct horse"
    proc = subprocess.Popen([sys.executable, "-m", "gek", "tui", "--web", "--port", str(port)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://localhost:{port}"
    try:
        for _ in range(60):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.25)
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
            async with s.get(base + "/", allow_redirects=False) as resp:
                checks["/ redirects to /login"] = resp.status == 302 and resp.headers["Location"] == "/login"
            async with s.get(base + "/static/js/textual.js", allow_redirects=False) as resp:
                checks["static files gated too"] = resp.status == 302
            try:
                async with s.ws_connect(base.replace("http", "ws") + "/ws"):
                    checks["/ws refused without cookie"] = False
            except aiohttp.WSServerHandshakeError as e:
                checks["/ws refused without cookie"] = e.status == 401
            async with s.post(base + "/login", data={"password": "nope"}, allow_redirects=False) as resp:
                body = await resp.text()
                checks["wrong password rejected"] = resp.status == 401 and "wrong password" in body
                checks["no cookie on failure"] = "gek_session" not in resp.cookies
            async with s.post(base + "/login", data={"password": "correct horse"}, allow_redirects=False) as resp:
                c = resp.cookies.get("gek_session")
                checks["right password sets the cookie"] = resp.status == 302 and c is not None
                checks["cookie is HttpOnly + SameSite=Strict"] = bool(c) and c["httponly"] and \
                    c["samesite"].lower() == "strict"
            async with s.get(base + "/", allow_redirects=False) as resp:
                checks["/ served with cookie"] = resp.status == 200 and "textual" in (await resp.text()).lower()
            try:
                async with s.ws_connect(base.replace("http", "ws") + "/ws") as ws:
                    checks["/ws opens with cookie"] = not ws.closed
                    await ws.close()
            except aiohttp.WSServerHandshakeError:
                checks["/ws opens with cookie"] = False
            async with s.get(base + "/logout", allow_redirects=False) as resp:
                checks["logout clears it"] = resp.status == 302
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
