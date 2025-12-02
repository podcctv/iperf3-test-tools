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
PAYLOAD_REF=${PAYLOAD_REF:-"main"}
PAYLOAD_REPO=${PAYLOAD_REPO:-"podcctv/iperf3-test-tools"}
PAYLOAD_URL=${PAYLOAD_URL:-""}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}"
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
  --download-url <url>     Tar.gz URL for downloading project payload when files are missing
  --repo <owner/name>      GitHub repo slug for auto-downloads (default: podcctv/iperf3-test-tools)
  --ref <branch/tag>       Git ref/branch used for downloads (default: main)
  --no-local-agent         Skip launching a local agent container
  --no-remote              Skip deploying remote agents via hosts file
  --no-start-server        Do not auto-start iperf3 server on the local agent
  -h, --help               Show this help message
USAGE
}

log() { printf "[install-all] %s\n" "$*"; }

compute_download_url() {
  if [ -n "${PAYLOAD_URL}" ]; then
    echo "${PAYLOAD_URL}"
  else
    echo "https://codeload.github.com/${PAYLOAD_REPO}/tar.gz/${PAYLOAD_REF}"
  fi
}

ensure_payload() {
  local required missing=false
  for required in agent master-api docker-compose.yml deploy_agents.sh; do
    if [ ! -e "${PROJECT_ROOT}/${required}" ]; then
      missing=true
      break
    fi
  done

  if [ "${missing}" = false ]; then
    return
  fi

  local url tmp archive extracted_root
  url="$(compute_download_url)"
  tmp="$(mktemp -d)"
  archive="${tmp}/payload.tar.gz"

  log "Project files not found; downloading from ${url}..."
  curl -fsSL "${url}" -o "${archive}"

  tar -xzf "${archive}" -C "${tmp}"
  extracted_root="$(find "${tmp}" -maxdepth 1 -type d -name "*iperf3-test-tools*" | head -n 1)"

  if [ -z "${extracted_root}" ]; then
    log "Failed to locate extracted project directory from archive." >&2
    exit 1
  fi

  PROJECT_ROOT="${extracted_root}"
  log "Using downloaded payload at ${PROJECT_ROOT}."
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
  log "Building agent image (${AGENT_IMAGE})..."
  docker build -t "${AGENT_IMAGE}" "${PROJECT_ROOT}/agent"

  log "Building master-api service..."
  ${COMPOSE_CMD} -f "${PROJECT_ROOT}/docker-compose.yml" build master-api
}

start_master() {
  log "Starting master-api (and dependencies) via docker compose..."
  ${COMPOSE_CMD} -f "${PROJECT_ROOT}/docker-compose.yml" up -d db master-api
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
  HOSTS_FILE="${HOSTS_FILE}" AGENT_PORT="${AGENT_PORT}" IPERF_PORT="${IPERF_PORT}" IMAGE_NAME="${AGENT_IMAGE}" "${PROJECT_ROOT}/deploy_agents.sh"
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
      --download-url)
        PAYLOAD_URL=$2; shift 2 ;;
      --repo)
        PAYLOAD_REPO=$2; shift 2 ;;
      --ref)
        PAYLOAD_REF=$2; shift 2 ;;
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

main() {
  parse_args "$@"
  ensure_payload
  if [[ "${HOSTS_FILE}" != /* ]]; then
    HOSTS_FILE="${PROJECT_ROOT}/${HOSTS_FILE}"
  fi
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
