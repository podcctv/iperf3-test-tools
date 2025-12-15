"""Utilities for performing remote management actions over SSH."""

from __future__ import annotations

import shlex
import subprocess
from typing import Optional

from .schemas import AgentConfigRead


class RemoteCommandError(RuntimeError):
    pass


def _run_ssh(target: str, command: str, ssh_port: Optional[int] = None, timeout: int = 180) -> str:
    args = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "BatchMode=yes",
    ]

    if ssh_port:
        args.extend(["-p", str(ssh_port)])

    args.append(target)
    args.append(command)

    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RemoteCommandError(result.stderr.strip() or f"command failed with code {result.returncode}")

    return result.stdout.strip()


def redeploy_agent(config: AgentConfigRead) -> str:
    # Note: Docker socket is NOT mounted for security
    # Updates are handled by the Watchdog script on the host
    remote_cmd = (
        "command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh; "
        f"docker rm -f {shlex.quote(config.container_name)} || true; "
        f"docker pull {shlex.quote(config.image)} || true; "
        "mkdir -p /var/lib/iperf-agent/data; "
        f"docker run -d --name {shlex.quote(config.container_name)} "
        "--restart=always "
        f"-p {config.agent_port}:8000 "
        f"-p {config.iperf_port}:{config.iperf_port}/tcp "
        f"-p {config.iperf_port}:{config.iperf_port}/udp "
        "-v /var/lib/iperf-agent/data:/app/data "
        f"-e IPERF_PORT={config.iperf_port} "
        f"-e CONTAINER_NAME={shlex.quote(config.container_name)} "
        f"{shlex.quote(config.image)}"
    )

    return _run_ssh(config.host, remote_cmd, ssh_port=config.ssh_port)


def remove_agent_container(config: AgentConfigRead) -> str:
    remote_cmd = f"docker rm -f {shlex.quote(config.container_name)} || true"
    return _run_ssh(config.host, remote_cmd, ssh_port=config.ssh_port)


def fetch_agent_logs(config: AgentConfigRead, lines: int = 200) -> str:
    remote_cmd = f"docker logs --tail {lines} {shlex.quote(config.container_name)}"
    return _run_ssh(config.host, remote_cmd, ssh_port=config.ssh_port)

