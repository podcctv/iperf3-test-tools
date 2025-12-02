#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="iperf-agent:latest"
AGENT_PORT=8000
IPERF_PORT=5201
HOSTS_FILE="hosts.txt"

if [ ! -f "$HOSTS_FILE" ]; then
  echo "hosts.txt not found; please create it with one host per line (e.g., user@host)" >&2
  exit 1
fi

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
    command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh && \
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
