# Ale

Ale, pronounced Ali or Ally like Alison, is a Discord-native agent harness on
top of the Claude Agent SDK. Curated per-turn context, filesystem-backed thread
memory, a narrow MCP tool surface, and three operating modes:

- **Actor** — day-to-day Discord chat. Lightweight tools, visible delegation.
- **Linguist** — research, source review, synthesis, citation-heavy work.
- **Engineer** — coding, deployment, debugging, Ale self-improvement.
  Engineer is **not** invoked by casual mention; it runs only via the
  `/engineer` slash command or the internal incident-handoff path.

Default models:

- Actor: `claude-haiku-4-5`
- Linguist: `claude-opus-4-6`
- Engineer: `claude-opus-4-6`

## Quickstart

```bash
git clone https://github.com/kcodes0/ale.git
cd Ale
uv sync
uv run ale onboard
uv run ale discord
```

`uv run ale onboard` is an interactive first-run setup — it asks for your
Discord bot token, your allowlist of user IDs, and (optionally) a GitHub
remote so Engineer can commit and push. Re-running it is safe: existing
values in `.env` are preserved unless you overwrite them.

The first time you start the gateway after onboarding, Discord registers the
`/engineer` slash command. `ALE_DISCORD_SYNC_COMMANDS=auto` records a local
sync marker under the state directory so normal restarts do not burn Discord's
global command-sync quota. Use `always` after changing command definitions, or
`never` when commands are managed elsewhere.

Local one-shot smoke test (no Discord):

```bash
uv run ale ask "Actor: summarize what Ale can do"
```

## Auth

Ale uses local Claude and Codex CLI auth by default — `ANTHROPIC_API_KEY` and
`CODEX_API_KEY` are optional. Set `ALE_ENABLE_LLM_ROUTER=false` if you want
thread routing and recap refresh to stay deterministic during testing.

For DM-only testing, leave Discord's privileged Message Content intent off.
Set `ALE_DISCORD_MESSAGE_CONTENT_INTENT=true` only after enabling that
privileged intent in the Discord Developer Portal.

## Long replies → PDF + Actor summary

When Linguist or Engineer return more than `ALE_DISCORD_ARTIFACT_THRESHOLD`
characters (default 2400):

1. The full report saves as `<thread>/<message_id>.md` and renders to
   `<thread>/<message_id>.pdf` under `~/.local/state/ale/artifacts/`.
2. A short Actor-voiced summary (one fast SDK call, no tools, capped at
   `ALE_ACTOR_SUMMARY_MAX_CHARS`, default 1400) becomes the Discord message.
3. The PDF attaches. If `ALE_ARTIFACT_PUBLISH_COMMAND` returns a URL, that
   link is included instead.

Knobs: `ALE_ENABLE_PDF_ARTIFACTS`, `ALE_ENABLE_ACTOR_SUMMARY`,
`ALE_ACTOR_SUMMARY_MAX_CHARS`, `ALE_ACTOR_SUMMARY_MAX_BUDGET_USD`.

## Hot Reload

Reload is **manual**. The Lead Engineer drives staged reloads from inside a
Workspace by calling the `reload_*` MCP tools. The file-system watchdog is
**off by default** — flip `ALE_HOT_RELOAD_ENABLED=true` only when you want
auto-reload during local development.

Three staged tiers:

1. **Config + prompts** (`reload_config_and_prompts`). Re-reads `.env` and
   re-imports `ale.personas` / `ale.prompts` so the BASE_SYSTEM and persona
   prompts visible to the next SDK call reflect disk. In-flight turns keep
   their snapshot.
2. **Tool module** (`reload_tools`). Re-imports `ale.tools` so MCP tool
   definitions on the next agent call are fresh. Waits for in-flight turns
   to drain — the bundled Claude CLI subprocess crashes if its tool registry
   changes mid-iteration.
3. **Candidate process + health check** (`candidate_health_check`). Spawns
   `ale health-check` as a subprocess to smoke-test the new code path end
   to end. `promote_candidate` re-execs the current process when
   `ALE_ENABLE_SELF_PROMOTE=true`. `rollback_reload` restores the previous
   snapshot if a regression slips through.

Triggers:

- Engineer MCP tools (primary): `reload_plan` (read-only inspection),
  `reload_config_and_prompts`, `reload_tools`, `candidate_health_check`,
  `promote_candidate`, `rollback_reload`.
- `SIGHUP` to the Discord process.
- File watchdog (off by default; turn on with `ALE_HOT_RELOAD_ENABLED=true`).

The watchdog respects an in-flight turn counter and emits
`watchdog_awaiting_idle` when it has to defer, so even with the watchdog on
it will not crash a running turn.

Relevant env vars: `ALE_HOT_RELOAD_ENABLED` (default `false`),
`ALE_HOT_RELOAD_TOOLS` (`true`), `ALE_HOT_RELOAD_CANDIDATE` (`false`),
`ALE_ENABLE_RELOAD_TOOLS` (`true`), `ALE_ENABLE_SELF_PROMOTE` (`false`),
`ALE_RELOAD_WATCH_PATHS` (`ale`), `ALE_RELOAD_DEBOUNCE_SECONDS` (`1.5`),
`ALE_RELOAD_POLL_INTERVAL_SECONDS` (`1.0`),
`ALE_RELOAD_HEALTH_CHECK_TIMEOUT_SECONDS` (`60`), `ALE_CANDIDATE_PYTHON`.

## Operations

Logs rotate in-process before writes once a file reaches `ALE_LOG_MAX_BYTES`
(default 10000000), keeping `ALE_LOG_BACKUP_COUNT` backups (default 5). For
long-lived hosts, an external supervisor-level log policy is still useful for
stdout/stderr and system logs.

## Failure Recovery

If a user-facing turn fails, Ale tells you it hit an internal error, creates
an Engineer incident Workspace, writes the failure context there, and asks
Engineer for a no-edit diagnosis in the background. Engineer is allowed to
self-modify only after a `candidate_health_check` succeeds and the user has
opted into `ALE_ENABLE_SELF_PROMOTE=true`.

## State

Defaults under `~/.local/state/ale/`:

- `threads/` — one directory per thread with `meta.json`, `messages.jsonl`,
  `recap.md`
- `workspaces/` — Linguist/Engineer team workspaces with `meta.json`,
  `team_chat.jsonl`, `files/`, `transcripts/`
- `artifacts/<thread>/` — full Markdown + PDF reports
- `logs/turns.jsonl`, `logs/all-events.jsonl`
- `logs/agent.log`, `logs/agent-team.log`, `logs/core-system.log`,
  `logs/discord.log`, `logs/reload.log`, `logs/router.log`,
  `logs/threads.log`, `logs/tools.log`, `logs/workspace.log`

`agent-team.log` carries cross-agent and team-coordination events: turn
lifecycle for Linguist/Engineer (`turn_id`, `parent_turn_id`, `agent_role`,
`team`, `tool_args_summary`, `duration_ms`, `cost_usd_estimate`), workspace
creation, team chat, codex worker dispatch/completion, incident handoffs,
and Actor summarizer turns. `reload.log` carries every staged-reload event
with `reload_id`, `tier`, `duration_ms`, `changed_files`, and outcome.

Memory files live at `~/.claude/projects/-home-claude/memory` by default.

## Safety Defaults

Actor uses lightweight tools only. Engineer can use filesystem and shell
tools through the Claude Agent SDK. Ale's custom `bash` MCP tool and
`codex_exec` implementation-worker tool are disabled unless
`ALE_ENABLE_BASH_TOOL=true` and `ALE_ENABLE_CODEX_EXEC=true`.

`codex_exec` modes:

- `read_only` — analysis only with `--sandbox read-only`
- `workspace_write` — normal patches with `--sandbox workspace-write`
- `full_auto` — autonomous edits with `--full-auto`, intended only for
  isolated environments

## Source Control

If a git remote was configured during `ale onboard`, Engineer commits and
pushes after a successful patch (after `uv run pytest -q` and
`uv run ruff check .` are both green). Engineer never force-pushes, never
amends published commits, and never invents remote URLs — if no remote
exists, it commits locally and tells you.

## Manual Setup

Skip the onboarding REPL? Set these directly:

```bash
export DISCORD_TOKEN_FILE="$HOME/.config/ale/discord-token"
export ALE_ALLOWED_USER_IDS="1234567890"
```

Or edit `.env` in the repo root. Required keys: `DISCORD_TOKEN` (or
`DISCORD_TOKEN_FILE`) and `ALE_ALLOWED_USER_IDS` (or an `access.json` under
`~/.local/state/ale/`).
