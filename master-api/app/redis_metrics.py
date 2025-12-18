"""
Redis Metrics Collector

Collects Redis performance metrics at regular intervals and stores them
in memory for real-time charting and historical analysis.
"""

import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)


class RedisMetricsCollector:
    """
    Collects and stores Redis metrics for visualization.
    
    Maintains a rolling window of metrics data in memory using deque
    for efficient O(1) append and pop operations.
    """
    
    def __init__(self, max_points: int = 1440):
        """
        Initialize metrics collector.
        
        Args:
            max_points: Maximum number of data points to store (default: 1440 = 24h @ 1min intervals)
        """
        self.max_points = max_points
        self._data = deque(maxlen=max_points)
        self._last_collection = 0
        logger.info(f"RedisMetricsCollector initialized with max_points={max_points}")
    
    def collect_and_store(self) -> bool:
        """
        Collect current metrics and store them.
        
        Returns:
            True if collection succeeded, False otherwise
        """
        try:
            from .redis_client import get_cache_stats
            
            # Get current stats
            stats = get_cache_stats()
            
            if stats.get('status') != 'connected':
                logger.warning("Redis not connected, skipping metrics collection")
                return False
            
            # Parse memory_used (e.g., "1.04M" -> 1.04)
            memory_str = stats.get('memory_used', '0M')
            memory_mb = self._parse_memory_mb(memory_str)
            
            # Calculate ops/sec (approximate based on total commands)
            current_commands = stats.get('total_commands_processed', 0)
            current_time = time.time()
            
            ops_per_sec = 0
            if self._last_collection > 0:
                time_delta = current_time - self._last_collection
                if time_delta > 0 and len(self._data) > 0:
                    last_commands = self._data[-1]['total_commands']
                    ops_per_sec = int((current_commands - last_commands) / time_delta)
            
            self._last_collection = current_time
            
            # Create data point
            data_point = {
                'timestamp': int(current_time),
                'memory_mb': round(memory_mb, 2),
                'ops_per_sec': max(0, ops_per_sec),  # Ensure non-negative
                'hit_rate': round(stats.get('hit_rate', 0), 2),
                'total_keys': stats.get('total_keys', 0),
                'total_commands': current_commands,
                'connected_clients': stats.get('connected_clients', 0),
                'evicted_keys': stats.get('evicted_keys', 0)
            }
            
            self._data.append(data_point)
            logger.debug(f"Collected metrics: mem={memory_mb:.2f}MB, ops={ops_per_sec}, hit_rate={stats.get('hit_rate', 0):.2f}%")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to collect Redis metrics: {e}")
            return False
    
    def get_history(self, duration_seconds: int = 21600) -> Dict[str, Any]:
        """
        Get historical metrics data.
        
        Args:
            duration_seconds: Duration to retrieve (default: 21600 = 6 hours)
        
        Returns:
            Dictionary with historical data points and current stats
        """
        if not self._data:
            return {
                'status': 'no_data',
                'data_points': [],
                'count': 0
            }
        
        current_time = int(time.time())
        cutoff_time = current_time - duration_seconds
        
        # Filter data points within the requested duration
        filtered_data = [
            {
                't': dp['timestamp'],
                'mem': dp['memory_mb'],
                'ops': dp['ops_per_sec'],
                'hit': dp['hit_rate'],
                'keys': dp['total_keys'],
                'clients': dp['connected_clients']
            }
            for dp in self._data
            if dp['timestamp'] >= cutoff_time
        ]
        
        return {
            'status': 'ok',
            'data_points': filtered_data,
            'count': len(filtered_data),
            'duration': duration_seconds,
            'latest': filtered_data[-1] if filtered_data else None
        }
    
    def _parse_memory_mb(self, memory_str: str) -> float:
        """
        Parse memory string to MB.
        
        Args:
            memory_str: Memory string like "1.04M", "1024K", "1G"
        
        Returns:
            Memory in MB
        """
        if not memory_str or memory_str == 'unknown':
            return 0.0
        
        try:
            # Remove trailing unit and convert
            value = 0.0
            if memory_str.endswith('G'):
                value = float(memory_str[:-1]) * 1024
            elif memory_str.endswith('M'):
                value = float(memory_str[:-1])
            elif memory_str.endswith('K'):
                value = float(memory_str[:-1]) / 1024
            elif memory_str.endswith('B'):
                value = float(memory_str[:-1]) / (1024 * 1024)
            else:
                # Assume bytes if no unit
                value = float(memory_str) / (1024 * 1024)
            
            return round(value, 2)
        except (ValueError, IndexError):
            logger.warning(f"Failed to parse memory string: {memory_str}")
            return 0.0
    
    def get_stats_summary(self) -> Dict[str, Any]:
        """Get summary statistics."""
        if not self._data:
            return {'status': 'no_data'}
        
        return {
            'status': 'ok',
            'total_points': len(self._data),
            'oldest_timestamp': self._data[0]['timestamp'] if self._data else None,
            'newest_timestamp': self._data[-1]['timestamp'] if self._data else None,
            'coverage_hours': round((self._data[-1]['timestamp'] - self._data[0]['timestamp']) / 3600, 1) if len(self._data) > 1 else 0
        }


# Global instance
_metrics_collector: Optional[RedisMetricsCollector] = None


def get_metrics_collector() -> RedisMetricsCollector:
    """Get or create the global metrics collector instance."""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = RedisMetricsCollector()
    return _metrics_collector
