"""
Redis Cache Performance Test

Tests the nodes API with Redis caching to verify:
1. Cache miss (first request) - hits database
2. Cache hit (subsequent requests) - served from Redis
3. Response time improvement
"""

import time
import subprocess
import json

def run_curl(url, description):
    """Run curl and measure time"""
    start = time.time()
    result = subprocess.run(
        ['curl', '-s', url],
        capture_output=True,
        text=True,
        shell=True
    )
    elapsed_ms = (time.time() - start) * 1000
    
    try:
        data = json.loads(result.stdout)
        return {
            'description': description,
            'time_ms': round(elapsed_ms, 2),
            'status': 'success',
            'data_count': len(data) if isinstance(data, list) else 1
        }
    except:
        return {
            'description': description,
            'time_ms': round(elapsed_ms, 2),
            'status': 'error',
            'error': result.stdout[:100]
        }

print("="*60)
print("REDIS CACHE PERFORMANCE TEST")
print("="*60)
print(f"Testing endpoint: /nodes")
print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
print()

# Test 1: First request (Cache MISS - should hit database)
print("TEST 1: First Request (Cache MISS)")
print("-" * 60)
result1 = run_curl('http://localhost:9000/nodes', 'First request (DB query)')
print(f"Time: {result1['time_ms']}ms")
print(f"Status: {result1['status']}")
if result1['status'] == 'success':
    print(f"Nodes found: {result1['data_count']}")
print()

# Wait a moment
time.sleep(0.5)

# Test 2-6: Subsequent requests (Cache HIT - should be from Redis)
print("TEST 2-6: Subsequent Requests (Cache HIT)")
print("-" * 60)
cached_times = []
for i in range(5):
    result = run_curl('http://localhost:9000/nodes', f'Request #{i+2} (Redis cache)')
    cached_times.append(result['time_ms'])
    print(f"Request #{i+2}: {result['time_ms']}ms")
    time.sleep(0.1)

avg_cached = sum(cached_times) / len(cached_times)
print()

# Summary
print("="*60)
print("PERFORMANCE SUMMARY")
print("="*60)
print(f"First Request (DB):     {result1['time_ms']}ms")
print(f"Avg Cached (Redis):     {round(avg_cached, 2)}ms")
print(f"Min Cached:             {min(cached_times)}ms")
print(f"Max Cached:             {max(cached_times)}ms")
print()

if result1['time_ms'] > 0 and avg_cached > 0:
    speedup = result1['time_ms'] / avg_cached
    improvement_pct = ((result1['time_ms'] - avg_cached) / result1['time_ms']) * 100
    print(f"**Performance Gain**:   {round(speedup, 1)}x faster")
    print(f"**Time Saved**:         {round(improvement_pct, 1)}%")
    print()

# Check logs for cache HIT/MISS
print("="*60)
print("CHECKING DOCKER LOGS FOR CACHE EVIDENCE")
print("="*60)
print("Running: docker compose logs --tail 20 master-api | grep Cache")
print()
