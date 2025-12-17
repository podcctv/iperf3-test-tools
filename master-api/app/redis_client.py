"""
Redis Client Module for Caching

Provides simple get/set operations with TTL support for performance optimization.
"""

import os
import json
import logging
from typing import Optional, Any

logger = logging.getLogger(__name__)

# Redis client (initialized on first use)
_redis_client = None


def get_redis_client():
    """Get or create Redis client instance."""
    global _redis_client
    
    if _redis_client is None:
        try:
            import redis
            redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
            _redis_client = redis.from_url(redis_url, decode_responses=True)
            # Test connection
            _redis_client.ping()
            logger.info(f"Redis connected: {redis_url}")
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
        ttl: Time to live in seconds (default: 60)
    
    Returns:
        True if successful, False otherwise
    """
    try:
        client = get_redis_client()
        if client is None:
            return False
        
        serialized = json.dumps(value)
        client.setex(key, ttl, serialized)
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
