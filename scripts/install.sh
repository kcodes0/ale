#!/usr/bin/env bash
set -euo pipefail

APP_NAME="pi-cloud-delegation-service"
APP_DIR="${APP_DIR:-/opt/${APP_NAME}}"
ENV_DIR="${ENV_DIR:-/etc/pi-cloud}"
ENV_FILE="${ENV_FILE:-${ENV_DIR}/pi-cloud.env}"
SERVICE_USER="${SERVICE_USER:-pi-cloud}"
BUILD_IMAGE="${BUILD_IMAGE:-true}"
INSTALL_SYSTEMD="${INSTALL_SYSTEMD:-auto}"
INSTALL_DOCKER="${INSTALL_DOCKER:-auto}"
INSTALL_PI_CLI="${INSTALL_PI_CLI:-true}"
INSTALL_POKE_CLI="${INSTALL_POKE_CLI:-false}"
INSTALL_CLOUDFLARED="${INSTALL_CLOUDFLARED:-false}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
is_root() { [[ "$(id -u)" -eq 0 ]]; }

apt_install() {
  if have apt-get; then
    log "Installing OS packages: $*"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
  else
    return 1
  fi
}

ensure_os_packages() {
  local missing=()
  for cmd in curl git rsync; do
    have "$cmd" || missing+=("$cmd")
  done
  if ((${#missing[@]} == 0)); then return; fi
  is_root || fail "Missing required commands: ${missing[*]}. Rerun with sudo so the installer can install them."
  apt_install ca-certificates curl git rsync gnupg lsb-release || fail "Install missing OS packages manually: ${missing[*]}"
}

ensure_bun() {
  if ! have bun; then
    log "Installing Bun"
    curl -fsSL https://bun.sh/install | bash
    export PATH="$HOME/.bun/bin:$PATH"
  fi
  have bun || fail "bun was not found after installation"

  # systemd unit uses /usr/local/bin/bun. Bun's installer places it under ~/.bun/bin.
  if is_root && [[ ! -x /usr/local/bin/bun ]]; then
    local bun_path
    bun_path="$(command -v bun)"
    ln -sf "$bun_path" /usr/local/bin/bun
  fi
}

ensure_docker() {
  if [[ "${BUILD_IMAGE}" != "true" ]]; then return; fi
  if have docker; then return; fi
  if [[ "${INSTALL_DOCKER}" == "false" ]]; then
    fail "docker is required when BUILD_IMAGE=true. Install Docker or rerun with BUILD_IMAGE=false."
  fi
  is_root || fail "Docker is missing. Rerun with sudo so the installer can install Docker, or set BUILD_IMAGE=false."

  if have apt-get; then
    log "Installing Docker from Docker's official convenience script"
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker 2>/dev/null || true
  else
    fail "Docker is missing and automatic Docker install is only implemented for apt-based Linux."
  fi
  have docker || fail "docker was not found after installation"
}

ensure_node_tools() {
  if [[ "${INSTALL_PI_CLI}" == "true" ]] && ! have pi; then
    log "Installing pi CLI globally for host-side OAuth setup"
    if have npm; then
      npm install -g @mariozechner/pi-coding-agent
    else
      bun add -g @mariozechner/pi-coding-agent
      export PATH="$HOME/.bun/bin:$PATH"
    fi
  fi

  if [[ "${INSTALL_POKE_CLI}" == "true" ]] && ! have poke; then
    log "Installing Poke CLI globally"
    if have npm; then
      npm install -g poke
    else
      bun add -g poke
      export PATH="$HOME/.bun/bin:$PATH"
    fi
  fi
}

ensure_cloudflared() {
  if [[ "${INSTALL_CLOUDFLARED}" != "true" ]] || have cloudflared; then return; fi
  is_root || fail "cloudflared install requires sudo/root"
  if have apt-get; then
    log "Installing cloudflared"
    mkdir -p --mode=0755 /usr/share/keyrings
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" > /etc/apt/sources.list.d/cloudflared.list
    apt-get update
    apt-get install -y cloudflared
  else
    fail "cloudflared automatic install is only implemented for apt-based Linux"
  fi
}

if [[ "$(uname -s)" != "Linux" && "${INSTALL_SYSTEMD}" != "false" ]]; then
  warn "Non-Linux host detected; skipping systemd setup. Use bun run start manually."
  INSTALL_SYSTEMD=false
fi

ensure_os_packages
ensure_bun
ensure_docker
ensure_node_tools
ensure_cloudflared

log "Preparing application directory: ${APP_DIR}"
if [[ "$(pwd)" != "${APP_DIR}" ]]; then
  if [[ ! -w "$(dirname "${APP_DIR}")" ]]; then
    is_root || fail "Installing to ${APP_DIR} requires root. Rerun with sudo or set APP_DIR to a writable path."
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
  if have systemctl && is_root; then INSTALL_SYSTEMD=true; else INSTALL_SYSTEMD=false; fi
fi

if [[ "${INSTALL_SYSTEMD}" == "true" ]]; then
  is_root || fail "systemd installation requires root"
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
2. Configure pi auth for the worker. Recommended host login:
   sudo -u ${SERVICE_USER} mkdir -p /home/${SERVICE_USER}/.pi/agent
   sudo -u ${SERVICE_USER} HOME=/home/${SERVICE_USER} pi
   # run /login and select ChatGPT/Codex, Claude, or Copilot
   # then set PI_AGENT_HOME=/home/${SERVICE_USER}/.pi in ${ENV_FILE}

   Alternative Docker-based login if you do not want host pi installed:
   sudo -u ${SERVICE_USER} mkdir -p /home/${SERVICE_USER}/.pi
   docker run --rm -it -v /home/${SERVICE_USER}/.pi:/home/pi/.pi ${PI_AGENT_IMAGE:-pi-agent:latest} pi
3. Expose MCP:
   - local/dev: poke tunnel http://localhost:3000/mcp --name "Pi Cloud"
   - server/prod: cloudflared tunnel --url http://localhost:3000
4. Register remote URL with Poke if not using poke tunnel:
   poke mcp add https://your-url.example/mcp --name "Pi Cloud" --api-key "<PI_CLOUD_API_KEY>"

Health check:
  curl http://localhost:3000/health

Useful installer flags:
  BUILD_IMAGE=false            Skip Docker image build
  INSTALL_DOCKER=false         Do not auto-install Docker
  INSTALL_PI_CLI=false         Do not install host pi CLI
  INSTALL_POKE_CLI=true        Install Poke CLI globally
  INSTALL_CLOUDFLARED=true     Install cloudflared on apt-based Linux
  INSTALL_SYSTEMD=false        Skip systemd service install

EOF
