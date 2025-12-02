#!/usr/bin/env bash
set -euo pipefail

# Optional arguments: REPO_DIR BRANCH
# Optional environment variables:
# - AUTO_UPDATE_DIRTY_MODE: how to handle local changes (abort|stash|reset). Default: abort.
REPO_DIR="${1:-$(pwd)}"
BRANCH="${2:-$(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)}"
DIRTY_MODE="${AUTO_UPDATE_DIRTY_MODE:-abort}"
STASH_REF=""

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
  case "${DIRTY_MODE}" in
    stash)
      echo "[auto_update] Working tree has local changes; stashing before update..."
      git stash push -u -m "[auto_update] before updating ${BRANCH}"
      STASH_REF="$(git stash list -n 1 --format='%gd')"
      ;;
    reset)
      echo "[auto_update] Working tree has local changes; resetting them before update..."
      git reset --hard HEAD
      ;;
    abort|*)
      echo "[auto_update] Working tree has local changes; aborting to avoid overwriting them." >&2
      exit 1
      ;;
  esac
fi

echo "[auto_update] Fetching updates for ${BRANCH}..."
git fetch --all --prune

echo "[auto_update] Pulling latest changes..."
git pull --ff-only origin "${BRANCH}"

if [ -n "${STASH_REF}" ]; then
  echo "[auto_update] Restoring stashed changes (${STASH_REF})..."
  if ! git stash pop "${STASH_REF}"; then
    echo "[auto_update] Failed to re-apply stashed changes (${STASH_REF}). Please resolve manually with 'git stash list' and 'git stash pop'." >&2
    exit 1
  fi
fi

echo "[auto_update] Restoring executable permissions..."
chmod +x install_master.sh install_agent.sh deploy_agents.sh 2>/dev/null || true
find agent master-api -name '*.sh' -exec chmod +x {} + 2>/dev/null || true

echo "[auto_update] Completed." 
