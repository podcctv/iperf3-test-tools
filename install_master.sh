#!/usr/bin/env bash
# Helper installer to set up master API, dashboard, and a local agent.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ALL_SCRIPT="${SCRIPT_DIR}/install_all.sh"

if [ ! -x "${INSTALL_ALL_SCRIPT}" ]; then
  echo "install_all.sh not found or not executable at ${INSTALL_ALL_SCRIPT}" >&2
  exit 1
fi

deploy_remote=false
pass_through=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deploy-remote)
      deploy_remote=true
      shift
      ;;
    *)
      pass_through+=("$1")
      shift
      ;;
  esac
done

env INSTALL_TARGET="all" \
    START_LOCAL_AGENT=true \
    DEPLOY_REMOTE="${deploy_remote}" \
    "${INSTALL_ALL_SCRIPT}" "${pass_through[@]}"
