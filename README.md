# Ale

Ale, pronounced Ali or Ally like Alison, is a Discord-native agent harness built on the
Claude Agent SDK. It implements the proposal in `discord-agent-harness-proposal-v1.2.pdf`:
curated per-turn context, filesystem-backed thread memory, a narrow tool surface, and three
main operating modes.

- **Actor**: day-to-day Discord presence and normal chat.
- **Linguist**: research, source review, synthesis, and citation-heavy work.
- **Engineer**: coding, deployment, debugging, automation, and Ale self-improvement.

Default models:

- Actor: `claude-haiku-4-5`
- Linguist: `claude-opus-4-6`
- Engineer: `claude-opus-4-6`

## Run

Install with uv:

```bash
uv sync
```

Set credentials:

```bash
export DISCORD_TOKEN_FILE="$HOME/.config/ale/discord-token"
export ALE_ALLOWED_USER_IDS="1234567890"
```

Ale can use local Claude and Codex CLI auth. `ANTHROPIC_API_KEY` and `CODEX_API_KEY`
are optional, not required.

For DM-only testing, Ale leaves Discord's privileged Message Content intent off by
default. Set `ALE_DISCORD_MESSAGE_CONTENT_INTENT=true` only after enabling that
privileged intent in the Discord Developer Portal.

Long Linguist and Engineer replies cross over `ALE_DISCORD_ARTIFACT_THRESHOLD`
(default 2400 chars). When that happens:

- The full report is saved as `<message_id>.md` and rendered to `<message_id>.pdf`
  under `~/.local/state/ale/artifacts/<thread>/`.
- A short Actor-voiced summary is generated (one fast SDK call, no tools, capped
  at `ALE_ACTOR_SUMMARY_MAX_CHARS`, default 1400) and posted as the visible
  Discord message.
- The PDF is attached. If `ALE_ARTIFACT_PUBLISH_COMMAND` returns a URL, the
  link is included in the message instead of a local attachment.

Knobs:

- `ALE_ENABLE_PDF_ARTIFACTS` (default `true`) — render the PDF.
- `ALE_ENABLE_ACTOR_SUMMARY` (default `true`) — run the Actor summarizer turn.
- `ALE_ACTOR_SUMMARY_MAX_CHARS` (default `1400`) — hard cap on the summary.
- `ALE_ACTOR_SUMMARY_MAX_BUDGET_USD` (default `0.05`) — per-summary budget.

Set `ALE_ARTIFACT_PUBLISH_COMMAND` to turn those files into links. Ale runs the command
without a shell, replaces `{path}` with the Markdown file path if present, and sends the
first URL printed to stdout. Example:

```bash
export ALE_ARTIFACT_PUBLISH_COMMAND="publish-to-google-doc {path}"
```

The command can be a local wrapper around Google Drive/Docs, `rclone`, or any private
publisher that uploads the Markdown and prints a share URL.

## Failure Recovery

Explicit persona messages like `Linguist:` and `Engineer:` skip the cheap LLM router and
route directly. If a user-facing agent turn fails, Ale tells you it hit an internal error,
creates an Engineer incident Workspace, writes the failure context there, and asks Engineer
for a no-edit diagnosis in the background.

Start Discord:

```bash
uv run ale discord
```

Local one-shot smoke test:

```bash
uv run ale ask "Actor: summarize what Ale can do"
```

## State

Defaults:

- Thread store: `~/.local/state/ale/threads`
- Turn logs: `~/.local/state/ale/logs/turns.jsonl`
- Global event log: `~/.local/state/ale/logs/all-events.jsonl`
- Subsystem logs: `agent.log`, `agent-team.log`, `core-system.log`, `discord.log`,
  `reload.log`, `router.log`, `threads.log`, `tools.log`, `workspace.log`
- Team workspaces: `~/.local/state/ale/workspaces`
- Memory files: `~/.claude/projects/-home-claude/memory`

`agent-team.log` carries cross-agent and team-coordination signals: turn lifecycle
for Linguist/Engineer (with `turn_id`, `parent_turn_id`, `agent_role`, `team`,
`tool_args_summary`, `duration_ms`, `cost_usd_estimate`), workspace creation,
team chat messages, codex worker dispatch/completion, and incident handoffs.
`reload.log` carries every staged-reload event with `reload_id`, `tier`,
`duration_ms`, `changed_files`, and outcome.

Each thread has `meta.json`, `messages.jsonl`, and `recap.md`.

## Workspaces

Linguist and Engineer can create persistent Workspaces. A Workspace has:

- `meta.json`
- `team_chat.jsonl`
- `files/`
- `transcripts/`

The shared files and team chat are designed so partial research, plans, reviews, and
implementation notes survive agent failure or dismissal.

## Safety Defaults

Actor uses lightweight tools and visible delegation to Linguist or Engineer. Engineer can use
filesystem and shell tools through the Claude Agent SDK. Ale's custom `bash` MCP tool and
`codex_exec` implementation-worker tool are disabled unless `ALE_ENABLE_BASH_TOOL=true` and
`ALE_ENABLE_CODEX_EXEC=true`, respectively.

## Codex Workers

Engineer can call `codex_exec` as a narrow implementation worker. The tool runs
`codex exec --json`, captures the final message with `-o`, and stores Codex JSONL events,
stderr, and the final message in the active Workspace when `workspace_id` is supplied.

Supported modes:

- `read_only`: analysis only with `--sandbox read-only`
- `workspace_write`: normal patches with `--sandbox workspace-write`
- `full_auto`: autonomous edits with `--full-auto`, intended only for isolated environments

## Hot Reload

Reload is **manual**: the Lead Engineer drives staged reloads from inside a
Workspace by calling the `reload_*` MCP tools. The file-system watchdog is
**off by default** — flip `ALE_HOT_RELOAD_ENABLED=true` only when you want
auto-reload during local development.

Three staged tiers:

1. **Config + prompts** (`reload_config_and_prompts`). Re-reads `.env` into a
   fresh `Settings` and re-imports `ale.personas` / `ale.prompts` so the
   BASE_SYSTEM and persona prompts visible to the next SDK call reflect disk.
   In-flight turns keep their snapshot.
2. **Tool module** (`reload_tools`). Re-imports `ale.tools` so MCP tool
   definitions on the next agent call are fresh. Waits for in-flight turns to
   drain before swapping (the bundled Claude CLI subprocess crashes if its
   tool registry changes mid-iteration).
3. **Candidate process + health check** (`candidate_health_check`). Spawns
   `ale health-check` as a subprocess to smoke-test the new code path end to
   end. `promote_candidate` re-execs the current process when
   `ALE_ENABLE_SELF_PROMOTE=true`. `rollback_reload` restores the previous
   snapshot if a regression slips through.

Triggers:

- Engineer MCP tools (primary): `reload_plan` (read-only inspection),
  `reload_config_and_prompts`, `reload_tools`, `candidate_health_check`,
  `promote_candidate`, `rollback_reload`.
- `SIGHUP` to the Discord process — runs Tier 1, then Tier 2 if
  `ALE_HOT_RELOAD_TOOLS=true`, then Tier 3 if `ALE_HOT_RELOAD_CANDIDATE=true`.
- File watchdog (off by default; turn on with `ALE_HOT_RELOAD_ENABLED=true`).

The watchdog respects an in-flight turn counter and emits
`watchdog_awaiting_idle` when it has to defer a reload, so even with the
watchdog enabled it will not crash a running turn.

Relevant env vars:

- `ALE_HOT_RELOAD_ENABLED` — auto file-watcher (default `false`)
- `ALE_HOT_RELOAD_TOOLS` — let `reload_tools` re-import (default `true`)
- `ALE_HOT_RELOAD_CANDIDATE` — let SIGHUP/watchdog also fire candidate health check (default `false`)
- `ALE_ENABLE_RELOAD_TOOLS` — expose reload MCP tools to Engineer (default `true`)
- `ALE_ENABLE_SELF_PROMOTE` — allow `promote_candidate` to re-exec (default `false`)
- `ALE_RELOAD_WATCH_PATHS` — roots to watch when watchdog is on (default `ale`)
- `ALE_RELOAD_DEBOUNCE_SECONDS` (default `1.5`)
- `ALE_RELOAD_POLL_INTERVAL_SECONDS` (default `1.0`)
- `ALE_RELOAD_HEALTH_CHECK_TIMEOUT_SECONDS` (default `60`)
- `ALE_CANDIDATE_PYTHON` — explicit Python executable for candidate spawn

## Local Auth

By default, Ale does not require AI API keys. The Claude Agent SDK uses local Claude
auth when no `ANTHROPIC_API_KEY` is present, and `codex_exec` uses local Codex auth
when no `CODEX_API_KEY` is present. Set `ALE_ENABLE_LLM_ROUTER=false` if you want
thread routing and recap refresh to stay deterministic/local during testing.
