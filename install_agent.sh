#!/usr/bin/env bash
# Helper installer to set up an agent-only deployment on a remote host.
set -euo pipefail

AGENT_IMAGE=${AGENT_IMAGE:-"iperf-agent:latest"}
AGENT_PORT=${AGENT_PORT:-8000}
AGENT_LISTEN_PORT=${AGENT_LISTEN_PORT:-8000}
IPERF_PORT=${IPERF_PORT:-5201}
START_IPERF_SERVER=${START_IPERF_SERVER:-true}
REPO_REF=${REPO_REF:-""}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}"
CACHE_ROOT="${XDG_CACHE_HOME:-$HOME/.cache}/iperf3-test-tools"
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

fetch_repo_tarball() {
  local ref tar_url temp_dir
  ref="${REPO_REF:-main}"
  tar_url="${REPO_URL%.git}/archive/refs/heads/${ref}.tar.gz"
  temp_dir="$1"

  if command -v curl >/dev/null 2>&1; then
    downloader=(curl -fsSL "${tar_url}")
  elif command -v wget >/dev/null 2>&1; then
    downloader=(wget -qO- "${tar_url}")
  else
    log "Neither curl nor wget is available to fetch repository tarball."
    return 1
  fi

  if ! "${downloader[@]}" | tar -xz -C "${temp_dir}" --strip-components=1; then
    log "Failed to download repository tarball from ${tar_url}."
    return 1
  fi
}

ensure_download_prereqs() {
  if command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1; then
    return
  fi

  if command -v apt-get >/dev/null 2>&1; then
    log "Installing curl (download prerequisite)..."
    apt-get update -y >/dev/null 2>&1 && apt-get install -y curl >/dev/null 2>&1 && return
  fi

  log "Unable to install download prerequisites automatically (curl/wget missing)."
  return 1
}

ensure_repo_available() {
  local initial_repo_root="${REPO_ROOT}" temp_repo

  if [ -d "${REPO_ROOT}/agent" ]; then
    log "Using existing repository at ${REPO_ROOT}."
  else
    mkdir -p "${CACHE_ROOT}"
    temp_repo="${CACHE_ROOT}/agent-build"

    log "Local agent directory not found; fetching repository into ${temp_repo}..."
    rm -rf "${temp_repo}"

    if command -v git >/dev/null 2>&1; then
      if git clone --depth 1 "${REPO_URL}" "${temp_repo}" >/dev/null 2>&1; then
        if [ -n "${REPO_REF}" ]; then
          git -C "${temp_repo}" checkout "${REPO_REF}" >/dev/null 2>&1 || log "Checkout ${REPO_REF} failed; using default branch."
        fi
        REPO_ROOT="${temp_repo}"
      else
        log "Git clone failed; attempting tarball download..."
      fi
    fi

    if [ "${REPO_ROOT}" = "${initial_repo_root}" ] || [ ! -d "${REPO_ROOT}/agent" ]; then
      ensure_download_prereqs || true
      mkdir -p "${temp_repo}"
      if fetch_repo_tarball "${temp_repo}"; then
        REPO_ROOT="${temp_repo}"
      fi
    fi
  fi

  if [ ! -d "${REPO_ROOT}/agent" ]; then
    log "Unable to obtain repository; aborting."
    exit 1
  fi
}

ensure_docker() {
  if command -v docker >/dev/null 2>&1; then
    return
  fi

  ensure_download_prereqs || true
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
  if [ ! -d "${REPO_ROOT}/agent" ]; then
    log "Agent directory missing at ${REPO_ROOT}/agent; download may have failed."
    exit 1
  fi

  log "Building agent image (${AGENT_IMAGE})..."
  docker build -t "${AGENT_IMAGE}" "${REPO_ROOT}/agent"

  log "Launching local agent container (host port ${AGENT_PORT} -> container port ${AGENT_LISTEN_PORT}; iperf port ${IPERF_PORT})..."
  docker rm -f iperf-agent >/dev/null 2>&1 || true
  docker run -d --name iperf-agent \
    --restart=always \
    -p "${AGENT_PORT}:${AGENT_LISTEN_PORT}" \
    -p "${IPERF_PORT}:${IPERF_PORT}/tcp" \
    -p "${IPERF_PORT}:${IPERF_PORT}/udp" \
    -e "AGENT_API_PORT=${AGENT_LISTEN_PORT}" \
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
      --agent-listen-port)
        AGENT_LISTEN_PORT="$2"; shift 2 ;;
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
  --agent-port <port>         Host port where the agent API is exposed (default: 8000)
  --agent-listen-port <port>  Container port the agent listens on (default: 8000)
  --iperf-port <port>         iperf3 TCP/UDP port to expose (default: 5201)
  --no-start-server           Skip auto-starting iperf3 server inside the agent
  --repo-url <url>            Git remote URL used for auto-update
  --repo-ref <ref>            Git ref to check out before installing
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
  ensure_repo_available
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
