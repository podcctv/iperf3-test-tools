#!/usr/bin/env bash
set -e

# 当前脚本所在目录（用于找到 agent.sh）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPO_DIR="/root/iperf3-test-tools"
REPO_URL="https://github.com/podcctv/iperf3-test-tools.git"

MASTER_INSTALL_SCRIPT="${REPO_DIR}/install_master.sh"
AGENT_INSTALL_SCRIPT="${REPO_DIR}/install_agent.sh"
MANUAL_AGENT_SCRIPT="${SCRIPT_DIR}/agent.sh"   # 手动 NAT agent 安装脚本

# ------------ Docker 清理函数 ------------
cleanup_docker() {
    echo "[INFO] Cleaning existing Docker resources for iperf3..."

    # 1) 优先使用 docker compose down（如果有 compose 文件）
    if [ -f "${REPO_DIR}/docker-compose.yml" ]; then
        echo "[INFO] docker-compose.yml found, trying docker compose down..."
        (
            cd "${REPO_DIR}"
            if command -v docker &>/dev/null && docker compose version &>/dev/null; then
                docker compose down || true
            elif command -v docker-compose &>/dev/null; then
                docker-compose down || true
            fi
        )
    fi

    # 2) 收集所有相关容器（多次 filter 再去重 = OR 关系）
    CONTAINERS=$(
        {
            docker ps -aq --filter "ancestor=iperf3-test-tools-master-api" || true
            docker ps -aq --filter "ancestor=iperf-agent" || true
            docker ps -aq --filter "name=iperf3-test-tools" || true
            docker ps -aq --filter "name=iperf-agent" || true
        } | sort -u
    )

    if [ -n "$CONTAINERS" ]; then
        echo "[INFO] Removing containers: $CONTAINERS"
        docker rm -f $CONTAINERS || true
    fi

    # 3) 收集并删除相关镜像
    IMAGES=$(docker images --format '{{.ID}} {{.Repository}}' \
        | awk '/iperf3-test-tools-master-api|iperf-agent/ {print $1}' \
        | sort -u)

    if [ -n "$IMAGES" ]; then
        echo "[INFO] Removing images: $IMAGES"
        docker rmi -f $IMAGES || true
    fi

    echo "[INFO] Docker cleanup finished."
}

validate_port() {
    local port="$1"
    [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ]
}

prompt_required_port() {
    local label="$1" default_value="$2" value
    value="$default_value"
    while [ -z "$value" ] || ! validate_port "$value"; do
        read -rp "请输入 ${label} (1-65535)：" value
    done
    echo "$value"
}

prompt_optional_port() {
    local label="$1" default_value="$2" value
    value="$default_value"
    while [ -n "$value" ] && ! validate_port "$value"; do
        read -rp "请输入 ${label} (1-65535，直接回车跳过)：" value
    done
    echo "$value"
}

echo "[INFO] Checking iperf3-test-tools..."

# ------------ 仓库不存在则克隆 ------------
if [ ! -d "$REPO_DIR/.git" ]; then
    echo "[INFO] Repository not found. Cloning..."
    git clone "$REPO_URL" "$REPO_DIR"
    echo "[INFO] Clone completed."
fi

cd "$REPO_DIR"

# ------------ 如果有本地改动，直接丢弃（保持一键可用） ------------
if [ -n "$(git status --porcelain)" ]; then
    echo "[WARN] Detected local changes in ${REPO_DIR}."
    echo "[WARN] All local changes will be discarded to sync with remote."
    git reset --hard HEAD
    git clean -fd
    echo "[INFO] Local repository cleaned."
fi

# ------------ 获取远程信息并更新 ------------
echo "[INFO] Fetching remote info..."
git fetch origin

LOCAL_HASH=$(git rev-parse HEAD)
REMOTE_HASH=$(git rev-parse origin/main)

echo "[INFO] Local:  $LOCAL_HASH"
echo "[INFO] Remote: $REMOTE_HASH"

if [ "$LOCAL_HASH" != "$REMOTE_HASH" ]; then
    echo "[INFO] New version detected. Updating..."
    git pull --rebase
    echo "[INFO] Update completed."
else
    echo "[INFO] Already the latest version. No update needed."
fi

echo
echo "================ 安装选项 ================"
echo "1) 自动安装 master（含本机 agent 容器）"
echo "2) 自动安装 agent（仅作为测试节点）"
echo "3) 手动安装 agent（NAT VPS 指定端口）"
echo "4) 不执行安装（仅更新代码）"
echo "========================================="
read -rp "请选择 [1/2/3/4]：" choice

case "$choice" in
    1)
        # 自动安装 master（含本机 agent）
        cleanup_docker
        echo "[INFO] Installing master..."
        bash "$MASTER_INSTALL_SCRIPT"
        echo "[INFO] Master installation completed. (install_master.sh 已在本机启动 agent)"
        ;;
    2)
        # 自动安装 agent（走 install_agent.sh 默认流程）
        cleanup_docker
        echo "[INFO] Installing agent only (auto mode)..."
        bash "$AGENT_INSTALL_SCRIPT"
        echo "[INFO] Agent installation completed."
        ;;
    3)
        # 手动安装 agent（NAT VPS、端口自行指定）
        echo "[INFO] Manual agent installation (NAT mode, custom ports)..."
        AGENT_PORT=$(prompt_required_port "Agent API 端口（宿主机 NAT 映射端口）" "${AGENT_PORT:-}")
        AGENT_LISTEN_PORT=$(prompt_optional_port "Agent API 端口（容器内监听，留空则与宿主机相同）" "${AGENT_LISTEN_PORT:-}")
        IPERF_PORT=$(prompt_required_port "iperf3 端口（宿主机 NAT 映射端口）" "${IPERF_PORT:-}")
        if [ -x "$MANUAL_AGENT_SCRIPT" ]; then
            AGENT_PORT="$AGENT_PORT" AGENT_LISTEN_PORT="$AGENT_LISTEN_PORT" IPERF_PORT="$IPERF_PORT" bash "$MANUAL_AGENT_SCRIPT"
        else
            echo "[ERROR] 手动安装脚本未找到：$MANUAL_AGENT_SCRIPT"
            echo "[ERROR] 请确认 agent.sh 和 update_iperf3_master.sh 在同一目录，或修改脚本中的 MANUAL_AGENT_SCRIPT 路径。"
            exit 1
        fi
        ;;
    4)
        echo "[INFO] Skip installation. Done."
        ;;
    *)
        echo "[WARN] Invalid choice. No installation performed."
        ;;
esac
