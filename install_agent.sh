#!/usr/bin/env bash
# Helper installer to set up an agent-only deployment on a remote host.
set -euo pipefail

AGENT_IMAGE=${AGENT_IMAGE:-"iperf-agent:latest"}
AGENT_PORT=${AGENT_PORT:-8000}
AGENT_LISTEN_PORT=${AGENT_LISTEN_PORT:-8000}
IPERF_PORT=${IPERF_PORT:-}
START_IPERF_SERVER=${START_IPERF_SERVER:-true}
UNINSTALL=${UNINSTALL:-false}
REPO_REF=${REPO_REF:-""}
PORT_CONFIG_FILE=${PORT_CONFIG_FILE:-""}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}"
CACHE_ROOT="${XDG_CACHE_HOME:-$HOME/.cache}/iperf3-test-tools"
DEFAULT_REPO_URL="$(command -v git >/dev/null 2>&1 && git -C "${REPO_ROOT}" remote get-url origin 2>/dev/null || true)"
REPO_URL=${REPO_URL:-"${DEFAULT_REPO_URL:-https://github.com/podcctv/iperf3-test-tools.git}"}
REPO_UPDATED=false

log() { printf "[install-agent] %s\n" "$*"; }

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

detect_region() {
  # Detect if server is in China based on IP geolocation
  # Returns "cn" for China, "global" otherwise
  local ip country
  ip=$(detect_public_ip)
  
  if [ -z "${ip}" ]; then
    echo "global"
    return
  fi
  
  # Try multiple geolocation services
  if command -v curl >/dev/null 2>&1; then
    # Try ip-api.com (free, no key needed)
    country=$(curl -fsS --connect-timeout 5 "http://ip-api.com/line/${ip}?fields=countryCode" 2>/dev/null || true)
    
    if [ -z "${country}" ]; then
      # Fallback: try ipinfo.io
      country=$(curl -fsS --connect-timeout 5 "https://ipinfo.io/${ip}/country" 2>/dev/null || true)
    fi
  fi
  
  # Check if country is China
  case "${country}" in
    CN|cn|China|china)
      echo "cn"
      ;;
    *)
      echo "global"
      ;;
  esac
}

setup_pip_mirror() {
  # Set up pip mirror based on detected region
  local region
  region=$(detect_region)
  
  if [ "${region}" = "cn" ]; then
    log "Detected China region - using Aliyun pip mirror"
    PIP_INDEX_URL="https://mirrors.aliyun.com/pypi/simple/"
    PIP_TRUSTED_HOST="mirrors.aliyun.com"
  else
    log "Detected non-China region - using default PyPI"
    PIP_INDEX_URL="https://pypi.org/simple/"
    PIP_TRUSTED_HOST=""
  fi
  
  export PIP_INDEX_URL PIP_TRUSTED_HOST
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

select_python() {
  if command -v python3 >/dev/null 2>&1; then
    echo "python3"
  elif command -v python >/dev/null 2>&1; then
    echo "python"
  else
    echo ""
  fi
}

choose_random_port() {
  local py
  py=$(select_python)

  if [ -z "${py}" ]; then
    echo ""
    return
  fi

  "${py}" <<'PY'
import random
import socket

def find_port():
    for _ in range(50):
        port = random.randint(20000, 64000)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("", port))
            except OSError:
                continue
            return port

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


print(find_port())
PY
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

        print(f"[install-agent] Port probe on {address}:{port} skipped: {exc}", file=sys.stderr)
    return False


if can_bind(socket.AF_INET, "0.0.0.0"):
    sys.exit(1)

try:
    if can_bind(socket.AF_INET6, "::"):
        sys.exit(1)
except OSError:
    pass

sys.exit(0)
PY
}

ensure_random_iperf_port() {
  if [ -n "${IPERF_PORT}" ]; then
    return
  fi

  local random_port
  random_port=$(choose_random_port)

  if [ -n "${random_port}" ]; then
    IPERF_PORT="${random_port}"
    log "Selected random iperf3 port ${IPERF_PORT}."
  else
    IPERF_PORT=62001
    log "Could not determine random iperf3 port; falling back to ${IPERF_PORT}."
  fi
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
        if port_in_use "${port_value}"; then
          status=0
          log "Using ${label} port ${port_value}."
        else
          status=$?
        fi
      done
    else
      log "${label} port ${port_value} is already in use. Re-run with a different port via flags or environment variables."
      exit 1
    fi
  done
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

  if [ "${INSTALL_AGENT_UPDATED:-0}" -eq 1 ]; then
    return
  fi

  if [ ! -x "${REPO_ROOT}/install_agent.sh" ]; then
    log "Updated repository detected but installer not found at ${REPO_ROOT}; continuing without restart."
    return
  fi

  log "Repository updated; re-running installer with latest version..."
  INSTALL_AGENT_UPDATED=1 exec "${REPO_ROOT}/install_agent.sh" "$@"
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

  log "Building agent image (${AGENT_IMAGE}) with pip mirror: ${PIP_INDEX_URL}..."
  local build_args=""
  if [ -n "${PIP_INDEX_URL:-}" ]; then
    build_args="${build_args} --build-arg PIP_INDEX_URL=${PIP_INDEX_URL}"
  fi
  if [ -n "${PIP_TRUSTED_HOST:-}" ]; then
    build_args="${build_args} --build-arg PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST}"
  fi
  docker build ${build_args} -t "${AGENT_IMAGE}" "${REPO_ROOT}/agent"

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

uninstall_agent() {
  ensure_docker
  log "Removing iperf-agent container and image..."
  docker rm -f iperf-agent >/dev/null 2>&1 || true
  docker rmi -f "${AGENT_IMAGE}" >/dev/null 2>&1 || true
  log "Uninstall complete."
  exit 0
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --config)
        PORT_CONFIG_FILE="$2"; shift 2 ;;
      --config=*)
        PORT_CONFIG_FILE="${1#--config=}"; shift 1 ;;
      --agent-port)
        AGENT_PORT="$2"; shift 2 ;;
      --agent-listen-port)
        AGENT_LISTEN_PORT="$2"; shift 2 ;;
      --iperf-port)
        IPERF_PORT="$2"; shift 2 ;;
      --no-start-server)
        START_IPERF_SERVER=false; shift ;;
      --uninstall)
        UNINSTALL=true; shift ;;
      --repo-ref)
        REPO_REF="$2"; shift 2 ;;
      --repo-url)
        REPO_URL="$2"; shift 2 ;;
      -h|--help)
        cat <<'USAGE'
Usage: install_agent.sh [options]
  --config <path>            Source port variables from a file before applying flags
  --agent-port <port>         Host port where the agent API is exposed (default: 8000)
  --agent-listen-port <port>  Container port the agent listens on (default: 8000)
  --iperf-port <port>         iperf3 TCP/UDP port to expose (default: random available port)
  --no-start-server           Skip auto-starting iperf3 server inside the agent
  --uninstall                 Remove iperf-agent container and image
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
  preprocess_config_arg "$@"
  maybe_load_port_config
  parse_args "$@"
  ensure_random_iperf_port
  if [ "${UNINSTALL}" = true ]; then
    uninstall_agent
  fi
  ensure_repo_available
  update_repo
  rerun_if_repo_updated "$@"
  ensure_ports_available
  setup_pip_mirror
  ensure_docker
  start_agent

  local agent_host public_ip
  public_ip=$(detect_public_ip)
  if [ -n "${public_ip}" ]; then
    agent_host="${public_ip}"
  else
    agent_host=$(hostname -I 2>/dev/null | awk '{print $1}')
    if [ -z "${agent_host}" ]; then
      agent_host="$(hostname -f 2>/dev/null || hostname)"
    fi
    log "Public IP not detected; showing local host identifier instead."
  fi

  cat <<INFO
Agent installation complete.
Add this agent to the master dashboard with:
  Agent URL: http://${agent_host}:${AGENT_PORT}
  iperf3 port: ${IPERF_PORT}
INFO
}

main "$@"
