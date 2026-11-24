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

Agents with the `shell` power can do anything your user can. Run swarms that
have it inside a throwaway VM, not on a machine you care about.

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
| `gek run [--goal G] [-v \| -q]` | run until the orchestrator halts, the token budget runs out, or Ctrl-C |
| `gek inbox "message" [--as NAME]` | post to the orchestrator's inbox (works while a run is going; wakes it) |
| `gek explain [--run ID]` | human summary of a run and the repo |
| `gek tui` | live dashboard of every swarm: graphs, agents, feed |

Pick a swarm other than the current one with `-s NAME` (or `-c path/to/config.json`)
before the command: `gek -s tui explain`. `--seed` clones an existing repo; its
default branch becomes `main`.

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

Keys: up/down pick a swarm, Tab to the agents table, Enter for a transcript,
`[` / `]` older / newer run, `r` rescan swarms, `q` quit.

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

Every agent also gets `read_log`, `list_files`, `read_file`, `post`, `finish`,
and `write_file`/`commit` if it has any `writes`.

Honest caveat: `shell` is a big hole. A role with a shell can do anything your
user account can. The writes check only stops it committing outside its scope.

MCP servers are configured in `mcp_servers` (stdio only for now):

```json
"mcp_servers": { "fetch": { "command": "uvx", "args": ["mcp-server-fetch"], "env": {} } },
"ceiling": { "mcp": { "fetch": ["*"] } }
```

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
| `agents.max_alive`, `agents.max_steps` | swarm size cap, steps per agent (a role can set its own `max_steps`) |
| `agents.max_tool_output_chars`, `agents.shell_timeout_s` | tool output cap, shell timeout |
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
tests/smoke.py    end-to-end run with a scripted fake model (no API key needed)
```

## Publishing

```
uv build                      # dist/gek-X.Y.Z.tar.gz and .whl; config.json, .env, swarm/ are excluded
uv publish --token pypi-...   # or set UV_PUBLISH_TOKEN
```

Bump `version` in pyproject.toml and `__version__` in `src/gek/__init__.py` together.

## Smoke test

```
uv run python tests/smoke.py
```

Plays a whole scripted swarm in a temp dir: roles written (one rejected),
a coder commits and gets refused for writing outside its scope, a reviewer
merges its branch and kills a dawdling second coder that was using an MCP
tool, an outside `gek inbox` message arrives, the orchestrator halts. Prints
`gek explain` and a list of PASS/FAIL checks.

## License

MIT. See [LICENSE](LICENSE).
