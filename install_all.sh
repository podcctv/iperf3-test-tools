#!/usr/bin/env bash
# One-click bootstrap for master API, local agent, and optional remote agent deployment.
set -euo pipefail

# Configuration (override via environment or CLI flags)
AGENT_IMAGE=${AGENT_IMAGE:-"iperf-agent:latest"}
AGENT_PORT=${AGENT_PORT:-8000}
IPERF_PORT=${IPERF_PORT:-5201}
MASTER_API_PORT=${MASTER_API_PORT:-9000}
HOSTS_FILE=${HOSTS_FILE:-"hosts.txt"}
START_IPERF_SERVER=${START_IPERF_SERVER:-true}
DEPLOY_REMOTE=${DEPLOY_REMOTE:-true}
START_LOCAL_AGENT=${START_LOCAL_AGENT:-true}
DOWNLOAD_LATEST=${DOWNLOAD_LATEST:-false}
REPO_URL=${REPO_URL:-""}
REPO_REF=${REPO_REF:-"main"}
REPO_DEST=${REPO_DEST:-""}
INSTALL_TARGET=${INSTALL_TARGET:-""}
INSTALL_MASTER=${INSTALL_MASTER:-false}
INSTALL_AGENT=${INSTALL_AGENT:-false}

ORIGINAL_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${ORIGINAL_SCRIPT_DIR}"
COMPOSE_CMD=""

usage() {
  cat <<USAGE
Usage: $(basename "$0") [options]

Bootstrap master API, local agent (with iperf3 server), and optionally deploy remote agents.

Options:
  --hosts <path>           Path to hosts inventory for remote deployment (default: hosts.txt)
  --agent-image <name>     Docker image tag for the agent (default: iperf-agent:latest)
  --agent-port <port>      Port to expose the agent API on the host (default: 8000)
  --master-port <port>     Port to expose the master API on the host (default: 9000)
  --iperf-port <port>      Port to expose iperf3 server TCP/UDP (default: 5201)
  --install-target <name>  Which components to install: master, agent, or all (default: prompt)
  --download-latest        Download the latest repository before running (requires --repo-url)
  --repo-url <url>         Repository URL to clone (e.g., https://github.com/org/iperf3-test-tools.git)
  --repo-ref <ref>         Branch, tag, or commit to fetch when downloading (default: main)
  --repo-dest <path>       Destination directory for downloaded repo (default: temporary dir)
  --no-local-agent         Skip launching a local agent container
  --no-remote              Skip deploying remote agents via hosts file
  --no-start-server        Do not auto-start iperf3 server on the local agent
  -h, --help               Show this help message
USAGE
}

log() { printf "[install-all] %s\n" "$*"; }

download_repo() {
  if [ -z "${REPO_URL}" ]; then
    log "--download-latest requires --repo-url (or REPO_URL env var) to be set." >&2
    exit 1
  fi

  local target_dir="${REPO_DEST}"
  if [ -z "${target_dir}" ]; then
    target_dir="$(mktemp -d)/iperf3-test-tools"
  fi

  log "Downloading repository from ${REPO_URL} (ref: ${REPO_REF}) to ${target_dir}..."
  rm -rf "${target_dir}"

  if command -v git >/dev/null 2>&1; then
    git clone --depth 1 --branch "${REPO_REF}" "${REPO_URL}" "${target_dir}"
  else
    local archive_url
    archive_url="${REPO_URL%.git}/archive/${REPO_REF}.tar.gz"
    mkdir -p "${target_dir}"
    curl -fsSL "${archive_url}" | tar -xz --strip-components=1 -C "${target_dir}"
  fi

  REPO_ROOT="${target_dir}"
  log "Repository ready at ${REPO_ROOT}."
}

maybe_download_repo() {
  if [ "${DOWNLOAD_LATEST}" != true ]; then
    return
  fi

  download_repo
}

is_repo_complete() {
  [ -d "${REPO_ROOT}/agent" ] && [ -d "${REPO_ROOT}/master-api" ] && [ -f "${REPO_ROOT}/docker-compose.yml" ]
}

update_repo_from_git() {
  if [ -z "${REPO_URL}" ] || [ ! -d "${REPO_ROOT}/.git" ]; then
    return 1
  fi

  if ! command -v git >/dev/null 2>&1; then
    log "git is required to update the existing repository automatically."
    return 1
  fi

  log "Updating existing repository at ${REPO_ROOT} from ${REPO_URL} (ref: ${REPO_REF})..."
  git -C "${REPO_ROOT}" remote set-url origin "${REPO_URL}" || true
  git -C "${REPO_ROOT}" fetch origin
  git -C "${REPO_ROOT}" checkout "${REPO_REF}"
  git -C "${REPO_ROOT}" pull --ff-only origin "${REPO_REF}"
}

ensure_repo_ready() {
  local needs_refresh=false

  if ! is_repo_complete; then
    log "Required project files missing at ${REPO_ROOT}."
    needs_refresh=true
  fi

  if [ "${needs_refresh}" = false ] && [ -n "${REPO_URL}" ] && [ -d "${REPO_ROOT}/.git" ]; then
    local current_ref origin_url
    current_ref=$(git -C "${REPO_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null || git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo "")
    origin_url=$(git -C "${REPO_ROOT}" remote get-url origin 2>/dev/null || echo "")

    if [ -n "${origin_url}" ] && [ "${origin_url}" != "${REPO_URL}" ]; then
      log "Local repository origin (${origin_url}) differs from requested URL (${REPO_URL})."
      needs_refresh=true
    elif [ -n "${REPO_REF}" ] && [ -n "${current_ref}" ] && [ "${current_ref}" != "${REPO_REF}" ]; then
      log "Local repository ref (${current_ref}) differs from requested ref (${REPO_REF})."
      needs_refresh=true
    fi
  fi

  if [ "${needs_refresh}" = true ]; then
    if ! update_repo_from_git; then
      if [ -z "${REPO_URL}" ]; then
        log "Set REPO_URL (and optionally REPO_REF) to allow automatic download/update of the project." >&2
        exit 1
      fi

      download_repo
    else
      log "Repository updated at ${REPO_ROOT}."
    fi
    resolve_paths
  else
    log "Using existing repository at ${REPO_ROOT}."
  fi
}

ensure_docker() {
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

build_images() {
  if [ "${INSTALL_AGENT}" = true ]; then
    log "Building agent image (${AGENT_IMAGE})..."
    docker build -t "${AGENT_IMAGE}" "${REPO_ROOT}/agent"
  fi

  if [ "${INSTALL_MASTER}" = true ]; then
    log "Building master-api service..."
    MASTER_API_PORT="${MASTER_API_PORT}" ${COMPOSE_CMD} -f "${REPO_ROOT}/docker-compose.yml" build master-api
  fi
}

start_master() {
  if [ "${INSTALL_MASTER}" != true ]; then
    log "Master installation skipped by target selection."
    return
  fi

  log "Starting master-api (and dependencies) via docker compose..."
  MASTER_API_PORT="${MASTER_API_PORT}" ${COMPOSE_CMD} -f "${REPO_ROOT}/docker-compose.yml" up -d db master-api
}

start_local_agent() {
  if [ "${INSTALL_AGENT}" != true ]; then
    log "Local agent installation skipped by target selection."
    return
  fi

  log "Launching local agent container (ports ${AGENT_PORT} and ${IPERF_PORT})..."
  docker rm -f iperf-agent >/dev/null 2>&1 || true
  docker run -d --name iperf-agent \
    --restart=always \
    -p "${AGENT_PORT}:8000" \
    -p "${IPERF_PORT}:${IPERF_PORT}/tcp" \
    -p "${IPERF_PORT}:${IPERF_PORT}/udp" \
    "${AGENT_IMAGE}"

  if [ "${START_IPERF_SERVER}" = true ]; then
    log "Starting iperf3 server inside local agent..."
    curl -sSf -X POST "http://localhost:${AGENT_PORT}/start_server" \
      -H "Content-Type: application/json" \
      -d "{\"port\": ${IPERF_PORT}}" >/dev/null
  else
    log "Skipping iperf3 server auto-start (requested)."
  fi
}

deploy_remote_agents() {
  if [ "${INSTALL_AGENT}" != true ]; then
    log "Remote deployment skipped because agent installation was not selected."
    return
  fi

  if [ "${DEPLOY_REMOTE}" != true ]; then
    log "Remote deployment skipped by flag."
    return
  fi

  if [ ! -f "${HOSTS_FILE}" ] || [ ! -s "${HOSTS_FILE}" ]; then
    log "No remote hosts file found at ${HOSTS_FILE}; skipping remote agent deployment."
    return
  fi

  log "Deploying remote agents using ${HOSTS_FILE}..."
  HOSTS_FILE="${HOSTS_FILE}" AGENT_PORT="${AGENT_PORT}" IPERF_PORT="${IPERF_PORT}" IMAGE_NAME="${AGENT_IMAGE}" "${REPO_ROOT}/deploy_agents.sh"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --hosts)
        HOSTS_FILE=$2; shift 2 ;;
      --agent-image)
        AGENT_IMAGE=$2; shift 2 ;;
      --agent-port)
        AGENT_PORT=$2; shift 2 ;;
      --master-port)
        MASTER_API_PORT=$2; shift 2 ;;
      --iperf-port)
        IPERF_PORT=$2; shift 2 ;;
      --download-latest)
        DOWNLOAD_LATEST=true; shift ;;
      --repo-url)
        REPO_URL=$2; shift 2 ;;
      --repo-ref)
        REPO_REF=$2; shift 2 ;;
      --repo-dest)
        REPO_DEST=$2; shift 2 ;;
      --install-target)
        INSTALL_TARGET=$2; shift 2 ;;
      --no-local-agent)
        START_LOCAL_AGENT=false; shift ;;
      --no-remote)
        DEPLOY_REMOTE=false; shift ;;
      --no-start-server)
        START_IPERF_SERVER=false; shift ;;
      -h|--help)
        usage; exit 0 ;;
      *)
        echo "Unknown option: $1" >&2
        usage; exit 1 ;;
    esac
  done
}

resolve_paths() {
  if [[ "${HOSTS_FILE}" != /* ]]; then
    HOSTS_FILE="${REPO_ROOT}/${HOSTS_FILE}"
  fi
}

prompt_install_target() {
  if [ -n "${INSTALL_TARGET}" ] || [ ! -t 0 ]; then
    return
  fi

  cat <<'PROMPT'
Select installation target:
  1) master (master API only)
  2) agent (local/remote agents only)
  3) all (master API and agents)
PROMPT
  read -rp "Enter choice [1-3]: " choice
  case "$choice" in
    1) INSTALL_TARGET="master" ;;
    2) INSTALL_TARGET="agent" ;;
    3) INSTALL_TARGET="all" ;;
    *)
      log "Invalid selection. Please choose 1, 2, or 3."
      prompt_install_target
      ;;
  esac
}

prompt_ports() {
  local input

  if [ -t 0 ]; then
    read -rp "Master API port [${MASTER_API_PORT}]: " input || true
    if [ -n "$input" ]; then
      MASTER_API_PORT="$input"
    fi

    read -rp "Agent API port [${AGENT_PORT}]: " input || true
    if [ -n "$input" ]; then
      AGENT_PORT="$input"
    fi
  fi
}

set_install_flags() {
  case "${INSTALL_TARGET}" in
    master)
      INSTALL_MASTER=true
      INSTALL_AGENT=false
      DEPLOY_REMOTE=false
      START_LOCAL_AGENT=false
      ;;
    agent)
      INSTALL_MASTER=false
      INSTALL_AGENT=true
      ;;
    all|"")
      INSTALL_MASTER=true
      INSTALL_AGENT=true
      ;;
    *)
      log "Unknown installation target: ${INSTALL_TARGET}. Use master, agent, or all."
      exit 1
      ;;
  esac
}

main() {
  parse_args "$@"
  prompt_install_target
  prompt_ports
  set_install_flags
  maybe_download_repo
  ensure_repo_ready
  resolve_paths
  ensure_docker
  ensure_compose
  build_images

  if [ "${INSTALL_MASTER}" = true ]; then
    start_master
  fi

  if [ "${INSTALL_AGENT}" = true ]; then
    if [ "${START_LOCAL_AGENT}" = true ]; then
      start_local_agent
    else
      log "Local agent launch skipped by flag."
    fi

    deploy_remote_agents
  fi

  log "Bootstrap complete."

  if [ "${INSTALL_MASTER}" = true ]; then
    log "  Master API on http://localhost:${MASTER_API_PORT}."
  fi

  if [ "${INSTALL_AGENT}" = true ]; then
    log "  Local agent on http://localhost:${AGENT_PORT}."
  fi
}

main "$@"
