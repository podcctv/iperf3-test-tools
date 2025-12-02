#!/usr/bin/env bash
set -euo pipefail

# Optional arguments: REPO_DIR BRANCH
REPO_DIR="${1:-$(pwd)}"
BRANCH="${2:-$(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)}"

if ! command -v git >/dev/null 2>&1; then
  echo "[auto_update] git is required but not installed." >&2
  exit 1
fi

if [ ! -d "${REPO_DIR}/.git" ]; then
  echo "[auto_update] ${REPO_DIR} does not look like a git repository." >&2
  exit 1
fi

cd "${REPO_DIR}"

if [ -n "$(git status --porcelain)" ]; then
  echo "[auto_update] Working tree has local changes; aborting to avoid overwriting them." >&2
  exit 1
fi

echo "[auto_update] Fetching updates for ${BRANCH}..."
git fetch --all --prune

echo "[auto_update] Pulling latest changes..."
git pull --ff-only origin "${BRANCH}"

echo "[auto_update] Restoring executable permissions..."
chmod +x install_master.sh install_agent.sh deploy_agents.sh 2>/dev/null || true
find agent master-api -name '*.sh' -exec chmod +x {} + 2>/dev/null || true

echo "[auto_update] Completed." 
