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
HOSTS_FILE=${HOSTS_FILE:-"hosts.txt"}
REPO_REF=${REPO_REF:-""}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}"
DEFAULT_REPO_URL="$(command -v git >/dev/null 2>&1 && git -C "${REPO_ROOT}" remote get-url origin 2>/dev/null || true)"
REPO_URL=${REPO_URL:-"${DEFAULT_REPO_URL:-https://github.com/podcctv/iperf3-test-tools.git}"}
COMPOSE_CMD=""

log() { printf "[install-master] %s\n" "$*"; }

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
    commit_delta=$(git -C "${REPO_ROOT}" rev-list --count "${before}..${after}" 2>/dev/null || echo "?")
    log "Repository updated: ${commit_delta} new commit(s) applied (${before:0:7} -> ${after:0:7})."
  else
    log "Repository already up to date (${after:0:7})."
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
      --deploy-remote)
        DEPLOY_REMOTE=true; shift ;;
      --agent-port)
        AGENT_PORT="$2"; shift 2 ;;
      --iperf-port)
        IPERF_PORT="$2"; shift 2 ;;
      --master-port)
        MASTER_API_PORT="$2"; shift 2 ;;
      --web-port)
        MASTER_WEB_PORT="$2"; shift 2 ;;
      --no-start-server)
        START_IPERF_SERVER=false; shift ;;
      --hosts)
        HOSTS_FILE="$2"; shift 2 ;;
      --repo-ref)
        REPO_REF="$2"; shift 2 ;;
      --repo-url)
        REPO_URL="$2"; shift 2 ;;
      -h|--help)
        cat <<'USAGE'
Usage: install_master.sh [options]
  --deploy-remote          Deploy agents to hosts listed in hosts.txt
  --agent-port <port>      Agent API port to expose (default: 8000)
  --iperf-port <port>      iperf3 TCP/UDP port to expose (default: 5201)
  --master-port <port>     Master API host port (default: 9000)
  --web-port <port>        Dashboard host port (default: 9100)
  --no-start-server        Skip auto-starting iperf3 server inside the agent
  --hosts <path>           Inventory file for remote agent deployment (default: hosts.txt)
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
  parse_args "$@"
  update_repo
  ensure_docker
  ensure_compose
  start_master_stack
  start_agent
  deploy_remote_agents

  log "Bootstrap complete."
  log "  Master API on http://localhost:${MASTER_API_PORT}."
  log "  Dashboard UI on http://localhost:${MASTER_WEB_PORT}/web."
  log "  Local agent on http://localhost:${AGENT_PORT}."
}

main "$@"
