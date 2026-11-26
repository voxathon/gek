# gek control protocol

Every way of steering a swarm (the CLI, the TUI, the web TUI, a Bonnet bridge, a bot,
another agent) is a client of one thing: **commits on the swarm's `swarm/inbox` branch.**
There is no other API. A client never talks to the running process.

- A commit **without** a `Control:` trailer is a message for the orchestrator. It wakes the
  orchestrator, which reads it in its log like anything else.
- A commit **with** a `Control:` trailer is a command. The harness carries it out itself,
  without asking the orchestrator, and tells the orchestrator about it in its next wake
  briefing.

## Message format

```
<subject: one line, what you did and why>

<body: free text; for some verbs, a payload (see below)>

Control: <verb> [args]
From: <who sent it>
```

- The commit author is the sender. It is what the record shows (`agent_kill` by `moxxie`).
  Senders choose their own name; the swarm's machine is trusted, so anyone who can write
  to the repo is trusted. Remote access needs its own gate (see the web TUI login).
- One command per commit. Commits are processed oldest first.
- The reason a client gives goes in the subject or the first paragraph of the body; it is
  logged as `reason`.

## Verbs

| Control | Args | Payload in body | Effect |
| --- | --- | --- | --- |
| `kill AGENT` | agent id | reason | Kill a live agent, on the record. |
| `halt` | | reason | Stop the swarm; live agents are killed. |
| `wake` | | reason | Wake the orchestrator now (ignored in human mode, except it logs a notice). |
| `spawn ROLE [from=BRANCH]` | role name, optional branch to start from | note for the agent | Spawn one agent into an existing role, checked against the ceiling and `max_alive`. |
| `role set` | | the role template as JSON | Create or replace a role template, validated against the ceiling. |
| `role retire NAME` | role name | reason | Retire a template. Live agents of it keep running. |
| `config set KEY` | dotted key, e.g. `agents.max_alive` | the value as JSON (`3`, `true`, `"text"`, `{...}`) | Change a live setting (see hot keys). Persisted to the swarm's `config.json`. |
| `mcp set NAME` | server name | the server entry as JSON | Add or replace an `mcp_servers` entry, stdio or remote. Applied live, persisted, and the MCP generation live agents sync on is bumped. |
| `mcp rm NAME` | server name | reason | Delete an `mcp_servers` entry. Persisted. |
| `mcp enable NAME` / `mcp disable NAME` | server name | reason | Set `enabled` back to `true` / to `false`: the reversible version of `rm`. Persisted. |
| `mode llm` / `mode human` | | reason | Who orchestrates. `human`: the model is not called on wakes; wakes become notices for you. `llm`: the model orchestrates, and you can still act. Persisted. |
| `pause` | | reason | Freeze the swarm: the orchestrator doesn't wake, agents finish their current step and wait. Nothing is killed. |
| `unpause` | | reason | Unfreeze. (`resume` is accepted as an alias.) |

Unknown verbs and failed commands are logged (`control` event with `ok: false` and the
error) and posted to chatter, so the sender can see why.

## MCP servers over the control plane

`mcp` is how a running swarm gains, loses or parks an MCP server. A server is either local
(stdio: `command` + `args`) or remote (`url` + `transport` + `headers`); see
[MCP.md](MCP.md) for the whole schema. `mcp set` takes the entry itself as the payload, so
the same verb covers both kinds:

```
Control: mcp set linear
```

```json
{"url": "https://mcp.example.com/mcp",
 "transport": "streamable-http",
 "headers": {"Authorization": "Bearer ${ENV:MCP_TOKEN}"}}
```

The stdio form is the same idea with a `command`. Header values stay placeholders — a real
token in the payload would be a token in the swarm's git history.

What the sender sees back, for a remote entry: the endpoint **host and path** (with any
userinfo stripped) and the **transport**. Never a header value, never an env value. What
lands in the log for a remote server is the server name, the kind, the transport, the
endpoint **host** only, and the header/env **names** where they help — a missing variable is
reported by name so it can be fixed.

A remote server that can't be reached, or whose `${ENV:NAME}` variable is unset, is not a
failed command: connecting is per-agent and best-effort, so that server contributes **no
tools** while the rest of the swarm carries on. The connect is logged as an `mcp` event with
`ok: false` and the error text. The error can't echo a secret: configured header and env
values are scrubbed out of it first.

## Hot keys (live config)

Changeable while running: `goal`, `budget.*`, `ceiling.*`, `models.default`,
`models.orchestrator`, `models.aliases.*`, `models.pricing.*`, `mcp_servers.*` (stdio and
remote entries alike), `llm.temperature`, `llm.timeout_s`, `llm.num_retries`,
`llm.extra_params.*`, `agents.*`, `orchestrator.*` (including `orchestrator.mode`).

Need a restart: `swarm_dir`, `api.*`. Setting them is refused with that reason.

Changes to the ceiling may widen it. After any ceiling change, every role template is checked
again; roles now above the ceiling are reported to the orchestrator and in the log. Live agents
keep the powers they were spawned with until they end or are killed.

Editing `config.json` by hand while a swarm runs has the same effect: the harness notices the
file changed, applies the hot keys that differ, refuses the cold ones, and logs the diff with
author `file`.

## What the orchestrator is told

Its next wake briefing has a section "Outside actions since your last wake", one line per
command, with who sent it and why, and any config diff. Its instructions say these actions
are authoritative: adapt to them, don't undo them.

## When the swarm isn't running

The CLI applies `role set`, `role retire`, `config set`, `mode` and `mcp set` / `mcp rm`
directly to the swarm's files, so they take effect on the next run; the CLI says so
("applies on the next run"). `kill`, `halt`, `wake`, `spawn`, `pause` and `unpause` need a
running swarm and say so. Commands sitting in the inbox when a run starts are not replayed.

## Logging

Every command produces a `control` event: `verb`, `arg`, `reason`, `sha`, `ok`, and
`result` or `error`. Config changes also produce a `config` event with `key`, `old`, `new`;
an `mcp` command shows up as a `control` event with verb `mcp` and arg `set NAME`, plus a
`config` event for `mcp_servers.NAME`. Mode and pause changes produce `mode` and `pause`
events.
