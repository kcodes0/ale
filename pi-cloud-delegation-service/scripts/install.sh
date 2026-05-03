#!/usr/bin/env bash
set -euo pipefail

APP_NAME="pi-cloud-delegation-service"
APP_DIR="${APP_DIR:-/opt/${APP_NAME}}"
ENV_DIR="${ENV_DIR:-/etc/pi-cloud}"
ENV_FILE="${ENV_FILE:-${ENV_DIR}/pi-cloud.env}"
SERVICE_USER="${SERVICE_USER:-pi-cloud}"
BUILD_IMAGE="${BUILD_IMAGE:-true}"
INSTALL_SYSTEMD="${INSTALL_SYSTEMD:-auto}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

if [[ "$(uname -s)" != "Linux" && "${INSTALL_SYSTEMD}" != "false" ]]; then
  warn "Non-Linux host detected; skipping systemd setup. Use bun run start manually."
  INSTALL_SYSTEMD=false
fi

if ! have bun; then
  log "Installing Bun"
  curl -fsSL https://bun.sh/install | bash
  export PATH="$HOME/.bun/bin:$PATH"
fi
have bun || fail "bun was not found after installation"

if ! have git; then
  fail "git is required. Install git and rerun this script."
fi

if [[ "${BUILD_IMAGE}" == "true" ]]; then
  have docker || fail "docker is required when BUILD_IMAGE=true. Install Docker or rerun with BUILD_IMAGE=false."
fi

log "Preparing application directory: ${APP_DIR}"
if [[ "$(pwd)" != "${APP_DIR}" ]]; then
  if [[ "$(id -u)" -ne 0 && "${APP_DIR}" == /opt/* ]]; then
    fail "Installing to ${APP_DIR} requires root. Rerun with sudo or set APP_DIR to a writable path."
  fi
  mkdir -p "${APP_DIR}"
  rsync -a --delete \
    --exclude node_modules \
    --exclude .git \
    --exclude .pi-cloud \
    ./ "${APP_DIR}/"
fi

cd "${APP_DIR}"
log "Installing Bun dependencies"
bun install --frozen-lockfile || bun install
bun run typecheck

log "Creating runtime directories"
mkdir -p "${APP_DIR}/.pi-cloud/workspaces" "${APP_DIR}/.pi-cloud/logs" "${ENV_DIR}"

if [[ ! -f "${ENV_FILE}" ]]; then
  log "Creating ${ENV_FILE}"
  cp .env.example "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  warn "Edit ${ENV_FILE} before exposing the service. At minimum set PI_CLOUD_API_KEY, POKE_API_KEY, and ALLOWED_REPOS."
else
  log "Keeping existing ${ENV_FILE}"
fi

if [[ "${BUILD_IMAGE}" == "true" ]]; then
  log "Building pi agent Docker image"
  docker build -t "${PI_AGENT_IMAGE:-pi-agent:latest}" -f docker/pi-agent.Dockerfile .
fi

if [[ "${INSTALL_SYSTEMD}" == "auto" ]]; then
  if have systemctl && [[ "$(id -u)" -eq 0 ]]; then INSTALL_SYSTEMD=true; else INSTALL_SYSTEMD=false; fi
fi

if [[ "${INSTALL_SYSTEMD}" == "true" ]]; then
  [[ "$(id -u)" -eq 0 ]] || fail "systemd installation requires root"
  log "Creating service user ${SERVICE_USER}"
  if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
  fi
  if getent group docker >/dev/null 2>&1; then
    usermod -aG docker "${SERVICE_USER}" || true
  else
    warn "docker group not found; the service user may not be able to run containers"
  fi
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}/.pi-cloud"
  cp deploy/pi-cloud.service /etc/systemd/system/pi-cloud.service
  sed -i "s#WorkingDirectory=/opt/pi-cloud-delegation-service#WorkingDirectory=${APP_DIR}#" /etc/systemd/system/pi-cloud.service
  systemctl daemon-reload
  systemctl enable pi-cloud.service
  log "Starting pi-cloud.service"
  systemctl restart pi-cloud.service
  systemctl --no-pager --full status pi-cloud.service || true
fi

cat <<EOF

Install complete.

Next steps:
1. Edit: ${ENV_FILE}
2. Configure pi auth for the worker. Recommended:
   sudo -u ${SERVICE_USER} mkdir -p /home/${SERVICE_USER}/.pi/agent
   sudo -u ${SERVICE_USER} PI_HOME=/home/${SERVICE_USER}/.pi pi
   # run /login and select ChatGPT/Codex, Claude, or Copilot
   # then set PI_AGENT_HOME=/home/${SERVICE_USER}/.pi in ${ENV_FILE}
3. Expose MCP:
   - local/dev: poke tunnel http://localhost:3000/mcp --name "Pi Cloud"
   - server/prod: cloudflared tunnel --url http://localhost:3000
4. Register remote URL with Poke if not using poke tunnel:
   poke mcp add https://your-url.example/mcp --name "Pi Cloud" --api-key "<PI_CLOUD_API_KEY>"

Health check:
  curl http://localhost:3000/health

EOF
