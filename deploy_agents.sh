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

while read -r HOST; do
  [ -z "$HOST" ] && continue
  echo "==== Deploying to $HOST ===="

  ssh -o StrictHostKeyChecking=no "$HOST" "\
    command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh && \
    docker rm -f iperf-agent || true && \
    docker run -d --name iperf-agent \
      --restart=always \
      -p ${AGENT_PORT}:8000 \
      -p ${IPERF_PORT}:${IPERF_PORT}/tcp \
      -p ${IPERF_PORT}:${IPERF_PORT}/udp \
      ${IMAGE_NAME}
  "
done < "$HOSTS_FILE"

echo "All agents deployed."
