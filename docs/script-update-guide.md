# Shell Script Update Troubleshooting / 脚本更新故障排查

This guide collects common reasons why the installer or deployment scripts fail to update, plus a simple way to automate refreshes.

## Typical failure causes / 常见失败原因

- **Working tree not clean / 工作区有改动**: pending edits block `git pull`. Run `git status` to confirm and commit or stash changes.
- **Network interruptions / 网络不通**: outbound GitHub access or proxy settings can block `git fetch`. Retry with a reachable mirror when necessary.
- **File permissions / 权限丢失**: the executable bit on `.sh` files might be dropped when copying between filesystems. Re-apply with `chmod +x *.sh`.
- **Windows line endings / 行尾符问题**: CRLF endings break `bash`. Convert with `sed -i 's/\r$//' <file>` or `dos2unix`.
- **Locked files or running processes / 文件被占用**: stop any running container startup scripts before replacing them.

## Quick recovery steps / 快速修复步骤

1. Ensure the repository is intact:
   ```bash
   git status
   git fetch --all --prune
   git pull --ff-only
   ```
2. Restore executable bits if needed:
   ```bash
   chmod +x install_master.sh install_agent.sh deploy_agents.sh
   find agent master-api -name '*.sh' -exec chmod +x {} +
   ```
3. Re-run the installer with verbose output to capture errors:
   ```bash
   bash -x ./install_master.sh --clean-existing
   ```

## Automating `.sh` updates / 自动更新脚本

Use `tools/auto_update.sh` to pull the latest scripts and re-apply permissions. It aborts if the working tree has local changes to avoid overwriting edits.

```bash
# Update the current branch in-place
./tools/auto_update.sh

# Specify a repo path or branch explicitly
./tools/auto_update.sh /path/to/iperf3-test-tools main
```

Integrate with cron or a systemd timer for unattended updates, e.g. `0 3 * * * /path/to/tools/auto_update.sh >> /var/log/iperf3-tools-update.log 2>&1`.
