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

# 检查端口是否被占用
check_port_available() {
    local port="$1"
    if command -v ss &>/dev/null; then
        ! ss -tuln | grep -q ":${port} "
    elif command -v netstat &>/dev/null; then
        ! netstat -tuln | grep -q ":${port} "
    else
        # 无法检查，假设可用
        return 0
    fi
}

# 提示用户输入可用端口
prompt_available_port() {
    local label="$1" default_port="$2" port
    port="$default_port"
    
    while true; do
        if ! validate_port "$port"; then
            read -rp "请输入 ${label} (1-65535, 默认 ${default_port})：" port
            port="${port:-$default_port}"
            continue
        fi
        
        if check_port_available "$port"; then
            echo "$port"
            return 0
        else
            echo "[WARN] 端口 $port 已被占用" >&2
            read -rp "请输入其他可用端口 (1-65535)：" port
        fi
    done
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

    UPDATED_FILES=$(git diff --name-only "$LOCAL_HASH" HEAD)
    if echo "$UPDATED_FILES" | grep -q "update_iperf3_master.sh" && [ -z "${IPERF3_UPDATER_RERUN}" ]; then
        echo "[INFO] Detected updater script changes. Re-running with latest version..."
        IPERF3_UPDATER_RERUN=1 exec bash "$0" "$@"
    fi
else
    echo "[INFO] Already the latest version. No update needed."
fi

echo
echo "======== 安装选项（本地dockerfile编译） ========="
echo "1) 自动安装 master（含本机 agent 容器）"
echo "2) 自动安装 agent（仅作为测试节点）"
echo "3) 手动安装 agent（NAT VPS 指定端口）"
echo "4) 手动安装 agent（内网设备 反向穿透）"
echo "========== 安装选项（pull ghcr.io） ==========="
echo "5) 自动安装 master（含本机 agent 容器）"
echo "6) 自动安装 agent（仅作为测试节点）"
echo "7) 手动安装 agent（NAT VPS 指定端口）"
echo "8) 手动安装 agent（内网设备 反向穿透）"
echo "================ 查询选项 ================"
echo "9) 查看 iperf-agent 日志"
echo "10) 查看 master-api 日志"
echo "================ 其他选项 ================"
echo "11) 退出"
echo "=========================================="
read -rp "请选择 [1-11]：" choice

# GHCR.io 镜像地址
GHCR_MASTER="ghcr.io/podcctv/iperf3-master-api:latest"
GHCR_AGENT="ghcr.io/podcctv/iperf3-agent:latest"
GHCR_AGENT_CN="ghcr.io/podcctv/iperf3-agent:cn"

# 检测是否为中国地区
detect_region() {
    local ip country
    ip=$(curl -fsS --connect-timeout 5 https://api.ipify.org 2>/dev/null || curl -fsS --connect-timeout 5 https://ifconfig.me 2>/dev/null || true)
    if [ -n "$ip" ]; then
        country=$(curl -fsS --connect-timeout 5 "http://ip-api.com/line/${ip}?fields=countryCode" 2>/dev/null || true)
        case "$country" in
            CN|cn) echo "cn" ;;
            *) echo "global" ;;
        esac
    else
        echo "global"
    fi
}

# 获取合适的 agent 镜像
get_agent_image() {
    local use_ghcr="$1"
    if [ "$use_ghcr" = "true" ]; then
        local region=$(detect_region)
        if [ "$region" = "cn" ]; then
            echo "$GHCR_AGENT_CN"
        else
            echo "$GHCR_AGENT"
        fi
    else
        echo "iperf-agent:latest"
    fi
}

# 显示 master 登录密码
show_master_password() {
    echo ""
    echo "================================================"
    echo "正在获取 Dashboard 登录密码..."
    echo "================================================"
    sleep 3  # 等待容器启动
    
    # 尝试从日志获取密码
    MASTER_CONTAINER=$(docker ps -q --filter "name=master-api" | head -1)
    if [ -z "$MASTER_CONTAINER" ]; then
        MASTER_CONTAINER=$(docker ps -q --filter "name=iperf3-test-tools-master" | head -1)
    fi
    
    if [ -n "$MASTER_CONTAINER" ]; then
        PASSWORD=$(docker logs "$MASTER_CONTAINER" 2>&1 | grep -i "Dashboard password initialized" | tail -1 | sed 's/.*Dashboard password initialized: //')
        if [ -n "$PASSWORD" ]; then
            echo ""
            echo "=============================================="
            echo "   Dashboard 登录密码: $PASSWORD"
            echo "=============================================="
            echo ""
            echo "[提示] 密码已保存到容器内部，重启后保持不变"
            echo "[提示] 如需修改密码，可在 Dashboard 设置中更改"
        else
            echo "[WARN] 无法从日志获取密码，请手动查看:"
            echo "       docker logs $MASTER_CONTAINER | grep 'Dashboard password'"
        fi
    else
        echo "[ERROR] 未找到 master-api 容器"
    fi
}

case "$choice" in
    1)
        # 本地编译 - 自动安装 master（含本机 agent）
        cleanup_docker
        echo "[INFO] 本地编译安装 master..."
        bash "$MASTER_INSTALL_SCRIPT"
        echo "[INFO] Master 安装完成！(含本机 agent)"
        show_master_password
        ;;
    2)
        # 本地编译 - 自动安装 agent
        cleanup_docker
        echo "[INFO] 本地编译安装 agent..."
        bash "$AGENT_INSTALL_SCRIPT"
        echo "[INFO] Agent 安装完成！"
        ;;
    3)
        # 本地编译 - 手动安装 agent（NAT VPS）
        cleanup_docker
        echo "[INFO] 本地编译 - 手动安装 agent (NAT 模式)..."
        AGENT_PORT=$(prompt_required_port "Agent API 端口（宿主机 NAT 映射端口）" "${AGENT_PORT:-}")
        AGENT_LISTEN_PORT=$(prompt_optional_port "Agent API 端口（容器内监听，留空则与宿主机相同）" "${AGENT_LISTEN_PORT:-}")
        IPERF_PORT=$(prompt_required_port "iperf3 端口（宿主机 NAT 映射端口）" "${IPERF_PORT:-}")
        if [ -x "$MANUAL_AGENT_SCRIPT" ]; then
            AGENT_PORT="$AGENT_PORT" AGENT_LISTEN_PORT="$AGENT_LISTEN_PORT" IPERF_PORT="$IPERF_PORT" bash "$MANUAL_AGENT_SCRIPT"
        else
            echo "[ERROR] 手动安装脚本未找到：$MANUAL_AGENT_SCRIPT"
            exit 1
        fi
        ;;
    4)
        # 本地编译 - 手动安装 agent（内网设备，反向穿透）
        cleanup_docker
        echo "[INFO] 本地编译 - 内网 agent 安装（反向穿透模式）"
        read -rp "请输入主控 Master API URL (如 https://yourdomain.com): " MASTER_URL
        while [ -z "$MASTER_URL" ]; do
            read -rp "Master URL 不能为空，请重新输入: " MASTER_URL
        done
        read -rp "请输入节点名称 (用于在主控中显示): " NODE_NAME
        while [ -z "$NODE_NAME" ]; do
            read -rp "节点名称不能为空，请重新输入: " NODE_NAME
        done
        IPERF_PORT=$(prompt_required_port "iperf3 端口" "5201")
        
        echo "[INFO] 正在本地构建 agent 镜像..."
        docker build -t iperf-agent-reverse:latest "${REPO_DIR}/agent"
        
        docker run -d \
            --name iperf-agent-reverse \
            --restart=always \
            -p ${IPERF_PORT}:${IPERF_PORT}/tcp \
            -p ${IPERF_PORT}:${IPERF_PORT}/udp \
            -e MASTER_URL="$MASTER_URL" \
            -e NODE_NAME="$NODE_NAME" \
            -e IPERF_PORT="$IPERF_PORT" \
            -e AGENT_MODE="reverse" \
            iperf-agent-reverse:latest
        
        echo "[INFO] 内网 agent 安装完成！"
        echo "[INFO] Agent 将定期向 $MASTER_URL 注册"
        ;;
    5)
        # GHCR - 自动安装 master（含本机 agent）
        
        # 先检查所有端口
        echo ""
        echo "========== 端口检查 =========="
        DEFAULT_MASTER_PORT=9000
        echo "[INFO] 检查 Master API 端口 ${DEFAULT_MASTER_PORT} 是否可用..."
        MASTER_PORT=$(prompt_available_port "Master API 端口" "$DEFAULT_MASTER_PORT")
        
        DEFAULT_AGENT_PORT=8000
        echo "[INFO] 检查 Agent API 端口 ${DEFAULT_AGENT_PORT} 是否可用..."
        AGENT_PORT=$(prompt_available_port "Agent API 端口" "$DEFAULT_AGENT_PORT")
        
        DEFAULT_IPERF_PORT=5201
        echo "[INFO] 检查 iperf3 端口 ${DEFAULT_IPERF_PORT} 是否可用..."
        IPERF_PORT=$(prompt_available_port "iperf3 端口" "$DEFAULT_IPERF_PORT")
        
        echo ""
        echo "========== 端口配置确认 =========="
        echo "Master API 端口: $MASTER_PORT"
        echo "Agent API 端口:  $AGENT_PORT"
        echo "iperf3 端口:     $IPERF_PORT"
        echo "=================================="
        echo ""
        
        # 清理旧容器
        cleanup_docker
        
        # 使用本地构建 master-api（确保使用最新代码，包含随机密码功能）
        echo "[INFO] 本地构建 master-api 镜像（使用最新代码）..."
        cd "$REPO_DIR"
        MASTER_API_PORT="$MASTER_PORT" docker compose up -d --build
        
        # 安装 agent（使用 ghcr.io 镜像）
        AGENT_IMAGE=$(get_agent_image "true")
        echo "[INFO] 从 ghcr.io 拉取 agent 镜像: $AGENT_IMAGE..."
        docker pull "$AGENT_IMAGE"
        
        echo "[INFO] 启动本机 agent 容器..."
        docker run -d \
            --name iperf-agent \
            --restart=always \
            -p ${AGENT_PORT}:8000 \
            -p ${IPERF_PORT}:${IPERF_PORT}/tcp \
            -p ${IPERF_PORT}:${IPERF_PORT}/udp \
            -e IPERF_PORT="$IPERF_PORT" \
            "$AGENT_IMAGE"
        
        echo ""
        echo "[INFO] Master + Agent 安装完成！"
        echo "[INFO] Master 端口: $MASTER_PORT"
        echo "[INFO] Agent API 端口: $AGENT_PORT"
        echo "[INFO] iperf3 端口: $IPERF_PORT"
        show_master_password
        ;;
    6)
        # GHCR - 自动安装 agent
        cleanup_docker
        AGENT_IMAGE=$(get_agent_image "true")
        echo "[INFO] 从 ghcr.io 拉取 agent 镜像: $AGENT_IMAGE ..."
        docker pull "$AGENT_IMAGE"
        
        IPERF_PORT=${IPERF_PORT:-5201}
        docker run -d \
            --name iperf-agent \
            --restart=always \
            -p 8000:8000 \
            -p ${IPERF_PORT}:${IPERF_PORT}/tcp \
            -p ${IPERF_PORT}:${IPERF_PORT}/udp \
            -e IPERF_PORT="$IPERF_PORT" \
            "$AGENT_IMAGE"
        
        echo "[INFO] Agent 安装完成！(使用 ghcr.io 镜像)"
        ;;
    7)
        # GHCR - 手动安装 agent（NAT VPS）
        cleanup_docker
        AGENT_IMAGE=$(get_agent_image "true")
        echo "[INFO] 从 ghcr.io 拉取 agent (NAT 模式): $AGENT_IMAGE ..."
        docker pull "$AGENT_IMAGE"
        
        AGENT_PORT=$(prompt_required_port "Agent API 端口（宿主机 NAT 映射端口）" "${AGENT_PORT:-}")
        AGENT_LISTEN_PORT=$(prompt_optional_port "Agent API 端口（容器内监听，留空则与宿主机相同）" "${AGENT_LISTEN_PORT:-}")
        IPERF_PORT=$(prompt_required_port "iperf3 端口（宿主机 NAT 映射端口）" "${IPERF_PORT:-}")
        
        CONTAINER_API_PORT=${AGENT_LISTEN_PORT:-$AGENT_PORT}
        
        docker run -d \
            --name iperf-agent \
            --restart=always \
            -p ${AGENT_PORT}:${CONTAINER_API_PORT} \
            -p ${IPERF_PORT}:${IPERF_PORT}/tcp \
            -p ${IPERF_PORT}:${IPERF_PORT}/udp \
            -e AGENT_API_PORT="${CONTAINER_API_PORT}" \
            -e IPERF_PORT="$IPERF_PORT" \
            "$AGENT_IMAGE"
        
        echo "[INFO] Agent 安装完成！(NAT 模式, ghcr.io 镜像)"
        ;;
    8)
        # GHCR - 手动安装 agent（内网设备，反向穿透）
        cleanup_docker
        AGENT_IMAGE=$(get_agent_image "true")
        echo "[INFO] 从 ghcr.io 拉取 agent (反向穿透): $AGENT_IMAGE ..."
        docker pull "$AGENT_IMAGE"
        
        read -rp "请输入主控 Master API URL (如 https://yourdomain.com): " MASTER_URL
        while [ -z "$MASTER_URL" ]; do
            read -rp "Master URL 不能为空，请重新输入: " MASTER_URL
        done
        read -rp "请输入节点名称 (用于在主控中显示): " NODE_NAME
        while [ -z "$NODE_NAME" ]; do
            read -rp "节点名称不能为空，请重新输入: " NODE_NAME
        done
        IPERF_PORT=$(prompt_required_port "iperf3 端口" "5201")
        
        docker run -d \
            --name iperf-agent-reverse \
            --restart=always \
            -p ${IPERF_PORT}:${IPERF_PORT}/tcp \
            -p ${IPERF_PORT}:${IPERF_PORT}/udp \
            -e MASTER_URL="$MASTER_URL" \
            -e NODE_NAME="$NODE_NAME" \
            -e IPERF_PORT="$IPERF_PORT" \
            -e AGENT_MODE="reverse" \
            "$AGENT_IMAGE"
        
        echo "[INFO] 内网 agent 安装完成！(ghcr.io 镜像)"
        echo "[INFO] Agent 将定期向 $MASTER_URL 注册"
        ;;
    9)
        # 查看 iperf-agent 日志
        echo "[INFO] 查看 iperf-agent 日志 (按 Ctrl+C 退出)..."
        AGENT_CONTAINER=$(docker ps -q --filter "name=iperf-agent" | head -1)
        if [ -z "$AGENT_CONTAINER" ]; then
            AGENT_CONTAINER=$(docker ps -q --filter "name=iperf3-test-tools-agent" | head -1)
        fi
        if [ -n "$AGENT_CONTAINER" ]; then
            docker logs -f "$AGENT_CONTAINER"
        else
            echo "[ERROR] 未找到 iperf-agent 容器"
            docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
        fi
        ;;
    10)
        # 查看 master-api 日志
        echo "[INFO] 查看 master-api 日志 (按 Ctrl+C 退出)..."
        MASTER_CONTAINER=$(docker ps -q --filter "name=master-api" | head -1)
        if [ -z "$MASTER_CONTAINER" ]; then
            MASTER_CONTAINER=$(docker ps -q --filter "name=iperf3-test-tools-master" | head -1)
        fi
        if [ -n "$MASTER_CONTAINER" ]; then
            docker logs -f "$MASTER_CONTAINER"
        else
            echo "[ERROR] 未找到 master-api 容器"
            docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
        fi
        ;;
    11)
        echo "[INFO] 退出安装程序。"
        exit 0
        ;;
    *)
        echo "[WARN] 无效选项。未执行任何操作。"
        ;;
esac
