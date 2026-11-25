# gek

An asynchronous swarm harness. Not a call tree.

An LLM **orchestrator** wakes up, reads `git log`, writes **role templates**,
grants them powers, spawns **agents** into them, kills the ones that go feral,
and goes back to sleep. Agents are cattle: each gets its own git worktree and
branch, works out its own goal from its role's mandate, commits, posts to a
shared chatter branch, and ends by finishing or by being killed by a role that
holds `kill` over it. Nothing blocks on anything. State lives in git, not in
agents, and not even in the orchestrator, which has no memory between wakes
beyond what's in the log.

## If you just came back after months

```
gek list                    # which swarms exist, which is current, how each last run ended
gek explain                 # what is this, what happened last run, what's in the repo
gek explain --run 2026      # a specific run (id prefix)
```

Then `gek where` shows the logs folder: its `README.txt` explains the format, and the newest
`<run>/gek.log` has the blow-by-blow.

## Install

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/), and **git 2.38 or newer**
(merges use `git merge-tree --write-tree`; Debian 12 ships 2.39).

```
uv tool install gek
gek init todo --goal "what the swarm should do"   # creates ~/.gek/ on first use, makes 'todo' current
# put your key in ~/.gek/.env, pick models in ~/.gek/config.json
gek run
```

Agents with the `shell` power run commands as your user. **On Linux with bubblewrap**, each
command is sandboxed (see Sandbox): the filesystem is read-only except the agent's worktree,
/tmp and a cache, and the swarm's config, `.env`, logs and your `~/.ssh`-style credentials are
hidden. **Without bubblewrap** (Windows, macOS, a minimal distro, most containers) commands run
unsandboxed and every `gek run` prints one warning; run shell swarms in a throwaway VM there.

**Host-safe mode:** set `"shell": false` in the ceiling. Agents can then only read the log,
read and write files inside their own worktree (paths outside it are refused), commit within
their `writes` scope, and use the MCP tools you configured. That is safe enough to run on your
own machine. Agents never see your API key either way: their shell commands and the MCP servers
they start get an allowlisted environment without it (`agents.env_passthrough` adds names).

## Where things live

Everything goes under one folder, `~/.gek` (or `$GEK_HOME`):

```
~/.gek/
  config.json            shared settings for every swarm (models, api, ceiling, limits)
  .env                   shared secrets (GEK_API_BASE, GEK_API_KEY)
  current                name of the current swarm
  swarms/
    todo/
      config.json        this swarm's overrides: goal at least, any other key too
      repo.git/          its git repo (the shared medium)
      wt/                live agents' worktrees
      logs/              one folder per run
      run.lock           present while `gek run` is going
    tui/ ...
```

Settings layer: built-in defaults, then `~/.gek/config.json`, then the swarm's
own `config.json`.

**Portable swarms** keep the old self-contained layout instead:
`gek init --portable [--dir D]` writes `config.json`, `.env` and `swarm/` into
`D`. They don't read the shared `~/.gek` settings. Any command run in a folder
whose `config.json` is a gek config acts on that portable swarm; `-c path` points
at one from anywhere.

## Console

`gek run` prints a curated feed: wakes and sleeps, roles written or rejected,
spawns, commits, chatter posts, merges, kills, exits, inbox messages, errors.
Everything else goes only to the log files. `-v` echoes every event to stderr;
`-q` prints nothing.

```
15:25:05 [orchestrator] wakes (#1): start
15:25:05   + role coder (create) powers=[shell]: write code under src/
15:25:05   -> coder/5ddd spawned: write src/hello.py
15:25:07   *  coder/5ddd committed on role/coder/5ddd: add hello
15:25:07   ~  coder/5ddd says: hello.py ready on role/coder/5ddd
15:25:08   >> reviewer/1fba merged role/coder/5ddd into main: fast-forward to 6ce14b03d6
15:25:09   XX reviewer/1fba killed coder/462c: dawdling
```

## Commands

| Command | What it does |
| --- | --- |
| `gek init NAME [--goal G] [--seed REPO]` | new swarm under `~/.gek/swarms/NAME`; becomes current |
| `gek init --portable [--dir D] [--goal G] [--seed REPO]` | self-contained swarm in `D` |
| `gek list` (`ls`) | every swarm: current one starred, running or last run, goal |
| `gek use NAME` | switch the current swarm |
| `gek where` | paths of the current swarm's config, repo, worktrees, logs |
| `gek run [--goal G] [-v \| -q] [--detach]` | run until the orchestrator halts, the token budget runs out, or Ctrl-C |
| `gek resume [--respawn] [--fresh-budget] [--detach]` | pick up after the last run ended (see Resuming) |
| `gek inbox "message" [--as NAME]` | post to the orchestrator's inbox (works while a run is going; wakes it) |
| `gek explain [--run ID]` | human summary of a run and the repo |
| `gek tui [--web]` | live dashboard of every swarm: graphs, agents, feed; `--web` serves it to a browser |
| `gek push REMOTE [BRANCH] [--to P] [--everything]` | push swarm work to a git URL or path, as `gek/<swarm>/<branch>` |
| `gek kill AGENT [REASON]` | kill a live agent of the running swarm, on the record |
| `gek halt [REASON]` | stop the running swarm; live agents are killed |

`--seed` takes a path or a git URL (`https://...`, `git@host:path`, `file://...`), so a VM can
start straight from GitHub. `gek push` sends results back out without touching the remote's
own branches: `gek push /mnt/shared/gek` lands the swarm's `main` as `gek/<swarm>/main`.

Pick a swarm other than the current one with `-s NAME` (or `-c path/to/config.json`)
before the command: `gek -s tui explain`. `--seed` clones an existing repo; its
default branch becomes `main`.

## Resuming

```
gek resume                 # pick up after a halt, Ctrl-C, budget stop or crash
gek resume --respawn       # ...and respawn the agents that were mid-work, onto their own branches
gek resume --fresh-budget  # don't count tokens the earlier runs spent
gek resume --detach        # in the background (the TUI's palette has "Resume swarm" too)
```

A resume cleans up and briefs the orchestrator before its first wake:

- **A stale `run.lock`** (the process is gone; checked on Linux, macOS and Windows) is cleared.
- **Uncommitted work** left in agent worktrees by a crash is **salvaged**: committed onto that
  agent's branch (`gek: salvaged uncommitted work from coder-ab12`), then the worktree is removed.
- **Agents that were mid-work** (crashed, or killed when the swarm stopped) are listed with their
  branch, steps and last state. The orchestrator decides what to respawn (`resume_branch`), or
  `--respawn` does it for you with a "you're continuing X's work" note.
- **Tokens spent** by the earlier runs carry into `budget.max_total_tokens`, so a job resumed three
  times still respects one cap.
- The orchestrator's first briefing says how the last run ended and repeats its last note to itself.

## Control plane

Everything that steers a swarm is a commit on its `swarm/inbox` branch with a `Control:` trailer,
written by whoever sends it (see [docs/CONTROL.md](docs/CONTROL.md) for the full protocol). The CLI,
the TUI and any other client use the same channel, so every action is on the record with a name.

| Command | What it does |
| --- | --- |
| `gek spawn ROLE [NOTE] [--from BRANCH]` | spawn an agent into a role |
| `gek role list / show NAME / edit NAME / set FILE\|- / retire NAME` | manage role templates (`edit` opens `$EDITOR`; validated against the ceiling) |
| `gek config get [KEY]` / `gek config set KEY VALUE` | read or change settings live; VALUE is JSON. Ceiling changes ask first (`--yes` to skip). Restart-only keys are refused. |
| `gek mode human\|llm` | who orchestrates. `human`: the model isn't called; wakes become notices in the feed and TUI. |
| `gek wake [REASON]` | wake the orchestrator now |
| `gek pause` / `gek unpause` | freeze and unfreeze the swarm; agents finish their current step and wait |
| `gek kill AGENT [REASON]` / `gek halt [REASON]` | kill one agent / stop the swarm |
| `gek inbox "message"` | a plain message to the orchestrator |

Live settings take effect without a restart, are saved to the swarm's `config.json`, and show up
in the orchestrator's next briefing. Editing `config.json` by hand while the swarm runs works too.
When no swarm is running, `role`, `config` and `mode` change the files directly for the next run;
the rest need a running swarm. The orchestrator is told every outside action and treats them as
authoritative.

## TUI

```
gek tui
```

Every swarm under GEK_HOME in a sidebar (current one starred, running ones
marked). For the selected swarm's latest run: a status line (running or how it
ended, minutes, wakes, live/total agents, tokens with cache share, commits,
merges), three graphs (tokens over time with the cached share, live agents,
commits per minute), a table of agents (calls, prompt tokens, cache share,
output tokens, last tool), and the live feed. It tails `events.jsonl`, so it
works on a running swarm in another terminal and on old runs alike.

Orchestrator wakes show as orange lines on the graphs and in a strip above
them with each wake's reason ("timer", "inbox from moxxie", "coder/ab12 exited").

Select an agent (or the orchestrator) in the table and press Enter for its full
transcript: harness messages, its replies, every tool call with arguments,
every result (errors in red), and wake separators for the orchestrator. It
keeps updating while the agent lives. `f` toggles full/short, `s` shows the
system prompt, `g`/`G` top/bottom, `esc` back.

**The TUI is the whole control plane.** You can create a swarm, start it, steer it, stop it
and push its work without leaving it (or a browser tab):

| Key | Does |
| --- | --- |
| `n` | new swarm: name, goal, seed (path or git URL), optionally start it right away |
| `R` | start the selected swarm in the background (detached: closing the TUI or tab doesn't stop it) |
| `s` | spawn an agent into a role, with a note |
| `k` | kill the selected agent (asks for a reason) |
| `i` | message the orchestrator (wakes it) |
| `p` | pause / unpause |
| `m` | switch llm / human orchestration |
| `w` | wake the orchestrator now |
| `o` | roles: edit templates as JSON with live validation against the ceiling; `ctrl+s` save, `ctrl+r` retire, `ctrl+n` new |
| `c` | config: every setting; Enter on a live one changes it (applied now if running, else next run) |
| `P` | push the swarm's work to a git remote |
| `H` | halt (asks for a reason) |
| `ctrl+p` | command palette: every action above, plus "Swarm: NAME" to switch swarms |
| `[` / `]`, `r`, `q` | older / newer run, rescan swarms, quit |

All of it goes through the control protocol ([docs/CONTROL.md](docs/CONTROL.md)), exactly like
the CLI, so it's all on the record as `human`.

**On a phone or any narrow window** the layout stacks: no sidebar (switch swarms with `ctrl+p`),
one graph, the agents table above the feed.

### In a browser

```
gek tui --web                                               # http://localhost:8000
gek tui --web --host 0.0.0.0 --public-url http://10.0.0.15:8000   # from other machines
```

The TUI runs on the swarm's machine; the browser only gets the app (textual-serve), not a shell.
Serving beyond localhost needs a password: put `GEK_WEB_PASSWORD=...` in `~/.gek/.env`. The
browser gets a login page; the password becomes an HttpOnly, SameSite=Strict session cookie,
checked on every route including the websocket. Restarting the server logs everyone out.
Open it at the same address as `--public-url` so the cookie matches.

This is plain HTTP: on your own LAN that's a reasonable minimum, but the cookie travels
unencrypted. Past your LAN, use Tailscale (or an HTTPS reverse proxy) rather than opening a port.
And remember what's behind the login: agents with `shell` can do anything the swarm's user can.

## The repo is the medium

Everything lives in `swarm/repo.git` (bare). Look at it with any git tool.

| Ref | What lives there | Who writes |
| --- | --- | --- |
| `main` | the work | roles with the `merge` power only |
| `role/<role>/<hex>` | one branch per agent | that agent (`commit` tool) |
| `swarm/chatter` | status, handoffs, spawns, kills, exits, sleeps (empty commits) | everyone |
| `swarm/roles` | `roles/<name>.json` templates | orchestrator |
| `swarm/inbox` | messages for the orchestrator | roles with `interrupt`, humans via `gek inbox` |

Commits carry trailers (`Role:`, `Instance:`, `Kills:`, `Reason:`, `Exit:`,
`Spawned:`, `Sleep:`), so `git log swarm/chatter --grep "Kills:"` and friends work.

Agent worktrees are under `swarm/wt/` while the agent lives and are removed
when it ends. Its branch stays. Uncommitted work dies with the agent, on purpose.
The orchestrator can spawn a fresh agent onto a dead one's branch with `resume_branch`.

## Powers

The orchestrator can grant anything up to the **ceiling** in config.json, and
nothing above it. A template above the ceiling is rejected with the reason,
and the rejection is logged.

| Power | Meaning |
| --- | --- |
| `writes: [prefixes]` | paths the role may commit under (`""` or `"*"` = anywhere). Enforced at commit time. |
| `shell` | `run` tool: shell commands in its worktree |
| `merge` | `merge` and `diff` tools: merge branches into main (conflicts are reported, main untouched) |
| `interrupt` | `interrupt` tool: post to the inbox, which wakes the orchestrator |
| `kill: [roles]` | `kill` and `list_agents` tools for agents of those roles (`"*"` = any) |
| `mcp: {server: [tool globs]}` | connect to that MCP server and see only matching tools, as `mcp__<server>__<tool>` |

**Limits are visible.** The orchestrator's wake briefing lists every cap (agents' steps,
shell timeout, output size, its own steps per wake), what each model alias maps to, total
spend against the budget, its own spend, and per-agent tokens. Agents are told their step and
token caps up front and see a status line as they go, so they can commit and hand off before
being stopped. A role's `max_tokens` stops its agents at that many tokens (`token_cap` exit).

Every agent also gets `read_log`, `list_files`, `read_file`, `post`, `finish`,
and `write_file`/`commit` if it has any `writes`.

Honest caveat: `shell` is a big hole without the sandbox. A role with an unsandboxed shell can
do anything your user account can, and the writes check only stops it committing outside its
scope. With the sandbox it can still read whatever your user can read that isn't hidden, and use
the network (`agents.sandbox_network`).

## Sandbox

`agents.sandbox` decides how `run` commands are wrapped:

| Value | Behavior |
|---|---|
| `"auto"` (default) | bwrap when it works; otherwise one warning per run and commands run as before |
| `"bwrap"` | `gek run` refuses to start unless bwrap works |
| `"off"` | never sandbox, no warning |

`gek run` tests bwrap once at start (it can be installed but broken, e.g. inside Docker or with
unprivileged user namespaces disabled). Install it with `apt install bubblewrap`,
`dnf install bubblewrap`, `pacman -S bubblewrap` or `apk add bubblewrap`; desktop distros with
Flatpak usually have it already.

Inside the sandbox:

- `/` is read-only; the agent's worktree and the swarm's git objects (so `git commit` works) are writable.
- `/tmp` is a fresh tmpfs per command.
- `$XDG_CACHE_HOME`, `UV_CACHE_DIR`, `PIP_CACHE_DIR` and `npm_config_cache` point into
  `<swarm>/sandbox-cache`, shared by the swarm's agents and kept between commands and runs.
- Hidden: `GEK_HOME`, the swarm folder (config.json, `.env`, logs, other agents' worktrees) and
  `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.azure`, `~/.kube`, `~/.docker`, `~/.config/gcloud`,
  `~/.config/gh`, `~/.netrc`, `~/.git-credentials`, `~/.pypirc`, `~/.npmrc`, `~/.password-store`,
  keyrings. `agents.sandbox_hide` adds paths.
- `agents.sandbox_writable` adds writable paths; `agents.sandbox_network: false` cuts the network.
- New PID, IPC and UTS namespaces; the command dies with the harness.

Agents and the orchestrator are told in their prompts whether shells are sandboxed.

MCP servers are configured in `mcp_servers` (stdio only for now):

```json
"mcp_servers": { "fetch": { "command": "uvx", "args": ["mcp-server-fetch"], "env": {} } },
"ceiling": { "mcp": { "fetch": ["*"] } }
```

## Compaction

Every step resends an agent's whole history, so a long agent's cost grows with the square of
its length. Once an agent's prompt passes `agents.compact_at_tokens` (default 50,000), gek asks it
to write notes on its own history (task, commits, current state, next steps, gotchas) and swaps
the older messages for them. Kept: the system prompt, the spawn message and the last
`agents.compact_keep_recent` messages (default 8, never splitting a tool call from its result).

- It's on by default because short agents never reach the threshold and pay nothing. A
  compaction costs one extra call and one prompt-cache miss.
- A role's `compact_at` overrides the threshold; `-1` turns it off for that role. `0` in config
  turns it off for the swarm.
- If the summary call fails, gek falls back to trimming old tool outputs and tries again once the
  prompt has grown by half the threshold.
- Each compaction is a `compact` event with the notes, shows in the feed, and is in the agent's
  transcript. Agents are told up front, so they commit and post as they go.

## Wake and sleep

The orchestrator wakes on: start, its own timer (it chooses the sleep length,
clamped to `min_sleep_s`..`max_sleep_s`), anything posted to `swarm/inbox`
(polled every `inbox_poll_s`, so `gek inbox` from another terminal works), and
an agent exiting (`wake_on_agent_exit`). Interrupts are debounced
(`debounce_s`) and never wake it twice within `min_sleep_s`.

Each wake starts fresh: the prior, the goal, the ceiling, current roles and
agents, and the git log since the last wake. It ends the wake with `sleep` or
`halt`. Ctrl-C kills live agents (on the record) and stops cleanly.

## Config

`config.json` (everything not set falls back to defaults in `src/gek/config.py`):

| Key | Meaning |
| --- | --- |
| `swarm_dir` | where repo.git, wt/ and logs/ live, relative to config.json (`.` for home swarms, `swarm` for portable) |
| `goal` | what the swarm is for; shown to every actor |
| `api.api_base_env`, `api.api_key_env` | names of the env vars holding base URL and key |
| `models.orchestrator`, `models.default` | litellm model names |
| `models.aliases` | names roles can use in their `model` field, e.g. `"cheap": "openai/some-small-model"` |
| `llm.timeout_s`, `llm.num_retries`, `llm.temperature`, `llm.extra_params` | passed to litellm |
| `orchestrator.*` | sleep bounds, debounce, inbox poll, steps per wake, log size per wake |
| `agents.max_alive`, `agents.max_steps` | swarm size cap, steps per agent (a role can set its own `max_steps`, and a `max_tokens` cap per agent) |
| `agents.max_tool_output_chars`, `agents.shell_timeout_s` | tool output cap, shell timeout |
| `agents.compact_at_tokens`, `agents.compact_keep_recent` | see Compaction |
| `agents.sandbox`, `agents.sandbox_network`, `agents.sandbox_hide`, `agents.sandbox_writable` | see Sandbox |
| `agents.env_notes` | lines every agent and the orchestrator see, e.g. `["uv is installed: use uv run", "pytest is installed"]` |
| `agents.status_every_steps` | how often agents get a `[gek: step N/M, T tokens used]` line (every step once near a cap) |
| `models.pricing` | USD per million tokens by model or alias, e.g. `{"vllm-sr/auto": {"input": 0.27, "cached_input": 0.07, "output": 1.1}}`; overrides litellm's price, which is $0 for custom providers |
| `agents.env_passthrough` | env var names (default `[]`) passed through to an agent's `run` subprocesses and to the MCP servers it starts; everything not on the built-in allowlist is dropped, and `api.api_base_env`/`api.api_key_env` are never passed even if listed. An MCP server that needs its own key gets it through its `env` in `mcp_servers`. |
| `budget.max_total_tokens` | stop the swarm once this many tokens are spent (0 = no cap) |
| `ceiling` | the most power the orchestrator may grant |
| `mcp_servers` | MCP servers available to grant |

`.env` holds secrets: `~/.gek/.env` for home swarms, next to config.json for
portable ones (a swarm folder's own `.env` and one in the current directory also load):

```
GEK_API_BASE=https://app.vllm-sr.ai/v1
GEK_API_KEY=...
```

For an OpenAI-compatible endpoint like that one, model names are
`openai/<model-name-the-server-expects>`. The default `openai/auto` is a guess:
set the real names in config.json.

## Logs

Overly extensive, by request. Per run, in the swarm's `logs/<run>/` (`gek where` shows it):

- `events.jsonl`: every event (model calls with full responses and usage, every
  tool call and result, every git command, spawns, kills, exits, merges, role
  changes, inbox messages, budget notes). One JSON object per line with
  `ts`, `t`, `run`, `seq`, `kind`, `actor`, `data`.
- `gek.log`: the same, readable.
- `agents/<id>.jsonl`: each actor's whole conversation, every message once.
- `meta.json`: run id, versions, config snapshot.
- `mcp-<server>-<agent>.stderr.log`: whatever MCP servers print to stderr.

`logs/README.txt` lists every event kind. The future TUI reads these.

## Layout

```
src/gek/
  cli.py          commands
  config.py       config.json + .env, defaults
  logs.py         RunLog: events.jsonl, gek.log, transcripts
  gitrepo.py      git plumbing: swarm refs, worktrees, merges, log
  roles.py        role templates, powers, ceiling validation
  llm.py          litellm calls, token accounting, budget
  tools.py        tool registry with logged execution
  mcp_bridge.py   MCP servers as grantable, filtered tools
  agent.py        one agent: worktree, tools, loop, teardown
  swarm.py        roles, spawning, killing, the orchestrator wake loop
  prompts.py      the orchestrator prior and the agent system prompt
  explain.py      `gek explain`
  tui.py          `gek tui` (Textual + plotext)
  tui_control.py  the TUI's control screens: new swarm, spawn, push, roles, config
  web.py          `gek tui --web` with the password gate
  resume.py       `gek resume`: what a dead run left behind, salvage, cross-platform pid checks
  sandbox.py      bwrap around agents' `run` commands; the warning when there's none
tests/smoke.py    end-to-end run with a scripted fake model (no API key needed)
tests/limits.py   caps, status lines, briefing contents, custom pricing
tests/control.py  gek kill / halt / inbox against a running swarm, gek push
tests/controlplane.py  the whole control plane through the real CLI (docs/CONTROL.md)
tests/tui_control.py   the TUI control plane, headless: forms, editors, detached runs, phone layout
tests/web_login.py     the web TUI's password gate
tests/resume.py        gek resume after a hard crash: salvage, briefing, respawn, budget
tests/sandbox.py       what a sandboxed shell can and can't touch; fallbacks off Linux
tests/compact.py       history compaction: notes, kept tail, opt-out, failed summaries
docs/CONTROL.md   the control protocol: every way of steering a swarm
```

## Publishing

```
uv build                      # dist/gek-X.Y.Z.tar.gz and .whl; config.json, .env, swarm/ are excluded
uv publish --token pypi-...   # or set UV_PUBLISH_TOKEN
```

Bump `version` in pyproject.toml and `__version__` in `src/gek/__init__.py` together.

## Tests

```
uv run python tests/smoke.py
uv run python tests/limits.py
uv run python tests/control.py
uv run python tests/controlplane.py
uv run python tests/tui_control.py
uv run python tests/web_login.py
uv run python tests/resume.py
uv run python tests/sandbox.py   # bwrap checks are skipped where bwrap doesn't work
uv run python tests/compact.py
```

Plays a whole scripted swarm in a temp dir: roles written (one rejected),
a coder commits and gets refused for writing outside its scope, a reviewer
merges its branch and kills a dawdling second coder that was using an MCP
tool, an outside `gek inbox` message arrives, the orchestrator halts. Prints
`gek explain` and a list of PASS/FAIL checks.

## License

MIT. See [LICENSE](LICENSE).
