#!/usr/bin/env bash
# Helper installer to set up an agent-only deployment on a remote host.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ALL_SCRIPT="${SCRIPT_DIR}/install_all.sh"

if [ ! -x "${INSTALL_ALL_SCRIPT}" ]; then
  echo "install_all.sh not found or not executable at ${INSTALL_ALL_SCRIPT}" >&2
  exit 1
fi

agent_port="${AGENT_PORT:-8000}"
iperf_port="${IPERF_PORT:-5201}"

pass_through=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent-port)
      agent_port="$2"
      pass_through+=("$1" "$2")
      shift 2
      ;;
    --iperf-port)
      iperf_port="$2"
      pass_through+=("$1" "$2")
      shift 2
      ;;
    *)
      pass_through+=("$1")
      shift
      ;;
  esac
done

env INSTALL_TARGET="agent" \
    START_LOCAL_AGENT=true \
    DEPLOY_REMOTE=false \
    AGENT_PORT="${agent_port}" \
    IPERF_PORT="${iperf_port}" \
    "${INSTALL_ALL_SCRIPT}" "${pass_through[@]}"

agent_host=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "${agent_host}" ]; then
  agent_host="$(hostname -f 2>/dev/null || hostname)"
fi

cat <<INFO
Agent installation complete.
Add this agent to the master dashboard with:
  Agent URL: http://${agent_host}:${agent_port}
  iperf3 port: ${iperf_port}
INFO
