#!/usr/bin/env bash
# Helper installer to set up master API, dashboard, and a local agent.
set -euo pipefail

AGENT_IMAGE=${AGENT_IMAGE:-"iperf-agent:latest"}
AGENT_PORT=${AGENT_PORT:-8000}
IPERF_PORT=${IPERF_PORT:-5201}
MASTER_API_PORT=${MASTER_API_PORT:-9000}
MASTER_WEB_PORT=${MASTER_WEB_PORT:-9100}
START_IPERF_SERVER=${START_IPERF_SERVER:-true}
DEPLOY_REMOTE=${DEPLOY_REMOTE:-false}
HOSTS_FILE=${HOSTS_FILE:-""}
CLEAN_EXISTING=${CLEAN_EXISTING:-false}
REPO_REF=${REPO_REF:-""}
PORT_CONFIG_FILE=${PORT_CONFIG_FILE:-""}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT=""
DEFAULT_REPO_URL=""
REPO_UPDATED=false
COMPOSE_CMD=""

log() { printf "[install-master] %s\n" "$*"; }

select_python() {
  if command -v python3 >/dev/null 2>&1; then
    echo "python3"
  elif command -v python >/dev/null 2>&1; then
    echo "python"
  else
    echo ""
  fi
}

port_in_use() {
  local port=$1 py
  py=$(select_python)

  if [ -z "${py}" ]; then
    return 2
  fi

  "${py}" - "$port" <<'PY'
import errno, socket, sys

port = int(sys.argv[1])

def can_bind(family, address):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((address, port))
        return False
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return True

        # If binding fails for another reason (e.g., IPv6 unavailable), log it
        # and allow the installer to continue with the user's chosen port.
        print(f"[install-master] Port probe on {address}:{port} skipped: {exc}", file=sys.stderr)
        return False


# Check IPv4 and IPv6 independently; a conflict on either means the port is in use.
if can_bind(socket.AF_INET, "0.0.0.0"):
    sys.exit(1)

try:
    if can_bind(socket.AF_INET6, "::"):
        sys.exit(1)
except OSError:
    # IPv6 may be unavailable; ignore and continue.
    pass

sys.exit(0)
PY
}

prompt_for_port() {
  local label=$1 current=$2 new_port
  while true; do
    read -r -p "${label} port (${current}) is in use. Enter a different port: " new_port

    if [ -z "${new_port}" ]; then
      new_port=${current}
    fi

    if [[ "${new_port}" =~ ^[0-9]+$ ]] && [ "${new_port}" -ge 1 ] && [ "${new_port}" -le 65535 ]; then
      echo "${new_port}"
      return
    fi

    log "Invalid port: ${new_port}. Please provide a value between 1 and 65535."
  done
}

ensure_ports_available() {
  local py status label port_var port_value

  py=$(select_python)
  if [ -z "${py}" ]; then
    log "Python is required for port availability checks. Skipping prompts and continuing with current values."
    return
  fi

  for entry in \
    "Master API:MASTER_API_PORT" \
    "Dashboard:MASTER_WEB_PORT" \
    "Agent API:AGENT_PORT" \
    "iperf3:IPERF_PORT"; do
    label=${entry%%:*}
    port_var=${entry#*:}
    port_value=$(eval "echo \${${port_var}}")

    status=0
    port_in_use "${port_value}" || status=$?

    if [ "${status}" -eq 0 ]; then
      continue
    fi

    if [ "${status}" -eq 2 ]; then
      log "Skipping port availability checks due to missing Python interpreter."
      return
    fi

    if [ -t 0 ]; then
      while [ "${status}" -ne 0 ]; do
        port_value=$(prompt_for_port "${label}" "${port_value}")
        eval "${port_var}=${port_value}"
        port_in_use "${port_value}"; status=$?
        if [ "${status}" -eq 0 ]; then
          log "Using ${label} port ${port_value}."
        fi
      done
    else
      log "${label} port ${port_value} is already in use. Re-run with a different port via flags or environment variables."
      exit 1
    fi
  done
}

detect_public_ip() {
  local ip
  if command -v curl >/dev/null 2>&1; then
    ip=$(curl -fsS https://api.ipify.org 2>/dev/null || true)
  fi

  if [ -z "${ip}" ] && command -v dig >/dev/null 2>&1; then
    ip=$(dig +short myip.opendns.com @resolver1.opendns.com 2>/dev/null | tail -n1)
  fi

  echo "${ip:-}"
}

maybe_load_port_config() {
  local config_path="${PORT_CONFIG_FILE}" candidate

  if [ -z "${config_path}" ]; then
    for candidate in "${SCRIPT_DIR}/ports.env" "${PWD}/ports.env"; do
      if [ -f "${candidate}" ]; then
        config_path="${candidate}"
        break
      fi
    done
  fi

  if [ -n "${config_path}" ]; then
    if [ -f "${config_path}" ]; then
      # shellcheck disable=SC1090
      set -a; source "${config_path}"; set +a
      log "Loaded port configuration from ${config_path}."
    else
      log "Port config file ${config_path} not found; continuing with defaults and CLI args."
    fi
  fi
}

preprocess_config_arg() {
  local expect_path=false arg
  for arg in "$@"; do
    if [ "${expect_path}" = true ]; then
      PORT_CONFIG_FILE="$arg"
      expect_path=false
      continue
    fi

    case "${arg}" in
      --config)
        expect_path=true ;;
      --config=*)
        PORT_CONFIG_FILE="${arg#--config=}" ;;
    esac
  done
}

detect_repo_root() {
  local git_root="" fallback=""

  if command -v git >/dev/null 2>&1; then
    git_root=$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel 2>/dev/null || true)
  fi

  if [ -n "${git_root}" ] && [ -d "${git_root}" ]; then
    REPO_ROOT="${git_root}"
  elif [ -f "${SCRIPT_DIR}/docker-compose.yml" ]; then
    REPO_ROOT="${SCRIPT_DIR}"
  elif [ -f "${PWD}/docker-compose.yml" ]; then
    REPO_ROOT="${PWD}"
  else
    REPO_ROOT="${SCRIPT_DIR}/iperf3-test-tools"
  fi

  if [ -z "${DEFAULT_REPO_URL}" ] && command -v git >/dev/null 2>&1; then
    DEFAULT_REPO_URL="$(git -C "${REPO_ROOT}" remote get-url origin 2>/dev/null || true)"
  fi
}

normalize_repo_url() {
  local url=$1

  case "${url}" in
    git@github.com:*)
      echo "https://github.com/${url#git@github.com:}"
      ;;
    ssh://git@github.com/*)
      echo "https://github.com/${url#ssh://git@github.com/}"
      ;;
    *)
      echo "${url}"
      ;;
  esac
}

download_repo_if_missing() {
  if [ -f "${REPO_ROOT}/docker-compose.yml" ]; then
    return
  fi

  ensure_command git git

  if [ -d "${REPO_ROOT}" ] && [ -n "$(ls -A "${REPO_ROOT}" 2>/dev/null)" ]; then
    log "Repository directory ${REPO_ROOT} exists but lacks required files; replacing it..."
    rm -rf "${REPO_ROOT}"
  fi

  log "Cloning repository from ${REPO_URL} into ${REPO_ROOT}..."
  mkdir -p "$(dirname "${REPO_ROOT}")"
  if ! git clone --depth 1 "${REPO_URL}" "${REPO_ROOT}" >/dev/null 2>&1; then
    log "Failed to clone repository from ${REPO_URL}. Please download it manually and re-run."
    exit 1
  fi

  if [ -n "${REPO_REF}" ]; then
    if git -C "${REPO_ROOT}" fetch origin "${REPO_REF}" >/dev/null 2>&1; then
      git -C "${REPO_ROOT}" checkout "${REPO_REF}" >/dev/null 2>&1 || true
    fi
  fi
}

ensure_command() {
  local bin=$1 pkg=$2
  if command -v "$bin" >/dev/null 2>&1; then
    return
  fi

  if command -v apt-get >/dev/null 2>&1; then
    log "Installing missing dependency: ${pkg}..."
    apt-get update -y >/dev/null 2>&1
    apt-get install -y "$pkg" >/dev/null 2>&1
  else
    log "Missing required command: ${bin}. Please install ${pkg} and re-run."
    exit 1
  fi
}

update_repo() {
  if [ ! -d "${REPO_ROOT}/.git" ] || ! command -v git >/dev/null 2>&1; then
    log "Git repository not detected; skipping auto-update."
    return
  fi

  local ref before after commit_delta
  ref="${REPO_REF:-$(git -C "${REPO_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")}"
  before=$(git -C "${REPO_ROOT}" rev-parse HEAD)

  git -C "${REPO_ROOT}" remote set-url origin "${REPO_URL}" >/dev/null 2>&1 || true
  if git -C "${REPO_ROOT}" fetch origin "${ref}" >/dev/null 2>&1 && \
     git -C "${REPO_ROOT}" pull --ff-only origin "${ref}" >/dev/null 2>&1; then
    after=$(git -C "${REPO_ROOT}" rev-parse HEAD)
  else
    log "Auto-update failed; proceeding with local copy."
    after="${before}"
  fi

  if [ "${before}" != "${after}" ]; then
    REPO_UPDATED=true
    commit_delta=$(git -C "${REPO_ROOT}" rev-list --count "${before}..${after}" 2>/dev/null || echo "?")
    log "Repository updated: ${commit_delta} new commit(s) applied (${before:0:7} -> ${after:0:7})."
  else
    log "Repository already up to date (${after:0:7})."
  fi
}

rerun_if_repo_updated() {
  if [ "${REPO_UPDATED}" != true ]; then
    return
  fi

  if [ "${INSTALL_MASTER_UPDATED:-0}" -eq 1 ]; then
    return
  fi

  if [ ! -x "${REPO_ROOT}/install_master.sh" ]; then
    log "Updated repository detected but installer not found at ${REPO_ROOT}; continuing without restart."
    return
  fi

  log "Repository updated; re-running installer with latest version..."
  INSTALL_MASTER_UPDATED=1 exec "${REPO_ROOT}/install_master.sh" "$@"
}

ensure_docker() {
  ensure_command curl curl

  if command -v docker >/dev/null 2>&1; then
    return
  fi

  log "Docker not found; attempting installation via get.docker.com..."
  curl -fsSL https://get.docker.com | sh
}

ensure_compose() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
    return
  fi

  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
    return
  fi

  log "Docker Compose is required. Install docker-compose or the Docker Compose plugin and re-run."
  exit 1
}

ensure_compose_file() {
  local compose_file="${REPO_ROOT}/docker-compose.yml"
  if [ ! -f "${compose_file}" ]; then
    log "Missing compose file at ${compose_file}; attempting to download repository..."
    download_repo_if_missing

    if [ -f "${compose_file}" ]; then
      return
    fi

    log "Compose file still not found after download attempt."
    log "Ensure you are running the installer from a valid checkout (repo root: ${REPO_ROOT})."
    exit 1
  fi
}

cleanup_existing_services() {
  local project_name existing=""
  project_name=${COMPOSE_PROJECT_NAME:-$(basename "${REPO_ROOT}")}

  if docker ps -a --format '{{.Names}}' | grep -q "^iperf-agent$"; then
    existing+=" iperf-agent"
  fi

  if docker ps -a --format '{{.Names}}' | grep -Eq "^${project_name}-master-api-[0-9]+$"; then
    existing+=" ${project_name}-master-api"
  fi

  if docker ps -a --format '{{.Names}}' | grep -Eq "^${project_name}-db-[0-9]+$"; then
    existing+=" ${project_name}-db"
  fi

  if [ -z "${existing}" ]; then
    return
  fi

  log "Detected existing services:${existing}"

  if [ "${CLEAN_EXISTING}" = true ]; then
    log "Removing existing master stack and local agent containers..."
    MASTER_API_PORT="${MASTER_API_PORT}" MASTER_WEB_PORT="${MASTER_WEB_PORT}" ${COMPOSE_CMD} -f "${REPO_ROOT}/docker-compose.yml" down -v || true
    docker rm -f iperf-agent >/dev/null 2>&1 || true
  else
    log "Re-run with --clean-existing to automatically remove them before reinstalling."
  fi
}

wait_for_local_agent() {
  local retries=30 delay=2
  for _ in $(seq 1 ${retries}); do
    if curl -sf "http://localhost:${AGENT_PORT}/health" >/dev/null; then
      return 0
    fi
    sleep "${delay}"
  done
  return 1
}

start_agent() {
  log "Building agent image (${AGENT_IMAGE})..."
  docker build -t "${AGENT_IMAGE}" "${REPO_ROOT}/agent"

  log "Launching local agent container (ports ${AGENT_PORT} and ${IPERF_PORT})..."
  docker rm -f iperf-agent >/dev/null 2>&1 || true
  docker run -d --name iperf-agent \
    --restart=always \
    -p "${AGENT_PORT}:8000" \
    -p "${IPERF_PORT}:${IPERF_PORT}/tcp" \
    -p "${IPERF_PORT}:${IPERF_PORT}/udp" \
    "${AGENT_IMAGE}"

  if [ "${START_IPERF_SERVER}" = true ]; then
    if wait_for_local_agent; then
      log "Starting iperf3 server inside local agent..."
      curl -sSf -X POST "http://localhost:${AGENT_PORT}/start_server" \
        -H "Content-Type: application/json" \
        -d "{\"port\": ${IPERF_PORT}}" >/dev/null || log "Failed to start iperf3 server via agent API."
    else
      log "Local agent did not become ready in time; skipping iperf3 server start."
    fi
  else
    log "Skipping iperf3 server auto-start (requested)."
  fi
}

start_master_stack() {
  log "Building master-api service..."
  MASTER_API_PORT="${MASTER_API_PORT}" MASTER_WEB_PORT="${MASTER_WEB_PORT}" ${COMPOSE_CMD} -f "${REPO_ROOT}/docker-compose.yml" build master-api

  log "Starting master-api (and dependencies) via docker compose..."
  MASTER_API_PORT="${MASTER_API_PORT}" MASTER_WEB_PORT="${MASTER_WEB_PORT}" ${COMPOSE_CMD} -f "${REPO_ROOT}/docker-compose.yml" up -d db master-api
}

deploy_remote_agents() {
  if [ "${DEPLOY_REMOTE}" != true ]; then
    return
  fi

  if [ -z "${HOSTS_FILE}" ]; then
    log "No hosts file provided; skipping remote agent deployment."
    log "Run deploy_agents.sh manually with --hosts-file <path> or set HOSTS_FILE to enable automation."
    return
  fi

  if [ ! -f "${HOSTS_FILE}" ] || [ ! -s "${HOSTS_FILE}" ]; then
    log "Remote hosts file not found or empty at ${HOSTS_FILE}; skipping remote agent deployment."
    return
  fi

  log "Deploying remote agents using ${HOSTS_FILE}..."
  HOSTS_FILE="${HOSTS_FILE}" AGENT_PORT="${AGENT_PORT}" IPERF_PORT="${IPERF_PORT}" IMAGE_NAME="${AGENT_IMAGE}" "${REPO_ROOT}/deploy_agents.sh"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --config)
        PORT_CONFIG_FILE="$2"; shift 2 ;;
      --config=*)
        PORT_CONFIG_FILE="${1#--config=}"; shift 1 ;;
      --deploy-remote)
        DEPLOY_REMOTE=true; shift ;;
      --agent-port)
        AGENT_PORT="$2"; shift 2 ;;
      --iperf-port)
        IPERF_PORT="$2"; shift 2 ;;
      --master-port|--master-api-port)
        MASTER_API_PORT="$2"; shift 2 ;;
      --web-port|--dashboard-port)
        MASTER_WEB_PORT="$2"; shift 2 ;;
      --no-start-server)
        START_IPERF_SERVER=false; shift ;;
      --hosts|--hosts-file)
        HOSTS_FILE="$2"; shift 2 ;;
      --clean-existing)
        CLEAN_EXISTING=true; shift ;;
      --repo-ref)
        REPO_REF="$2"; shift 2 ;;
      --repo-url)
        REPO_URL="$2"; shift 2 ;;
      -h|--help)
        cat <<'USAGE'
  Usage: install_master.sh [options]
    --config <path>          Source port variables from a file before applying flags
    --deploy-remote          Deploy agents to hosts listed in an inventory file
    --agent-port <port>      Agent API port to expose (default: 8000)
    --iperf-port <port>      iperf3 TCP/UDP port to expose (default: 5201)
    --master-port <port>     Master API host port (default: 9000)
    --master-api-port <port> Same as --master-port for convenience
    --web-port <port>        Dashboard host port (default: 9100)
    --dashboard-port <port>  Same as --web-port for convenience
  --no-start-server        Skip auto-starting iperf3 server inside the agent
  --hosts-file <path>      Inventory file for remote agent deployment
  --clean-existing         Stop and remove existing master/api/db + local agent containers before install
  --repo-url <url>         Git remote URL used for auto-update
  --repo-ref <ref>         Git ref to check out before installing
USAGE
        exit 0 ;;
      *)
        echo "Unknown option: $1" >&2
        exit 1 ;;
    esac
  done
}

main() {
  preprocess_config_arg "$@"
  maybe_load_port_config
  parse_args "$@"
  detect_repo_root
  REPO_URL=$(normalize_repo_url "${REPO_URL:-${DEFAULT_REPO_URL:-https://github.com/podcctv/iperf3-test-tools.git}}")
  download_repo_if_missing
  update_repo
  rerun_if_repo_updated "$@"
  ensure_docker
  ensure_compose
  ensure_compose_file
  cleanup_existing_services
  ensure_ports_available
  start_master_stack
  start_agent
  deploy_remote_agents

  local public_ip host_display
  public_ip=$(detect_public_ip)
  host_display=${public_ip:-localhost}

  log "Bootstrap complete."
  if [ -z "${public_ip}" ]; then
    log "  Public IP not detected; falling back to localhost for display."
  fi
  log "  Master API on http://${host_display}:${MASTER_API_PORT}."
  log "  Dashboard UI on http://${host_display}:${MASTER_WEB_PORT}/web."
  log "  Local agent on http://${host_display}:${AGENT_PORT}."
}

main "$@"
