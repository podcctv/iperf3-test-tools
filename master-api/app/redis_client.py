"""
Redis Client Module for Caching

Provides optimized caching operations with connection pooling, batch operations,
and comprehensive statistics for performance optimization.
"""

import os
import json
import logging
from typing import Optional, Any, Dict, List

logger = logging.getLogger(__name__)

# Redis client with connection pool (initialized on first use)
_redis_client = None
_connection_pool = None


def get_redis_client():
    """Get or create Redis client instance with connection pooling."""
    global _redis_client, _connection_pool
    
    if _redis_client is None:
        try:
            import redis
            redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
            
            # Create connection pool for better performance
            if _connection_pool is None:
                _connection_pool = redis.ConnectionPool.from_url(
                    redis_url,
                    max_connections=50,
                    socket_keepalive=True,
                    socket_keepalive_options={},
                    socket_connect_timeout=5,
                    socket_timeout=5,
                    retry_on_timeout=True,
                    health_check_interval=30,
                    decode_responses=True
                )
            
            _redis_client = redis.Redis(connection_pool=_connection_pool)
            
            # Test connection
            _redis_client.ping()
            logger.info(f"Redis connected with connection pool: {redis_url}")
        except Exception as e:
            logger.warning(f"Redis connection failed: {e}. Caching disabled.")
            _redis_client = None
    
    return _redis_client


def cache_set(key: str, value: Any, ttl: int = 60) -> bool:
    """
    Set a cache value with TTL (time-to-live) in seconds.
    
    Args:
        key: Cache key
        value: Value to cache (will be JSON-serialized)
        ttl: Time to live in seconds (default: 60, 0 = no expiration)
    
    Returns:
        True if successful, False otherwise
    """
    try:
        client = get_redis_client()
        if client is None:
            return False
        
        serialized = json.dumps(value, separators=(',', ':'))  # Compact JSON
        
        if ttl > 0:
            client.setex(key, ttl, serialized)
        else:
            client.set(key, serialized)
        return True
    except Exception as e:
        logger.debug(f"Cache set failed for key {key}: {e}")
        return False


def cache_get(key: str) -> Optional[Any]:
    """
    Get a cached value.
    
    Args:
        key: Cache key
    
    Returns:
        Cached value (JSON-deserialized) or None if not found/expired
    """
    try:
        client = get_redis_client()
        if client is None:
            return None
        
        data = client.get(key)
        if data is None:
            return None
        
        return json.loads(data)
    except Exception as e:
        logger.debug(f"Cache get failed for key {key}: {e}")
        return None


def cache_delete(key: str) -> bool:
    """
    Delete a cache entry.
    
    Args:
        key: Cache key
    
    Returns:
        True if successful, False otherwise
    """
    try:
        client = get_redis_client()
        if client is None:
            return False
        
        client.delete(key)
        return True
    except Exception as e:
        logger.debug(f"Cache delete failed for key {key}: {e}")
        return False


def cache_clear_pattern(pattern: str) -> int:
    """
    Clear all cache keys matching a pattern.
    
    Args:
        pattern: Redis pattern (e.g. "nodes:*")
    
    Returns:
        Number of keys deleted
    """
    try:
        client = get_redis_client()
        if client is None:
            return 0
        
        keys = client.keys(pattern)
        if keys:
            return client.delete(*keys)
        return 0
    except Exception as e:
        logger.debug(f"Cache clear pattern failed for {pattern}: {e}")
        return 0


def cache_get_multi(keys: List[str]) -> Dict[str, Any]:
    """
    Get multiple cached values in a single operation.
    
    Args:
        keys: List of cache keys
    
    Returns:
        Dictionary of key-value pairs (only keys that exist)
    """
    try:
        client = get_redis_client()
        if client is None or not keys:
            return {}
        
        values = client.mget(keys)
        result = {}
        for key, value in zip(keys, values):
            if value is not None:
                try:
                    result[key] = json.loads(value)
                except json.JSONDecodeError:
                    logger.debug(f"Failed to decode cached value for key {key}")
        return result
    except Exception as e:
        logger.debug(f"Cache multi-get failed: {e}")
        return {}


def cache_set_multi(items: Dict[str, Any], ttl: int = 60) -> bool:
    """
    Set multiple cache values in a single operation.
    
    Args:
        items: Dictionary of key-value pairs to cache
        ttl: Time to live in seconds (default: 60)
    
    Returns:
        True if successful, False otherwise
    """
    try:
        client = get_redis_client()
        if client is None or not items:
            return False
        
        pipeline = client.pipeline()
        for key, value in items.items():
            serialized = json.dumps(value, separators=(',', ':'))
            if ttl > 0:
                pipeline.setex(key, ttl, serialized)
            else:
                pipeline.set(key, serialized)
        pipeline.execute()
        return True
    except Exception as e:
        logger.debug(f"Cache multi-set failed: {e}")
        return False


def cache_exists(key: str) -> bool:
    """
    Check if a cache key exists.
    
    Args:
        key: Cache key
    
    Returns:
        True if key exists, False otherwise
    """
    try:
        client = get_redis_client()
        if client is None:
            return False
        return client.exists(key) > 0
    except Exception as e:
        logger.debug(f"Cache exists check failed for key {key}: {e}")
        return False


def cache_ttl(key: str) -> int:
    """
    Get remaining TTL for a cache key.
    
    Args:
        key: Cache key
    
    Returns:
        Remaining TTL in seconds, -1 if no expiration, -2 if key doesn't exist
    """
    try:
        client = get_redis_client()
        if client is None:
            return -2
        return client.ttl(key)
    except Exception as e:
        logger.debug(f"Cache TTL check failed for key {key}: {e}")
        return -2


def get_cache_stats() -> Dict[str, Any]:
    """
    Get comprehensive cache statistics.
    
    Returns:
        Dictionary with cache statistics including hits, misses, memory usage, etc.
    """
    try:
        client = get_redis_client()
        if client is None:
            return {"status": "disconnected", "enabled": False}
        
        info = client.info()
        stats_info = info.get('stats', {})
        memory_info = info.get('memory', {})
        
        hits = stats_info.get('keyspace_hits', 0)
        misses = stats_info.get('keyspace_misses', 0)
        total_requests = hits + misses
        hit_rate = (hits / total_requests * 100) if total_requests > 0 else 0
        
        # Get total keys count
        total_keys = 0
        keyspace_info = {k: v for k, v in info.items() if k.startswith('db')}
        for db_info in keyspace_info.values():
            if isinstance(db_info, dict):
                total_keys += db_info.get('keys', 0)
        
        return {
            "status": "connected",
            "enabled": True,
            "keyspace_hits": hits,
            "keyspace_misses": misses,
            "total_requests": total_requests,
            "hit_rate": round(hit_rate, 2),
            "total_keys": total_keys,
            "memory_used": memory_info.get('used_memory_human', 'unknown'),
            "memory_peak": memory_info.get('used_memory_peak_human', 'unknown'),
            "connected_clients": info.get('connected_clients', 0),
            "total_connections_received": stats_info.get('total_connections_received', 0),
            "total_commands_processed": stats_info.get('total_commands_processed', 0),
            "evicted_keys": stats_info.get('evicted_keys', 0),
            "expired_keys": stats_info.get('expired_keys', 0)
        }
    except Exception as e:
        logger.error(f"Failed to get cache stats: {e}")
        return {"status": "error", "enabled": False, "error": str(e)}


def cache_clear_all() -> bool:
    """
    Clear all cache entries. Use with caution!
    
    Returns:
        True if successful, False otherwise
    """
    try:
        client = get_redis_client()
        if client is None:
            return False
        
        client.flushdb()
        logger.info("All cache entries cleared (FLUSHDB)")
        return True
    except Exception as e:
        logger.error(f"Cache clear all failed: {e}")
        return False
