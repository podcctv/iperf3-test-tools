#!/usr/bin/env bash
# iperf-agent-watchdog.sh
# 
# Watchdog script for iperf3 agent auto-update.
# Runs on the host machine, monitors for update requests from the agent container.
# 
# Install location: /usr/local/bin/iperf-agent-watchdog.sh
# Runs via cron every hour.

set -euo pipefail

# Configuration - can be overridden by environment or config file
AGENT_DATA_DIR="${AGENT_DATA_DIR:-/var/lib/iperf-agent/data}"
AGENT_CONTAINER_NAME="${AGENT_CONTAINER_NAME:-iperf-agent}"
AGENT_IMAGE="${AGENT_IMAGE:-iperf-agent:latest}"
LOG_FILE="/var/log/iperf-agent-watchdog.log"
LOCK_FILE="/var/run/iperf-agent-watchdog.lock"
REPO_DIR="${REPO_DIR:-/opt/iperf3-test-tools}"

# Load config file if exists
CONFIG_FILE="/etc/iperf-agent-watchdog.conf"
if [ -f "$CONFIG_FILE" ]; then
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
fi

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Ensure only one instance runs
acquire_lock() {
    exec 200>"$LOCK_FILE"
    if ! flock -n 200; then
        exit 0  # Another instance is running, exit silently
    fi
}

# Check if update is requested
check_update_request() {
    local request_file="${AGENT_DATA_DIR}/update_request.json"
    
    if [ ! -f "$request_file" ]; then
        return 1
    fi
    
    # Parse request file
    if command -v jq >/dev/null 2>&1; then
        TARGET_VERSION=$(jq -r '.target_version // empty' "$request_file" 2>/dev/null)
        TARGET_IMAGE=$(jq -r '.target_image // empty' "$request_file" 2>/dev/null)
        CURRENT_VERSION=$(jq -r '.current_version // empty' "$request_file" 2>/dev/null)
    else
        # Fallback: simple grep parsing
        TARGET_VERSION=$(grep -o '"target_version"[[:space:]]*:[[:space:]]*"[^"]*"' "$request_file" | cut -d'"' -f4)
        TARGET_IMAGE=$(grep -o '"target_image"[[:space:]]*:[[:space:]]*"[^"]*"' "$request_file" | cut -d'"' -f4)
        CURRENT_VERSION=$(grep -o '"current_version"[[:space:]]*:[[:space:]]*"[^"]*"' "$request_file" | cut -d'"' -f4)
    fi
    
    if [ -z "$TARGET_VERSION" ] || [ -z "$TARGET_IMAGE" ]; then
        log "Invalid update request file, removing"
        rm -f "$request_file"
        return 1
    fi
    
    log "Update request found: ${CURRENT_VERSION} -> ${TARGET_VERSION}"
    return 0
}

# Detect if we're in China and should use local build
detect_region() {
    local ip country
    
    # Try to detect public IP and region
    if command -v curl >/dev/null 2>&1; then
        ip=$(curl -fsS --connect-timeout 5 https://api.ipify.org 2>/dev/null || true)
        if [ -n "$ip" ]; then
            country=$(curl -fsS --connect-timeout 5 "http://ip-api.com/line/${ip}?fields=countryCode" 2>/dev/null || true)
        fi
    fi
    
    case "${country:-}" in
        CN|cn|China|china)
            echo "cn"
            ;;
        *)
            echo "global"
            ;;
    esac
}

# Get current agent configuration for container restart
get_agent_config() {
    # Read config from running container or data directory
    local config_file="${AGENT_DATA_DIR}/config.json"
    
    if [ -f "$config_file" ]; then
        if command -v jq >/dev/null 2>&1; then
            IPERF_PORT=$(jq -r '.iperf_port // 62001' "$config_file")
            AGENT_PORT=$(jq -r '.agent_port // 8000' "$config_file")
            AGENT_MODE=$(jq -r '.agent_mode // "normal"' "$config_file")
            MASTER_URL=$(jq -r '.master_url // ""' "$config_file")
            NODE_NAME=$(jq -r '.node_name // ""' "$config_file")
            POLL_INTERVAL=$(jq -r '.poll_interval // 10' "$config_file")
        else
            IPERF_PORT=$(grep -o '"iperf_port"[[:space:]]*:[[:space:]]*[0-9]*' "$config_file" | grep -o '[0-9]*$' || echo "62001")
            AGENT_PORT=$(grep -o '"agent_port"[[:space:]]*:[[:space:]]*[0-9]*' "$config_file" | grep -o '[0-9]*$' || echo "8000")
            AGENT_MODE="normal"
            MASTER_URL=""
            NODE_NAME=""
            POLL_INTERVAL=10
        fi
    else
        # Fallback to inspecting running container
        IPERF_PORT=$(docker inspect "$AGENT_CONTAINER_NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep "^IPERF_PORT=" | cut -d= -f2 || echo "62001")
        AGENT_PORT="8000"
        AGENT_MODE=$(docker inspect "$AGENT_CONTAINER_NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep "^AGENT_MODE=" | cut -d= -f2 || echo "normal")
        MASTER_URL=$(docker inspect "$AGENT_CONTAINER_NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep "^MASTER_URL=" | cut -d= -f2 || echo "")
        NODE_NAME=$(docker inspect "$AGENT_CONTAINER_NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep "^NODE_NAME=" | cut -d= -f2 || echo "")
        POLL_INTERVAL=$(docker inspect "$AGENT_CONTAINER_NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep "^POLL_INTERVAL=" | cut -d= -f2 || echo "10")
    fi
    
    # Ensure defaults
    IPERF_PORT="${IPERF_PORT:-62001}"
    AGENT_PORT="${AGENT_PORT:-8000}"
    AGENT_MODE="${AGENT_MODE:-normal}"
    POLL_INTERVAL="${POLL_INTERVAL:-10}"
}

# Pull or build the image based on region
prepare_image() {
    local image="$1"
    local region
    region=$(detect_region)
    
    if [ "$region" = "cn" ]; then
        log "CN region detected, building image locally..."
        
        # Ensure repo is available
        if [ ! -d "$REPO_DIR/.git" ]; then
            log "Cloning repository..."
            git clone --depth 1 https://github.com/podcctv/iperf3-test-tools.git "$REPO_DIR" 2>&1 | tee -a "$LOG_FILE"
        else
            log "Updating repository..."
            git -C "$REPO_DIR" pull origin main 2>&1 | tee -a "$LOG_FILE" || true
        fi
        
        # Build with CN mirrors
        log "Building image with Aliyun mirrors..."
        docker build \
            --build-arg APT_MIRROR=https://mirrors.aliyun.com \
            --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
            --build-arg PIP_TRUSTED_HOST=mirrors.aliyun.com \
            -t "$AGENT_IMAGE" \
            "$REPO_DIR/agent" 2>&1 | tee -a "$LOG_FILE"
        
        # Use local image name
        FINAL_IMAGE="$AGENT_IMAGE"
    else
        log "Pulling image from registry: $image"
        if docker pull "$image" 2>&1 | tee -a "$LOG_FILE"; then
            FINAL_IMAGE="$image"
        else
            log "Pull failed, falling back to local build..."
            prepare_image "$image"  # Retry with local build
            return
        fi
    fi
}

# Execute the update
execute_update() {
    log "Executing update..."
    
    # Get current config
    get_agent_config
    
    log "Config: iperf_port=$IPERF_PORT, agent_port=$AGENT_PORT, mode=$AGENT_MODE"
    
    # Prepare the image
    prepare_image "$TARGET_IMAGE"
    
    # Stop and remove current container
    log "Stopping current container..."
    docker stop "$AGENT_CONTAINER_NAME" 2>/dev/null || true
    docker rm "$AGENT_CONTAINER_NAME" 2>/dev/null || true
    
    # Start new container
    log "Starting new container with image: $FINAL_IMAGE"
    local docker_cmd="docker run -d --name $AGENT_CONTAINER_NAME \
        --restart=always \
        -p ${AGENT_PORT}:8000 \
        -p ${IPERF_PORT}:${IPERF_PORT}/tcp \
        -p ${IPERF_PORT}:${IPERF_PORT}/udp \
        -v ${AGENT_DATA_DIR}:/app/data \
        -e IPERF_PORT=${IPERF_PORT} \
        -e AGENT_MODE=${AGENT_MODE} \
        -e CONTAINER_NAME=${AGENT_CONTAINER_NAME}"
    
    # Add optional env vars
    if [ -n "$MASTER_URL" ]; then
        docker_cmd="$docker_cmd -e MASTER_URL=${MASTER_URL}"
    fi
    if [ -n "$NODE_NAME" ]; then
        docker_cmd="$docker_cmd -e NODE_NAME=${NODE_NAME}"
    fi
    docker_cmd="$docker_cmd -e POLL_INTERVAL=${POLL_INTERVAL}"
    docker_cmd="$docker_cmd $FINAL_IMAGE"
    
    log "Running: $docker_cmd"
    eval "$docker_cmd" 2>&1 | tee -a "$LOG_FILE"
    
    # Remove update request file
    rm -f "${AGENT_DATA_DIR}/update_request.json"
    
    # Write update result
    cat > "${AGENT_DATA_DIR}/update_result.json" <<EOF
{
  "status": "success",
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "previous_version": "${CURRENT_VERSION}",
  "new_version": "${TARGET_VERSION}",
  "image": "${FINAL_IMAGE}"
}
EOF
    
    log "Update completed successfully: ${CURRENT_VERSION} -> ${TARGET_VERSION}"
}

# Main
main() {
    acquire_lock
    
    if check_update_request; then
        execute_update
    fi
}

main "$@"
