# Pi Cloud Delegation Service

A Bun/TypeScript orchestration layer that lets Poke start safe, high-level pi coding jobs without exposing a raw pi process over iMessage.

## What it exposes

MCP endpoint: `POST /mcp`

Tools:

- `start_pi_task`
- `get_pi_task_status`
- `cancel_pi_task`
- `list_pi_tasks`
- `get_pi_task_logs`
- `approve_pi_task_action`

HTTP API is also available under `/api/tasks` for debugging/automation.

## Setup

Quick install on a Linux server:

```bash
sudo ./scripts/install.sh
```

Local/dev install:

```bash
bun install
cp .env.example .env
# edit .env
bun run index.ts
```

### ChatGPT OAuth / pi auth in Docker

Pi stores subscription OAuth credentials in `~/.pi/agent/auth.json`. To reuse ChatGPT/Codex OAuth in worker containers, login as the service user and set `PI_AGENT_HOME`:

```bash
sudo -u pi-cloud mkdir -p /home/pi-cloud/.pi/agent
sudo -u pi-cloud HOME=/home/pi-cloud pi
# run /login, choose OpenAI Codex / ChatGPT
# then set PI_AGENT_HOME=/home/pi-cloud/.pi in /etc/pi-cloud/pi-cloud.env
```

The service mounts that directory into each worker at `/home/pi/.pi`. If this is awkward, use API-key auth via environment variables instead, or run unsandboxed only for local development.

Register with Poke:

```bash
poke mcp add https://your-server.com/mcp --name "Pi Cloud" --api-key "$PI_CLOUD_API_KEY"
```

For a dev URL, `poke tunnel` is fine:

```bash
poke tunnel http://localhost:3000/mcp --name "Pi Cloud"
```

For an always-on server, prefer Cloudflare Tunnel, a reverse proxy, or any HTTPS ingress in front of `localhost:3000`.

## Key configuration

- `ALLOWED_REPOS`: comma-separated repo allowlist, e.g. `owner/repo` or `owner/repo=https://github.com/owner/repo.git`.
- `PI_CLOUD_API_KEY`: bearer token required for MCP/API requests.
- `PI_AGENT_IMAGE`: Docker image used for jobs. It should contain git, the pi CLI, auth/config, and needed toolchains.
- `PI_RUNNER_COMMAND`: command executed in the checked-out repo inside the sandbox. Default: `pi -p "$PI_TASK_PROMPT"`.
- `PI_AGENT_HOME`: optional host pi home, e.g. `/home/pi-cloud/.pi`, mounted into Docker as `/home/pi/.pi` so ChatGPT/Codex OAuth from `pi /login` can be reused.
- `POKE_API_KEY`: optional; enables completion/failure notifications via the Poke inbound API / SDK fallback.

## Safety behavior

- Repos are allowlisted.
- Each job gets a fresh workspace under `.pi-cloud/workspaces`.
- Jobs run in Docker by default.
- Runtime and concurrency are capped.
- Deploy/destructive/secret-looking tasks enter `awaiting_approval` until `approve_pi_task_action` is called.
- Logs are captured under `.pi-cloud/logs`.

Do not run production jobs with `PI_CLOUD_ALLOW_UNSANDBOXED=true`; it is for local development only.

## HTTP API

```bash
curl -H "Authorization: Bearer $PI_CLOUD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"repo":"owner/repo","task":"Fix failing tests","mode":"fix"}' \
  http://localhost:3000/api/tasks
```
