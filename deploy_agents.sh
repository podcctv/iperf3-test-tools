#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="iperf-agent:latest"
AGENT_PORT=8000
IPERF_PORT=62001
HOSTS_FILE=""

usage() {
  cat <<'USAGE'
Usage: deploy_agents.sh --hosts-file <path> [--agent-port 8000] [--iperf-port 62001] [--image iperf-agent:latest]

Deploy the agent image to a list of SSH hosts provided in a newline-delimited inventory file.
Lines may be formatted as "user@host [agent_port] [iperf_port]" with optional SSH port suffix (user@host:2222).
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hosts-file|--hosts)
      HOSTS_FILE="$2"; shift 2 ;;
    --agent-port)
      AGENT_PORT="$2"; shift 2 ;;
    --iperf-port)
      IPERF_PORT="$2"; shift 2 ;;
    --image)
      IMAGE_NAME="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1 ;;
  esac
done

if [ -z "$HOSTS_FILE" ] || [ ! -f "$HOSTS_FILE" ]; then
  echo "A hosts inventory file is required. Provide it with --hosts-file <path>." >&2
  exit 1
fi

ensure_image_remote() {
  local host=$1
  local ssh_port=$2

  if ssh -o StrictHostKeyChecking=no ${ssh_port:+-p ${ssh_port}} "$host" "docker image inspect ${IMAGE_NAME} >/dev/null 2>&1"; then
    return 0
  fi

  echo "Image ${IMAGE_NAME} not found on ${host}; streaming local image..."
  if ! docker image inspect ${IMAGE_NAME} >/dev/null 2>&1; then
    echo "Local image ${IMAGE_NAME} is missing. Please build it before running deploy_agents.sh" >&2
    exit 1
  fi

  docker save ${IMAGE_NAME} | gzip | ssh -o StrictHostKeyChecking=no ${ssh_port:+-p ${ssh_port}} "$host" "gunzip | docker load"
}

while IFS= read -r RAW_LINE; do
  LINE="${RAW_LINE%%#*}"
  LINE="${LINE#${LINE%%[![:space:]]*}}"
  LINE="${LINE%${LINE##*[![:space:]]}}"
  [ -z "$LINE" ] && continue

  IFS=' ' read -r HOST HOST_AGENT_PORT HOST_IPERF_PORT <<< "$LINE"
  HOST_AGENT_PORT=${HOST_AGENT_PORT:-$AGENT_PORT}
  HOST_IPERF_PORT=${HOST_IPERF_PORT:-$IPERF_PORT}

  SSH_HOST="$HOST"
  SSH_PORT=""

  # Allow host strings like user@example.com:2222 by translating the port
  # suffix into "ssh -p" while preserving plain hosts and IPv6 literals.
  colon_count=${HOST//[^:]}
  colon_count=${#colon_count}
  if [[ $colon_count -eq 1 && "$HOST" =~ ^(.+):([0-9]+)$ ]]; then
    SSH_HOST="${BASH_REMATCH[1]}"
    SSH_PORT="${BASH_REMATCH[2]}"
  fi

  echo "==== Deploying to $HOST (agent port: ${HOST_AGENT_PORT}, iperf3 port: ${HOST_IPERF_PORT}) ===="

  ssh -o StrictHostKeyChecking=no ${SSH_PORT:+-p ${SSH_PORT}} "$SSH_HOST" "\
    command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh"

  ensure_image_remote "$SSH_HOST" "$SSH_PORT"

  ssh -o StrictHostKeyChecking=no ${SSH_PORT:+-p ${SSH_PORT}} "$SSH_HOST" "\
    docker rm -f iperf-agent || true && \
    docker run -d --name iperf-agent \
      --restart=always \
      -p ${HOST_AGENT_PORT}:8000 \
      -p ${HOST_IPERF_PORT}:${HOST_IPERF_PORT}/tcp \
      -p ${HOST_IPERF_PORT}:${HOST_IPERF_PORT}/udp \
      ${IMAGE_NAME}
  "
done < "$HOSTS_FILE"

echo "All agents deployed."
