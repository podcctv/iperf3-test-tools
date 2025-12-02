#!/usr/bin/env bash
# One-click bootstrap for master API, local agent, and optional remote agent deployment.
set -euo pipefail

# Configuration (override via environment or CLI flags)
AGENT_IMAGE=${AGENT_IMAGE:-"iperf-agent:latest"}
AGENT_PORT=${AGENT_PORT:-8000}
IPERF_PORT=${IPERF_PORT:-5201}
HOSTS_FILE=${HOSTS_FILE:-"hosts.txt"}
START_IPERF_SERVER=${START_IPERF_SERVER:-true}
DEPLOY_REMOTE=${DEPLOY_REMOTE:-true}
START_LOCAL_AGENT=${START_LOCAL_AGENT:-true}
DOWNLOAD_LATEST=${DOWNLOAD_LATEST:-false}
REPO_URL=${REPO_URL:-""}
REPO_REF=${REPO_REF:-"main"}
REPO_DEST=${REPO_DEST:-""}

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
  --iperf-port <port>      Port to expose iperf3 server TCP/UDP (default: 5201)
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
  if [ ! -d "${REPO_ROOT}/agent" ]; then
    if [ "${DOWNLOAD_LATEST}" = true ] || [ -n "${REPO_URL}" ]; then
      log "Agent directory missing at ${REPO_ROOT}/agent; attempting repository download..."
      download_repo
      resolve_paths
    else
      log "Agent directory missing at ${REPO_ROOT}/agent. Run from a full checkout or provide --download-latest with --repo-url."
      exit 1
    fi
  fi

  log "Building agent image (${AGENT_IMAGE})..."
  docker build -t "${AGENT_IMAGE}" "${REPO_ROOT}/agent"

  log "Building master-api service..."
  ${COMPOSE_CMD} -f "${REPO_ROOT}/docker-compose.yml" build master-api
}

start_master() {
  log "Starting master-api (and dependencies) via docker compose..."
  ${COMPOSE_CMD} -f "${REPO_ROOT}/docker-compose.yml" up -d db master-api
}

start_local_agent() {
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

main() {
  parse_args "$@"
  maybe_download_repo
  resolve_paths
  ensure_docker
  ensure_compose
  build_images
  start_master

  if [ "${START_LOCAL_AGENT}" = true ]; then
    start_local_agent
  else
    log "Local agent launch skipped by flag."
  fi

  deploy_remote_agents

  log "Bootstrap complete. Master API on http://localhost:9000; local agent on http://localhost:${AGENT_PORT}."
}

main "$@"
