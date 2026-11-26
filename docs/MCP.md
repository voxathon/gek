# MCP servers

`mcp_servers` in `config.json` is a map of server name → entry. A server is either
**local** (stdio: gek spawns a `command`) or **remote** (a `url` spoken over
`streamable-http` or `sse`). Roles get at these tools through `powers.mcp`, and the tools
show up as `mcp__<server>__<tool>` — filtered by the globs the role was granted.

## The two shapes

```json
"mcp_servers": {
  "shell": {
    "command": "uvx",
    "args": ["mcp-server-fetch"],
    "env": {"FETCH_USER_AGENT": "gek"},
    "timeout_s": 60
  },
  "linear": {
    "url": "https://mcp.linear.app/mcp",
    "transport": "streamable-http",
    "headers": {"Authorization": "Bearer ${ENV:MCP_TOKEN}"},
    "timeout_s": 60
  }
}
```

| key | applies to | meaning |
| --- | --- | --- |
| `command` | stdio | the program to spawn. **Exactly one of `command` \| `url` per entry.** |
| `args`, `env` | stdio | argv tail; extra env vars for that server |
| `url` | remote | the MCP endpoint |
| `transport` | remote | `"streamable-http"` (the default) or `"sse"` |
| `headers` | remote | header name → value; values are `${ENV:NAME}` placeholders |
| `timeout_s` | both | connect timeout, default 60 |
| `enabled` | both | `false` takes the server out of play without deleting the entry |

An entry with **both** `command` and `url`, or with neither, is a `ValueError` naming the
server — it is a broken entry, not a half-remote one. Keys that start with `_` are ignored,
so `"_comment": "..."` is a safe way to annotate an entry.

## Secrets: `${ENV:NAME}`, never a literal

Remote header values are placeholders. `${ENV:MCP_TOKEN}` expands from the environment at
**connect time**, from the same agent-safe env allowlist the swarm runs with plus the
server's own variables — so `MCP_TOKEN=...` goes in `.env` (or the real environment) and
never in `config.json`, which is committed to the swarm repo.

Consequences, all of them deliberate:

- the placeholder is resolved at connect time, so rotating a token only needs a new run;
- a header value never reaches a log line, an event, or an error string — logs carry the
  header **names** (`auth_names`), and any configured value is scrubbed out of exception
  text before it is written;
- a missing variable is a `ValueError` naming the server **and** the variable, caught by
  the same handler as any other connect failure (see below).

## What you see on screen

Only the endpoint host and the transport, for a remote server:

- `host` is the host[:port] only — no path, no query string, no userinfo;
- the transport is `streamable-http` or `sse`;
- logs and events name the server, the kind (`remote`), the transport and the host, plus
  header/env names where they help. Never a header value, never an env value.

## Control plane

`mcp` is a control verb, so it works both live and offline. Every change is a
`Control: mcp <verb> <name>` commit from you on `swarm/inbox`, so it lands in the swarm's
history and in the orchestrator's next briefing ("Outside actions since your last wake").

Set a remote server from a JSON payload (stdin or an argument):

```sh
gek mcp set linear '{"url": "https://mcp.linear.app/mcp",
                     "transport": "streamable-http",
                     "headers": {"Authorization": "Bearer ${ENV:MCP_TOKEN}"}}'
gek mcp disable linear
gek mcp enable linear
gek mcp rm linear
```

- Live (swarm running): the change is applied to the running config, persisted to
  `config.json`, and bumps the MCP generation that live agents sync on — the next
  generation of an agent connects with the new server set.
- Offline (`gek -c config.json mcp set linear '...'` with nothing running): it is saved and
  says it "applies on the next run".
- The confirmation shows the endpoint host+path and the transport, with any userinfo
  stripped, and never a header or env value.

`enabled: false` is the reversible version of `rm`: the entry stays in `config.json`,
`mcp enable` brings it back.

## When a remote server is broken

A remote server that cannot be reached is **not** a swarm failure. `connect()` logs one
`mcp` event with `ok: false` and the error text, gives that server **no tools**, closes its
transport, and moves on — the agent keeps running with the other servers' tools. The same
is true when a `${ENV:NAME}` variable is missing.

So a bad remote entry costs you that server's tools, not the run. The failure is logged as
a message (not a traceback), and the error text never contains a secret.

## Troubleshooting

- **`url` unreachable / connection refused** — the event says `ok: false` with the error and
  the `host`; the server contributed no tools. Check the URL and that the endpoint is up;
  lower `timeout_s` while you are debugging so a dead endpoint fails fast.
- **Missing `${ENV:NAME}`** — the error names the server and the variable. Put
  `NAME=...` in `.env` (or export it) and start again; nothing else about the entry is wrong.
- **Unknown `transport`** — config error: only `"streamable-http"` and `"sse"` are accepted;
  the server is rejected with a `ValueError` naming it, and its tools are simply absent.
- **`command` and `url` in the same entry** (or neither) — `ValueError` naming the server.
  Pick one kind per entry.

## Not shipped

The TUI still edits stdio servers only. Remote entries are edited with the CLI/offline
`mcp set` payload above (or by hand in `config.json`); live agents pick them up on the next
generation.
