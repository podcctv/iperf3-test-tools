#!/usr/bin/env bash
# Helper installer to set up an agent-only deployment on a remote host.
set -euo pipefail

AGENT_IMAGE=${AGENT_IMAGE:-"iperf-agent:latest"}
AGENT_PORT=${AGENT_PORT:-8000}
IPERF_PORT=${IPERF_PORT:-5201}
START_IPERF_SERVER=${START_IPERF_SERVER:-true}
REPO_REF=${REPO_REF:-""}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}"
DEFAULT_REPO_URL="$(command -v git >/dev/null 2>&1 && git -C "${REPO_ROOT}" remote get-url origin 2>/dev/null || true)"
REPO_URL=${REPO_URL:-"${DEFAULT_REPO_URL:-https://github.com/podcctv/iperf3-test-tools.git}"}

log() { printf "[install-agent] %s\n" "$*"; }

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

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --agent-port)
        AGENT_PORT="$2"; shift 2 ;;
      --iperf-port)
        IPERF_PORT="$2"; shift 2 ;;
      --no-start-server)
        START_IPERF_SERVER=false; shift ;;
      --repo-ref)
        REPO_REF="$2"; shift 2 ;;
      --repo-url)
        REPO_URL="$2"; shift 2 ;;
      -h|--help)
        cat <<'USAGE'
Usage: install_agent.sh [options]
  --agent-port <port>     Agent API port to expose (default: 8000)
  --iperf-port <port>     iperf3 TCP/UDP port to expose (default: 5201)
  --no-start-server       Skip auto-starting iperf3 server inside the agent
  --repo-url <url>        Git remote URL used for auto-update
  --repo-ref <ref>        Git ref to check out before installing
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
  start_agent

  local agent_host
  agent_host=$(hostname -I 2>/dev/null | awk '{print $1}')
  if [ -z "${agent_host}" ]; then
    agent_host="$(hostname -f 2>/dev/null || hostname)"
  fi

  cat <<INFO
Agent installation complete.
Add this agent to the master dashboard with:
  Agent URL: http://${agent_host}:${AGENT_PORT}
  iperf3 port: ${IPERF_PORT}
INFO
}

main "$@"
