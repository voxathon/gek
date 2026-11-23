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
uv run gek explain          # what is this, what happened last run, what's in the repo
uv run gek explain --run 2026   # a specific run (id prefix)
```

Then read `swarm/logs/README.txt` for the log format, and the newest
`swarm/logs/<run>/gek.log` for the blow-by-blow.

## Install

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/), and **git 2.38 or newer**
(merges use `git merge-tree --write-tree`; Debian 12 ships 2.39).

```
uv tool install gek          # or, from a checkout: uv sync, then prefix commands with `uv run`
mkdir myswarm && cd myswarm
gek init --goal "what the swarm should do"     # writes config.json, .env, swarm/repo.git
# put your key in .env, pick models in config.json
gek run
```

Agents with the `shell` power can do anything your user can. Run swarms that
have it inside a throwaway VM, not on a machine you care about.

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

To start from an existing project instead of an empty repo:
`uv run gek init --seed path/to/some/repo`. Its default branch becomes `main`.

## Commands

| Command | What it does |
| --- | --- |
| `gek init [--goal G] [--seed REPO] [--dir D]` | create config.json, .env, and the swarm repo |
| `gek run [--goal G] [-v \| -q]` | run until the orchestrator halts, the token budget runs out, or Ctrl-C |
| `gek inbox "message" [--as NAME]` | post to the orchestrator's inbox (works while a run is going; wakes it) |
| `gek explain [--run ID]` | human summary of a run and the repo |

`-c path/to/config.json` goes before the command: `gek -c other.json run`.

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
| `swarm_dir` | where repo.git, wt/ and logs/ live (relative to config.json) |
| `goal` | what the swarm is for; shown to every actor |
| `api.api_base_env`, `api.api_key_env` | names of the env vars holding base URL and key |
| `models.orchestrator`, `models.default` | litellm model names |
| `models.aliases` | names roles can use in their `model` field, e.g. `"cheap": "openai/some-small-model"` |
| `llm.timeout_s`, `llm.num_retries`, `llm.temperature`, `llm.extra_params` | passed to litellm |
| `orchestrator.*` | sleep bounds, debounce, inbox poll, steps per wake, log size per wake |
| `agents.max_alive`, `agents.max_steps` | swarm size cap, steps per agent (a role can set its own `max_steps`) |
| `agents.max_tool_output_chars`, `agents.shell_timeout_s` | tool output cap, shell timeout |
| `budget.max_total_tokens` | stop the swarm once this many tokens are spent (0 = no cap) |
| `ceiling` | the most power the orchestrator may grant |
| `mcp_servers` | MCP servers available to grant |

`.env` holds secrets, loaded from next to config.json and from the current directory:

```
GEK_API_BASE=https://app.vllm-sr.ai/v1
GEK_API_KEY=...
```

For an OpenAI-compatible endpoint like that one, model names are
`openai/<model-name-the-server-expects>`. The default `openai/auto` is a guess:
set the real names in config.json.

## Logs

Overly extensive, by request. Per run, in `swarm/logs/<run>/`:

- `events.jsonl`: every event (model calls with full responses and usage, every
  tool call and result, every git command, spawns, kills, exits, merges, role
  changes, inbox messages, budget notes). One JSON object per line with
  `ts`, `t`, `run`, `seq`, `kind`, `actor`, `data`.
- `gek.log`: the same, readable.
- `agents/<id>.jsonl`: each actor's whole conversation, every message once.
- `meta.json`: run id, versions, config snapshot.
- `mcp-<server>-<agent>.stderr.log`: whatever MCP servers print to stderr.

`swarm/logs/README.txt` lists every event kind. The future TUI reads these.

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
