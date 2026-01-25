import { apiFetch } from './api.js';


let autoRefreshInterval = null;

// Fetch and display Redis stats
async function loadStats() {
  const refreshIcon = document.getElementById('refresh-icon');
  refreshIcon.classList.add('animate-spin');

  try {
    const response = await apiFetch('/api/cache/stats');
    const data = await response.json();

    document.getElementById('loading-state').classList.add('hidden');
    document.getElementById('error-state').classList.add('hidden');
    document.getElementById('main-content').classList.remove('hidden');

    // Status
    if (data.status === 'connected') {
      document.getElementById('status-badge').className = 'px-3 py-1 rounded-full text-xs font-semibold bg-emerald-500/20 text-emerald-400';
      document.getElementById('status-badge').textContent = '在线';
      document.getElementById('status-text').textContent = '已连接';
      document.getElementById('status-text').className = 'text-2xl font-bold text-emerald-400';
    } else {
      document.getElementById('status-badge').className = 'px-3 py-1 rounded-full text-xs font-semibold bg-red-500/20 text-red-400';
      document.getElementById('status-badge').textContent = '离线';
      document.getElementById('status-text').textContent = '未连接';
      document.getElementById('status-text').className = 'text-2xl font-bold text-red-400';
    }

    // Hit Rate
    const hitRate = data.hit_rate || 0;
    document.getElementById('hit-rate').textContent = hitRate.toFixed(2) + '%';
    document.getElementById('hit-count').textContent = (data.keyspace_hits || 0).toLocaleString();
    document.getElementById('miss-count').textContent = (data.keyspace_misses || 0).toLocaleString();

    // Trend indicator
    if (hitRate >= 90) {
      document.getElementById('hit-rate-trend').textContent = '🔥';
    } else if (hitRate >= 70) {
      document.getElementById('hit-rate-trend').textContent = '✅';
    } else if (hitRate >= 50) {
      document.getElementById('hit-rate-trend').textContent = '⚠️';
    } else {
      document.getElementById('hit-rate-trend').textContent = '📉';
    }

    // Memory
    document.getElementById('memory-used').textContent = data.memory_used || 'N/A';
    document.getElementById('memory-peak').textContent = data.memory_peak || 'N/A';

    // Calculate memory percentage (rough estimate based on maxmemory 256MB)
    const memUsed = data.memory_used || '0M';
    const memMatch = memUsed.match(/(\d+\.?\d*)/);
    if (memMatch) {
      const memMB = parseFloat(memMatch[1]);
      const memPercent = (memMB / 256 * 100).toFixed(1);
      document.getElementById('memory-percent').textContent = memPercent + '%';
      document.getElementById('memory-bar').style.width = Math.min(memPercent, 100) + '%';

      // Color code the bar
      const bar = document.getElementById('memory-bar');
      if (memPercent > 90) {
        bar.className = 'progress-bar bg-gradient-to-r from-red-500 to-orange-500 h-2 rounded-full';
      } else if (memPercent > 70) {
        bar.className = 'progress-bar bg-gradient-to-r from-amber-500 to-yellow-500 h-2 rounded-full';
      } else {
        bar.className = 'progress-bar bg-gradient-to-r from-blue-500 to-cyan-500 h-2 rounded-full';
      }
    }

    // Keys
    document.getElementById('total-keys').textContent = (data.total_keys || 0).toLocaleString();
    document.getElementById('evicted-keys').textContent = (data.evicted_keys || 0).toLocaleString();
    document.getElementById('expired-keys').textContent = (data.expired_keys || 0).toLocaleString();

    // Performance
    document.getElementById('total-connections').textContent = (data.total_connections_received || 0).toLocaleString();
    document.getElementById('connected-clients').textContent = data.connected_clients || 0;
    document.getElementById('total-commands').textContent = (data.total_commands_processed || 0).toLocaleString();

    // Last updated
    document.getElementById('last-updated').textContent = new Date().toLocaleString('zh-CN');

  } catch (error) {
    console.error('Error loading stats:', error);
    document.getElementById('loading-state').classList.add('hidden');
    document.getElementById('main-content').classList.add('hidden');
    document.getElementById('error-state').classList.remove('hidden');
    document.getElementById('error-message').textContent = error.message;
  } finally {
    refreshIcon.classList.remove('animate-spin');
  }
}

// Clear nodes cache
async function clearNodesCache() {
  if (!confirm('确定要清除节点缓存吗？')) return;

  try {
    const response = await apiFetch('/api/cache/clear?pattern=nodes:*', {
      method: 'POST'
    });
    const data = await response.json();
    alert(`成功清除 ${data.cleared} 个缓存键`);
    loadStats();
  } catch (error) {
    alert('清除失败: ' + error.message);
  }
}

// Clear all cache
async function clearAllCache() {
  if (!confirm('⚠️ 警告：确定要清除所有缓存吗？这将影响系统性能！')) return;

  try {
    const response = await apiFetch('/api/cache/clear?all=true', {
      method: 'POST'
    });
    const data = await response.json();
    alert(data.message || '缓存已清除');
    loadStats();
  } catch (error) {
    alert('清除失败: ' + error.message);
  }
}

// Warmup cache
async function warmupCache() {
  const btn = document.getElementById('warmup-btn');
  const originalText = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<div class="inline-block w-5 h-5 border-2 border-white border-t-transparent rounded-full animate-spin"></div> 预热中...';

  try {
    const response = await apiFetch('/api/cache/warmup', {
      method: 'POST'
    });
    const data = await response.json();
    alert(data.message || `缓存预热完成，${data.warmed_entries} 个条目已加载`);
    loadStats();
  } catch (error) {
    alert('预热失败: ' + error.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalText;
  }
}

// Auto-refresh toggle
document.getElementById('auto-refresh-toggle').addEventListener('change', (e) => {
  if (e.target.checked) {
    autoRefreshInterval = setInterval(loadStats, 30000);
  } else {
    if (autoRefreshInterval) {
      clearInterval(autoRefreshInterval);
      autoRefreshInterval = null;
    }
  }
});

// Event listeners
document.getElementById('refresh-btn').addEventListener('click', loadStats);
document.getElementById('clear-nodes-btn').addEventListener('click', clearNodesCache);
document.getElementById('clear-all-btn').addEventListener('click', clearAllCache);
document.getElementById('warmup-btn').addEventListener('click', warmupCache);

// Initial load
loadStats();
