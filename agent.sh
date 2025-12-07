#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_AGENT_SCRIPT="${SCRIPT_DIR}/install_agent.sh"

validate_port() {
  local port="$1"
  [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ]
}

prompt_port() {
  local label="$1" default_value="$2" value
  value="$default_value"
  while [ -z "$value" ] || ! validate_port "$value"; do
    read -rp "请输入 ${label} (1-65535)：" value
  done
  echo "$value"
}

AGENT_PORT=${AGENT_PORT:-"${1:-}"}
IPERF_PORT=${IPERF_PORT:-"${2:-}"}
AGENT_LISTEN_PORT=${AGENT_LISTEN_PORT:-""}

AGENT_PORT=$(prompt_port "Agent API 端口" "$AGENT_PORT")
IPERF_PORT=$(prompt_port "iperf3 端口" "$IPERF_PORT")

if [ -z "$AGENT_LISTEN_PORT" ]; then
  AGENT_LISTEN_PORT="$AGENT_PORT"
fi

if [ ! -x "$INSTALL_AGENT_SCRIPT" ]; then
  echo "[ERROR] 找不到 install_agent.sh：$INSTALL_AGENT_SCRIPT" >&2
  exit 1
fi

echo "[INFO] 使用以下端口安装 agent："
echo "  Agent API 端口（宿主机）：$AGENT_PORT"
echo "  Agent API 端口（容器内）：$AGENT_LISTEN_PORT"
echo "  iperf3 端口：$IPERF_PORT"

env AGENT_PORT="$AGENT_PORT" AGENT_LISTEN_PORT="$AGENT_LISTEN_PORT" IPERF_PORT="$IPERF_PORT" bash "$INSTALL_AGENT_SCRIPT"
