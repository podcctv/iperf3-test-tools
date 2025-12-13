"""
IP Whitelist Management Module for Agent

Manages allowed IP addresses for iperf3 test requests.
Only IPs in the whitelist can execute tests through this agent.
"""

import os
import logging
from typing import List, Set
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

class IPWhitelist:
    """Thread-safe IP whitelist manager"""
    
    def __init__(self, whitelist_file: str = "/app/data/ip_whitelist.txt"):
        self.whitelist_file = Path(whitelist_file)
        self._allowed_ips: Set[str] = set()
        self._lock = Lock()
        
        # Ensure data directory exists
        self.whitelist_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Load whitelist from file or environment
        self.reload()
    
    def reload(self) -> None:
        """Reload whitelist from file and environment variable"""
        with self._lock:
            self._allowed_ips.clear()
            
            # Load from environment variable (comma-separated)
            env_ips = os.getenv("ALLOWED_IPS", "")
            if env_ips:
                for ip in env_ips.split(","):
                    ip = ip.strip()
                    if ip:
                        self._allowed_ips.add(ip)
                logger.info(f"Loaded {len(self._allowed_ips)} IPs from environment")
            
            # Load from file (one IP per line)
            if self.whitelist_file.exists():
                try:
                    with open(self.whitelist_file, 'r') as f:
                        for line in f:
                            ip = line.strip()
                            if ip and not ip.startswith('#'):
                                self._allowed_ips.add(ip)
                    logger.info(f"Loaded {len(self._allowed_ips)} IPs from {self.whitelist_file}")
                except Exception as e:
                    logger.error(f"Failed to load whitelist from file: {e}")
            
            # Always allow localhost
            self._allowed_ips.add("127.0.0.1")
            self._allowed_ips.add("::1")
            
            logger.info(f"Total allowed IPs: {len(self._allowed_ips)}")
    
    def update(self, ips: List[str]) -> None:
        """Update whitelist with new IP list (REPLACE mode - clears existing)"""
        with self._lock:
            self._allowed_ips.clear()
            
            # Add new IPs with validation
            for ip in ips:
                ip = ip.strip()
                if ip and self._is_valid_ip(ip):
                    self._allowed_ips.add(ip)
                elif ip:
                    logger.warning(f"Skipping invalid IP address: {ip}")
            
            # Always allow localhost
            self._allowed_ips.add("127.0.0.1")
            self._allowed_ips.add("::1")
            
            # Save to file
            self._save_to_file()
            logger.info(f"Updated whitelist with {len(self._allowed_ips)} IPs (replace mode)")
    
    def merge(self, ips: List[str]) -> dict:
        """Merge IPs into whitelist (APPEND mode - keeps existing IPs)
        
        This is designed for multi-Master scenarios where multiple Masters
        share the same Agent. Each Master can add its IPs without removing
        IPs added by other Masters.
        
        Returns:
            dict with added count and skipped count
        """
        added = 0
        skipped = 0
        
        with self._lock:
            for ip in ips:
                ip = ip.strip()
                if not ip:
                    continue
                    
                if ip in self._allowed_ips:
                    skipped += 1
                    continue
                    
                if self._is_valid_ip(ip):
                    self._allowed_ips.add(ip)
                    added += 1
                    logger.info(f"Added IP to whitelist: {ip}")
                else:
                    logger.warning(f"Skipping invalid IP address: {ip}")
            
            # Save to file if any changes
            if added > 0:
                self._save_to_file()
        
        logger.info(f"Merged whitelist: {added} added, {skipped} already existed")
        return {"added": added, "skipped": skipped, "total": len(self._allowed_ips)}
    
    def remove_ips(self, ips: List[str]) -> dict:
        """Remove multiple IPs from whitelist
        
        Returns:
            dict with removed count and not_found count
        """
        removed = 0
        not_found = 0
        
        with self._lock:
            for ip in ips:
                ip = ip.strip()
                if not ip:
                    continue
                    
                # Don't allow removing localhost
                if ip in ["127.0.0.1", "::1"]:
                    logger.warning(f"Cannot remove localhost from whitelist: {ip}")
                    continue
                    
                if ip in self._allowed_ips:
                    self._allowed_ips.discard(ip)
                    removed += 1
                    logger.info(f"Removed IP from whitelist: {ip}")
                else:
                    not_found += 1
            
            # Save to file if any changes
            if removed > 0:
                self._save_to_file()
        
        logger.info(f"Removed from whitelist: {removed} removed, {not_found} not found")
        return {"removed": removed, "not_found": not_found, "total": len(self._allowed_ips)}
    
    def _save_to_file(self) -> None:
        """Save current whitelist to file"""
        try:
            with open(self.whitelist_file, 'w') as f:
                f.write("# IP Whitelist - Auto-generated by Master\n")
                f.write("# Supports multiple Masters in append mode\n\n")
                for ip in sorted(self._allowed_ips):
                    if ip not in ["127.0.0.1", "::1"]:  # Don't write localhost
                        f.write(f"{ip}\n")
        except Exception as e:
            logger.error(f"Failed to save whitelist to file: {e}")
    
    def _is_valid_ip(self, ip: str) -> bool:
        """Validate IP address or domain name format"""
        import ipaddress
        import re
        
        # Check for valid IP address
        try:
            ipaddress.ip_address(ip)
            return True
        except ValueError:
            pass
        
        # Check if it's a CIDR notation
        try:
            ipaddress.ip_network(ip, strict=False)
            return True
        except ValueError:
            pass
        
        # Check if it's a valid domain name
        domain_pattern = r'^([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$'
        if re.match(domain_pattern, ip):
            return True
        
        return False
    
    def is_allowed(self, ip: str) -> bool:
        """Check if an IP is in the whitelist (supports CIDR matching)
        
        Private/internal IPs (RFC1918) are automatically allowed:
        - 10.0.0.0/8
        - 172.16.0.0/12
        - 192.168.0.0/16
        """
        import ipaddress
        
        # Normalize IPv4-mapped IPv6 addresses (e.g., ::ffff:192.168.1.1 -> 192.168.1.1)
        original_ip = ip
        try:
            ip_obj = ipaddress.ip_address(ip)
            if ip_obj.version == 6 and ip_obj.ipv4_mapped:
                ip = str(ip_obj.ipv4_mapped)
                logger.debug(f"Normalized IPv4-mapped IPv6: {original_ip} -> {ip}")
        except ValueError:
            pass  # Not a valid IP, will be handled below
        
        # Auto-allow private/internal IPs (RFC1918)
        try:
            ip_obj = ipaddress.ip_address(ip)
            if ip_obj.is_private:
                logger.info(f"[WHITELIST] Auto-allowed private IP: {ip}")
                return True
        except ValueError:
            pass
        

        with self._lock:
            # Direct match
            if ip in self._allowed_ips:
                return True
            
            # Also check original IP in case it was in the whitelist as IPv6
            if original_ip != ip and original_ip in self._allowed_ips:
                return True
            
            # Check CIDR ranges
            try:
                ip_obj = ipaddress.ip_address(ip)
                for allowed in self._allowed_ips:
                    try:
                        # Check if it's a network (CIDR)
                        network = ipaddress.ip_network(allowed, strict=False)
                        if ip_obj in network:
                            return True
                    except ValueError:
                        # Not a network, skip
                        continue
            except ValueError:
                # Invalid IP format
                logger.warning(f"Invalid IP address format: {ip}")
                return False
            
            return False
    
    def get_all(self) -> List[str]:
        """Get all allowed IPs"""
        with self._lock:
            return sorted(list(self._allowed_ips))
    
    def add(self, ip: str) -> None:
        """Add a single IP to whitelist"""
        with self._lock:
            self._allowed_ips.add(ip.strip())
            logger.info(f"Added IP to whitelist: {ip}")
    
    def remove(self, ip: str) -> None:
        """Remove a single IP from whitelist"""
        with self._lock:
            self._allowed_ips.discard(ip.strip())
            logger.info(f"Removed IP from whitelist: {ip}")
    
    def get_statistics(self) -> dict:
        """Get whitelist statistics"""
        with self._lock:
            total_ips = len(self._allowed_ips)
            
            # Count IPv4 vs IPv6
            ipv4_count = 0
            ipv6_count = 0
            cidr_count = 0
            
            import ipaddress
            for ip_str in self._allowed_ips:
                try:
                    # Check if it's a network (CIDR)
                    network = ipaddress.ip_network(ip_str, strict=False)
                    if '/' in ip_str:
                        cidr_count += 1
                        if network.version == 4:
                            ipv4_count += 1
                        else:
                            ipv6_count += 1
                    else:
                        # Single IP
                        ip_obj = ipaddress.ip_address(ip_str)
                        if ip_obj.version == 4:
                            ipv4_count += 1
                        else:
                            ipv6_count += 1
                except ValueError:
                    pass
            
            return {
                "total": total_ips,
                "ipv4": ipv4_count,
                "ipv6": ipv6_count,
                "cidr_ranges": cidr_count,
                "file_path": str(self.whitelist_file),
                "file_exists": self.whitelist_file.exists()
            }


# Global whitelist instance
whitelist = IPWhitelist()
