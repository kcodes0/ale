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

### 1. Configure the service environment

Create/edit the production env file before exposing the service:

```bash
sudo mkdir -p /etc/pi-cloud
sudo cp .env.example /etc/pi-cloud/pi-cloud.env
sudo chmod 600 /etc/pi-cloud/pi-cloud.env
sudo nano /etc/pi-cloud/pi-cloud.env
```

Set at least:

```env
PI_CLOUD_API_KEY=replace-with-a-long-random-token
POKE_API_KEY=replace-with-your-poke-key
ALLOWED_REPOS=owner/repo,owner/other-repo=https://github.com/owner/other-repo.git
PI_AGENT_HOME=/home/pi-cloud/.pi
```

### 2. Install the service

From the repo root:

```bash
sudo ./scripts/install.sh
```

The installer checks/installs required host dependencies on apt-based Linux, including Bun, git, rsync, Docker when building the worker image, the host pi CLI for OAuth setup, and optionally Poke CLI/cloudflared via flags.

Verify systemd and the local health endpoint:

```bash
systemctl status pi-cloud.service --no-pager --full
curl http://localhost:3000/health
```

Expected health output:

```json
{"ok":true,"jobs":0,"activeJobs":0}
```

If the service is not healthy, check logs:

```bash
sudo journalctl -u pi-cloud.service --no-pager -n 100
```

### 3. Configure pi auth for Docker workers

Pi stores subscription OAuth credentials in `~/.pi/agent/auth.json`. The production service runs as the `pi-cloud` user, so the worker auth should live under `/home/pi-cloud/.pi`.

Preferred interactive login:

```bash
sudo -u pi-cloud mkdir -p /home/pi-cloud/.pi/agent
sudo -u pi-cloud HOME=/home/pi-cloud pi
# inside pi, run /login and choose OpenAI Codex / ChatGPT, Claude, or Copilot
```

Then confirm this is set in `/etc/pi-cloud/pi-cloud.env`:

```env
PI_AGENT_HOME=/home/pi-cloud/.pi
```

Restart after auth/env changes:

```bash
sudo systemctl restart pi-cloud.service
```

Alternative if you already logged in as your current user:

```bash
sudo mkdir -p /home/pi-cloud/.pi
sudo rsync -a ~/.pi/ /home/pi-cloud/.pi/
sudo chown -R pi-cloud:pi-cloud /home/pi-cloud/.pi
sudo chmod 700 /home/pi-cloud/.pi /home/pi-cloud/.pi/agent
sudo chmod 600 /home/pi-cloud/.pi/agent/auth.json
sudo systemctl restart pi-cloud.service
```

### Local/dev install

```bash
bun install
cp .env.example .env
# edit .env
bun run index.ts
```

The service mounts `PI_AGENT_HOME` into each worker at `/home/pi/.pi`. If this is awkward, use API-key auth via environment variables instead, or run unsandboxed only for local development.

## Expose/register with Poke

Register with Poke:

```bash
npx poke mcp add https://your-server.com/mcp --name "Pi Cloud" --api-key "$PI_CLOUD_API_KEY"
```

For a dev URL, `npx poke tunnel` is fine, but it normally stays attached to the terminal:

```bash
npx poke tunnel http://localhost:3000/mcp --name "Pi Cloud" --recipe
```

For an always-on Poke tunnel, run it as a separate systemd service. Adjust `User`, paths, and Node path for your host:

```ini
# /etc/systemd/system/pi-cloud-tunnel.service
[Unit]
Description=Pi Cloud Poke Tunnel
After=network-online.target pi-cloud.service
Wants=network-online.target
Requires=pi-cloud.service

[Service]
Type=simple
User=claude
Group=claude
WorkingDirectory=/home/claude/ale
Environment=HOME=/home/claude
Environment=XDG_CONFIG_HOME=/home/claude/.config
Environment=PATH=/home/claude/.nvm/versions/node/v24.15.0/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/claude/.nvm/versions/node/v24.15.0/bin/npx --yes poke tunnel http://localhost:3000/mcp --name "Pi Cloud" --recipe
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pi-cloud-tunnel.service
systemctl status pi-cloud-tunnel.service --no-pager --full
sudo journalctl -u pi-cloud-tunnel.service --no-pager -n 100
```

When `--recipe` is enabled, the tunnel logs include a `Recipe: https://poke.com/r/...` link and QR code. Before treating a 404 or "Recipe not found" page as a broken setup, check the Poke app's **Integrations** tab first. The tunnel-created MCP integration may already be installed/listed there even if the generated recipe link does not render as a public recipe page.

Troubleshoot further only if the integration is missing or the tunnel service is not active:

```bash
systemctl status pi-cloud-tunnel.service --no-pager --full
sudo journalctl -u pi-cloud-tunnel.service --no-pager -n 100
```

For production you can also use Cloudflare Tunnel, a reverse proxy, or any HTTPS ingress in front of `localhost:3000`.

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
