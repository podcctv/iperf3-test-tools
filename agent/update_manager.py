"""
Agent Auto-Update Manager

Handles self-updating of agent containers with:
- Multi-master version coordination (only upgrade, never downgrade)
- Config preservation (ports, reverse mode settings)
- Docker-based container replacement
"""

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any
import re

# Version comparison using semantic versioning
def parse_version(version_str: str) -> tuple:
    """Parse version string like '1.2.3' into tuple (1, 2, 3)"""
    if not version_str:
        return (0, 0, 0)
    # Remove 'v' prefix if present
    version_str = version_str.lstrip('v')
    parts = version_str.split('.')
    result = []
    for part in parts[:3]:
        # Extract numeric part
        match = re.match(r'(\d+)', part)
        result.append(int(match.group(1)) if match else 0)
    while len(result) < 3:
        result.append(0)
    return tuple(result)


def compare_versions(v1: str, v2: str) -> int:
    """
    Compare two version strings.
    Returns: 1 if v1 > v2, -1 if v1 < v2, 0 if equal
    """
    t1 = parse_version(v1)
    t2 = parse_version(v2)
    if t1 > t2:
        return 1
    elif t1 < t2:
        return -1
    return 0


class UpdateManager:
    """Manages agent self-update process"""
    
    def __init__(self, current_version: str):
        self.current_version = current_version
        self.config_path = Path("/app/config.json")
        self.update_lock = threading.Lock()
        self.is_updating = False
        self.last_update_check = 0
        self.update_cooldown = 300  # 5 minutes between update attempts
        
        # Track requested versions from different masters
        self.master_versions: Dict[str, str] = {}
        
    def get_current_config(self) -> Dict[str, Any]:
        """Get current agent configuration for preservation"""
        config = {
            "iperf_port": int(os.environ.get("IPERF_PORT", "62001")),
            "agent_port": int(os.environ.get("AGENT_API_PORT", "8000")),
            "master_url": os.environ.get("MASTER_URL", ""),
            "node_name": os.environ.get("NODE_NAME", ""),
            "agent_mode": os.environ.get("AGENT_MODE", "normal"),
            "poll_interval": int(os.environ.get("POLL_INTERVAL", "10")),
        }
        
        # Also load from config file if exists
        if self.config_path.exists():
            try:
                file_config = json.loads(self.config_path.read_text())
                for key in ["master_url", "node_name", "iperf_port", "agent_mode", "poll_interval"]:
                    if key in file_config and not config.get(key):
                        config[key] = file_config[key]
            except Exception:
                pass
                
        return config
    
    def save_config(self, config: Dict[str, Any]) -> bool:
        """Save configuration to file for persistence across updates"""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(config, indent=2))
            return True
        except Exception as e:
            print(f"[UPDATE] Failed to save config: {e}", flush=True)
            return False
    
    def register_master_version(self, master_url: str, requested_version: str) -> None:
        """Register the version requested by a master"""
        self.master_versions[master_url] = requested_version
        print(f"[UPDATE] Master {master_url} requests version {requested_version}", flush=True)
    
    def get_highest_requested_version(self) -> str:
        """Get the highest version requested by any master"""
        if not self.master_versions:
            return self.current_version
        
        highest = self.current_version
        for version in self.master_versions.values():
            if compare_versions(version, highest) > 0:
                highest = version
        return highest
    
    def should_update(self, target_version: str) -> tuple[bool, str]:
        """
        Determine if update should proceed.
        Returns: (should_update, reason)
        """
        # Check cooldown
        now = time.time()
        if now - self.last_update_check < self.update_cooldown:
            return False, "cooldown_active"
        
        # Check if already updating
        if self.is_updating:
            return False, "update_in_progress"
        
        # Compare versions - only upgrade, never downgrade
        comparison = compare_versions(target_version, self.current_version)
        if comparison <= 0:
            return False, "already_at_or_higher_version"
        
        # Check if a higher version is requested by another master
        highest = self.get_highest_requested_version()
        if compare_versions(target_version, highest) < 0:
            return False, f"higher_version_{highest}_requested"
        
        return True, "upgrade_available"
    
    def check_docker_available(self) -> bool:
        """Check if Docker is accessible from within the container"""
        try:
            result = subprocess.run(
                ["docker", "version"],
                capture_output=True,
                timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def pull_image(self, image: str) -> bool:
        """Pull the new agent image"""
        print(f"[UPDATE] Pulling image: {image}", flush=True)
        try:
            result = subprocess.run(
                ["docker", "pull", image],
                capture_output=True,
                text=True,
                timeout=300
            )
            if result.returncode != 0:
                print(f"[UPDATE] Pull failed: {result.stderr}", flush=True)
                return False
            return True
        except Exception as e:
            print(f"[UPDATE] Pull exception: {e}", flush=True)
            return False
    
    def execute_update(self, target_version: str, image: str) -> Dict[str, Any]:
        """
        Execute the self-update process.
        
        This creates an update script that will:
        1. Save current config
        2. Pull new image
        3. Stop current container
        4. Start new container with same config
        """
        with self.update_lock:
            if self.is_updating:
                return {"status": "skipped", "reason": "update_in_progress"}
            
            should_update, reason = self.should_update(target_version)
            if not should_update:
                return {"status": "skipped", "reason": reason}
            
            self.is_updating = True
            self.last_update_check = time.time()
        
        try:
            # Save current config
            config = self.get_current_config()
            if not self.save_config(config):
                self.is_updating = False
                return {"status": "failed", "reason": "config_save_failed"}
            
            # Check Docker access
            if not self.check_docker_available():
                self.is_updating = False
                return {"status": "failed", "reason": "docker_not_available"}
            
            # Pull new image
            if not self.pull_image(image):
                self.is_updating = False
                return {"status": "failed", "reason": "image_pull_failed"}
            
            # Get current container name
            container_name = os.environ.get("CONTAINER_NAME", "iperf-agent")
            
            # Create update script that will be executed after API response
            update_script = f"""#!/bin/bash
set -e
sleep 2  # Give time for API response
echo "[UPDATE] Stopping current container..."
docker stop {container_name} || true
docker rm {container_name} || true

echo "[UPDATE] Starting new container..."
docker run -d --name {container_name} \\
  --restart=always \\
  -v /var/run/docker.sock:/var/run/docker.sock \\
  -v /app/config.json:/app/config.json \\
  -v /app/data:/app/data \\
  -p {config['agent_port']}:8000 \\
  -p {config['iperf_port']}:{config['iperf_port']}/tcp \\
  -p {config['iperf_port']}:{config['iperf_port']}/udp \\
  -e AGENT_MODE={config['agent_mode']} \\
  -e MASTER_URL={config['master_url']} \\
  -e NODE_NAME={config['node_name']} \\
  -e IPERF_PORT={config['iperf_port']} \\
  -e POLL_INTERVAL={config['poll_interval']} \\
  -e CONTAINER_NAME={container_name} \\
  {image}

echo "[UPDATE] Update complete"
"""
            
            # Write update script
            script_path = Path("/tmp/agent_update.sh")
            script_path.write_text(update_script)
            script_path.chmod(0o755)
            
            # Execute update script in background
            subprocess.Popen(
                ["/bin/bash", str(script_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            
            return {
                "status": "updating",
                "current_version": self.current_version,
                "target_version": target_version,
                "config_preserved": config
            }
            
        except Exception as e:
            self.is_updating = False
            return {"status": "failed", "reason": str(e)}


# Global update manager instance (initialized when imported)
_update_manager: Optional[UpdateManager] = None


def get_update_manager(current_version: str) -> UpdateManager:
    """Get or create the global update manager instance"""
    global _update_manager
    if _update_manager is None:
        _update_manager = UpdateManager(current_version)
    return _update_manager
