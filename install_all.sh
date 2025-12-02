#!/usr/bin/env bash
# One-click bootstrap for master API, local agent, and optional remote agent deployment.
set -euo pipefail

# Configuration (override via environment or CLI flags)
AGENT_IMAGE=${AGENT_IMAGE:-"iperf-agent:latest"}
AGENT_PORT=${AGENT_PORT:-8000}
IPERF_PORT=${IPERF_PORT:-5201}
MASTER_API_PORT=${MASTER_API_PORT:-9000}
MASTER_WEB_PORT=${MASTER_WEB_PORT:-9100}
HOSTS_FILE=${HOSTS_FILE:-"hosts.txt"}
START_IPERF_SERVER=${START_IPERF_SERVER:-true}
DEPLOY_REMOTE=${DEPLOY_REMOTE:-true}
START_LOCAL_AGENT=${START_LOCAL_AGENT:-true}
DOWNLOAD_LATEST=${DOWNLOAD_LATEST:-false}
UPDATE_HOSTS_TEMPLATE=${UPDATE_HOSTS_TEMPLATE:-false}
REPO_URL=${REPO_URL:-"https://github.com/podcctv/iperf3-test-tools/"}
REPO_REF=${REPO_REF:-"main"}
REPO_DEST=${REPO_DEST:-""}
INSTALL_TARGET=${INSTALL_TARGET:-""}
INSTALL_MASTER=${INSTALL_MASTER:-false}
INSTALL_AGENT=${INSTALL_AGENT:-false}
UNINSTALL=${UNINSTALL:-false}
AUTO_UPDATE_REPO=${AUTO_UPDATE_REPO:-true}
UPDATE_ONLY=${UPDATE_ONLY:-false}

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
  --web-port <port>        Port to expose the web dashboard on the host (default: 9100)
  --iperf-port <port>      Port to expose iperf3 server TCP/UDP (default: 5201)
  --install-target <name>  Which components to install: master, agent, or all (default: prompt)
  --download-latest        Download the latest repository before running (uses default repo URL if unset)
  --repo-url <url>         Repository URL to clone (default: https://github.com/podcctv/iperf3-test-tools/)
  --repo-ref <ref>         Branch, tag, or commit to fetch when downloading (default: main)
  --repo-dest <path>       Destination directory for downloaded repo (default: temporary dir)
  --no-local-agent         Skip launching a local agent container
  --no-remote              Skip deploying remote agents via hosts file
  --no-start-server        Do not auto-start iperf3 server on the local agent
  --update-hosts-template  Copy hosts.txt template locally if missing (no overwrite)
  --uninstall              Remove master API stack, agent container, and built images
  -h, --help               Show this help message
USAGE
}

log() { printf "[install-all] %s\n" "$*"; }

download_repo() {
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
  if [ "${DOWNLOAD_LATEST}" = true ]; then
    download_repo
    return
  fi

  if [ -n "${REPO_URL}" ]; then
    local origin_url=""
    if command -v git >/dev/null 2>&1 && [ -d "${REPO_ROOT}/.git" ]; then
      origin_url=$(git -C "${REPO_ROOT}" remote get-url origin 2>/dev/null || echo "")
    fi

    if [ ! -d "${REPO_ROOT}" ] || [ "${origin_url}" != "${REPO_URL}" ]; then
      log "Repository download requested implicitly via REPO_URL; fetching latest from ${REPO_URL}."
      download_repo
    fi
  fi
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

sync_hosts_template() {
  local source_hosts dest_hosts

  if [ "${UPDATE_HOSTS_TEMPLATE}" != true ]; then
    return
  fi

  source_hosts="${REPO_ROOT}/hosts.txt"
  dest_hosts="${ORIGINAL_SCRIPT_DIR}/hosts.txt"

  if [ ! -f "${source_hosts}" ]; then
    log "No hosts.txt found in repository at ${source_hosts}; skipping template sync."
    return
  fi

  if [ -f "${dest_hosts}" ]; then
    log "hosts.txt already exists at ${dest_hosts}; not overwriting."
    return
  fi

  log "Copying hosts.txt template to ${dest_hosts}..."
  cp "${source_hosts}" "${dest_hosts}"
}

ensure_repo_ready() {
  local needs_refresh=false

  if [ "${AUTO_UPDATE_REPO}" = true ] && update_repo_from_git; then
    log "Repository updated from ${REPO_URL}."
    resolve_paths
    sync_hosts_template
    return
  fi

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
    MASTER_API_PORT="${MASTER_API_PORT}" MASTER_WEB_PORT="${MASTER_WEB_PORT}" ${COMPOSE_CMD} -f "${REPO_ROOT}/docker-compose.yml" build master-api
  fi
}

start_master() {
  if [ "${INSTALL_MASTER}" != true ]; then
    log "Master installation skipped by target selection."
    return
  fi

  log "Starting master-api (and dependencies) via docker compose..."
  MASTER_API_PORT="${MASTER_API_PORT}" MASTER_WEB_PORT="${MASTER_WEB_PORT}" ${COMPOSE_CMD} -f "${REPO_ROOT}/docker-compose.yml" up -d db master-api
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
    if wait_for_local_agent; then
      log "Starting iperf3 server inside local agent..."
      if ! curl -sSf -X POST "http://localhost:${AGENT_PORT}/start_server" \
        -H "Content-Type: application/json" \
        -d "{\"port\": ${IPERF_PORT}}" >/dev/null; then
        log "Failed to start iperf3 server via agent API."
      fi
    else
      log "Local agent did not become ready in time; skipping iperf3 server start."
    fi
  else
    log "Skipping iperf3 server auto-start (requested)."
  fi
}

wait_for_local_agent() {
  local retries=30
  local delay=2

  for _ in $(seq 1 ${retries}); do
    if curl -sf "http://localhost:${AGENT_PORT}/health" >/dev/null; then
      return 0
    fi
    sleep "${delay}"
  done

  return 1
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

  parse_hosts_file

  if [ ${#HOST_ENTRIES[@]} -eq 0 ]; then
    log "hosts.txt is present but contains no usable host entries; skipping remote deployment."
    return
  fi

  select_hosts_for_deploy

  local hosts_file_to_use="${HOSTS_FILE}"

  if [ ${#SELECTED_HOSTS[@]} -gt 0 ] && [ ${#SELECTED_HOSTS[@]} -lt ${#HOST_ENTRIES[@]} ]; then
    hosts_file_to_use="$(mktemp)"
    printf "%s\n" "${SELECTED_HOSTS[@]}" > "${hosts_file_to_use}"
  fi

  log "Deploying remote agents using ${hosts_file_to_use}..."
  HOSTS_FILE="${hosts_file_to_use}" AGENT_PORT="${AGENT_PORT}" IPERF_PORT="${IPERF_PORT}" IMAGE_NAME="${AGENT_IMAGE}" "${REPO_ROOT}/deploy_agents.sh"
}

uninstall_project() {
  log "Uninstalling iperf3 test tools (containers, images, and volumes)..."

  if [ -f "${REPO_ROOT}/docker-compose.yml" ]; then
    log "Stopping master-api stack and removing volumes..."
    MASTER_API_PORT="${MASTER_API_PORT}" MASTER_WEB_PORT="${MASTER_WEB_PORT}" ${COMPOSE_CMD} -f "${REPO_ROOT}/docker-compose.yml" down -v || \
      log "Failed to shut down master-api stack (it may not be running)."
  else
    log "docker-compose.yml not found at ${REPO_ROOT}; skipping master-api removal."
  fi

  log "Removing local agent container if present..."
  if docker rm -f iperf-agent >/dev/null 2>&1; then
    log "Removed local agent container."
  else
    log "No local agent container found."
  fi

  log "Removing agent image (${AGENT_IMAGE}) if present..."
  if docker rmi "${AGENT_IMAGE}" >/dev/null 2>&1; then
    log "Removed agent image ${AGENT_IMAGE}."
  else
    log "Agent image ${AGENT_IMAGE} not found or in use; skipping removal."
  fi

  log "Uninstall complete."
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
      --web-port)
        MASTER_WEB_PORT=$2; shift 2 ;;
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
      --uninstall)
        UNINSTALL=true; shift ;;
      --no-local-agent)
        START_LOCAL_AGENT=false; shift ;;
      --no-remote)
        DEPLOY_REMOTE=false; shift ;;
      --no-start-server)
        START_IPERF_SERVER=false; shift ;;
      --update-hosts-template)
        UPDATE_HOSTS_TEMPLATE=true; shift ;;
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
  4) uninstall (remove containers and images)
  5) update repo (refresh local repository)
PROMPT
  read -rp "Enter choice [1-5]: " choice
  case "$choice" in
    1) INSTALL_TARGET="master" ;;
    2) INSTALL_TARGET="agent" ;;
    3) INSTALL_TARGET="all" ;;
    4)
      INSTALL_TARGET="uninstall"
      UNINSTALL=true
      return
      ;;
    5)
      INSTALL_TARGET="update"
      UPDATE_ONLY=true
      return
      ;;
    *)
      log "Invalid selection. Please choose 1, 2, 3, 4, or 5."
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

    read -rp "Dashboard port [${MASTER_WEB_PORT}]: " input || true
    if [ -n "$input" ]; then
      MASTER_WEB_PORT="$input"
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
    update)
      UPDATE_ONLY=true
      ;;
    uninstall)
      UNINSTALL=true
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

is_master_running() {
  if ! command -v docker >/dev/null 2>&1; then
    return 1
  fi

  docker ps -a --format '{{.Names}}' | grep -q 'master-api'
}

prevent_agent_when_master_present() {
  if [ "${INSTALL_AGENT}" != true ] || [ "${INSTALL_MASTER}" = true ]; then
    return
  fi

  if is_master_running; then
    log "Detected existing master installation (container name contains 'master-api'). Agent-only install aborted."
    log "Use the uninstall option or choose 'all' if you intend to reinstall everything."
    exit 1
  fi
}

parse_hosts_file() {
  HOST_ENTRIES=()

  while IFS= read -r RAW_LINE; do
    LINE="${RAW_LINE%%#*}"
    LINE="${LINE#${LINE%%[![:space:]]*}}"
    LINE="${LINE%${LINE##*[![:space:]]}}"
    [ -z "$LINE" ] && continue

    HOST_ENTRIES+=("$LINE")
  done < "${HOSTS_FILE}"
}

select_hosts_for_deploy() {
  SELECTED_HOSTS=()

  if [ ${#HOST_ENTRIES[@]} -eq 0 ]; then
    return
  fi

  if [ ! -t 0 ] || [ ${#HOST_ENTRIES[@]} -le 1 ]; then
    SELECTED_HOSTS=("${HOST_ENTRIES[@]}")
    return
  fi

  log "Detected ${#HOST_ENTRIES[@]} remote host entries:"
  for idx in "${!HOST_ENTRIES[@]}"; do
    printf "  %s) %s\n" "$((idx + 1))" "${HOST_ENTRIES[$idx]}"
  done
  echo "  a) all (default)"

  read -rp "Choose host to deploy [a]: " choice || true

  if [ -z "$choice" ] || [[ "$choice" =~ ^[aA]$ ]]; then
    SELECTED_HOSTS=("${HOST_ENTRIES[@]}")
    return
  fi

  if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le ${#HOST_ENTRIES[@]} ]; then
    SELECTED_HOSTS=("${HOST_ENTRIES[$((choice - 1))]}")
    return
  fi

  log "Invalid selection; deploying to all hosts."
  SELECTED_HOSTS=("${HOST_ENTRIES[@]}")
}

main() {
  parse_args "$@"

  prompt_install_target

  set_install_flags

  if [ "${UPDATE_ONLY}" = true ]; then
    ensure_repo_ready
    log "Repository refreshed; exiting."
    exit 0
  fi

  if [ "${UNINSTALL}" = true ]; then
    ensure_repo_ready
    ensure_docker
    ensure_compose
    uninstall_project
    exit 0
  fi

  prompt_ports
  maybe_download_repo
  ensure_repo_ready
  sync_hosts_template
  resolve_paths
  ensure_docker
  prevent_agent_when_master_present
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
    log "  Dashboard UI on http://localhost:${MASTER_WEB_PORT}/web."
    local dashboard_password="${DASHBOARD_PASSWORD:-iperf-pass}"
    log "  Dashboard password: ${dashboard_password}"
  fi

  if [ "${INSTALL_AGENT}" = true ]; then
    log "  Local agent on http://localhost:${AGENT_PORT}."
  fi
}

main "$@"
