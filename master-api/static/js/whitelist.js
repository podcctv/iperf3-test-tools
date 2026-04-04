import { apiFetch } from './api.js';





function showAlert(message, type = 'info') {
  const alert = document.getElementById('alert-box');
  alert.className = 'mb-6 p-4 rounded-lg border';

  if (type === 'success') {
    alert.classList.add('border-emerald-500', 'bg-emerald-500/10', 'text-emerald-100');
  } else if (type === 'error') {
    alert.classList.add('border-rose-500', 'bg-rose-500/10', 'text-rose-100');
  } else {
    alert.classList.add('border-blue-500', 'bg-blue-500/10', 'text-blue-100');
  }

  alert.textContent = message;
  alert.classList.remove('hidden');

  setTimeout(() => alert.classList.add('hidden'), 5000);
}

async function loadWhitelist() {
  const tbody = document.getElementById('whitelist-table-body');
  if (!tbody) return;

  try {
    const res = await apiFetch('/admin/whitelist');
    const data = await res.json();

    if (!data.whitelist || data.whitelist.length === 0) {
      tbody.innerHTML = `
            <tr>
              <td colspan="5" class="px-4 py-8 text-center text-slate-500 wl-help">
                暂无白名单 IP，点击上方"添加"按钮开始
              </td>
            </tr>
          `;
      document.getElementById('whitelist-total').textContent = '0';
      document.getElementById('whitelist-cidr-count').textContent = '0';
      return;
    }

    // Update stats
    document.getElementById('whitelist-total').textContent = data.count || data.whitelist.length;
    const cidrCount = data.whitelist.filter(ip => ip.includes('/')).length;
    document.getElementById('whitelist-cidr-count').textContent = cidrCount;

    // Render table rows
    tbody.innerHTML = data.whitelist.map(ip => {
      const nodeInfo = data.nodes?.find(n => n.ip === ip);
      const isCIDR = ip.includes('/');
      const isIPv6 = ip.includes(':');

      let ipType = 'IPv4';
      if (isCIDR) ipType = 'CIDR';
      else if (isIPv6) ipType = 'IPv6';

      let source = nodeInfo ? `节点: ${nodeInfo.name}` : '手动添加';

      let syncStatusHtml = '<span class="text-slate-500">-</span>';
      if (nodeInfo) {
        if (!nodeInfo.whitelist_sync_status || nodeInfo.whitelist_sync_status === 'unknown') {
          syncStatusHtml = '<span class="text-slate-500 flex items-center gap-1">❓ 未检查</span>';
        } else if (nodeInfo.whitelist_sync_status === 'synced') {
          syncStatusHtml = '<span class="text-emerald-400 flex items-center gap-1">✅ 已同步</span>';
        } else if (nodeInfo.whitelist_sync_status === 'not_synced') {
          syncStatusHtml = '<span class="text-yellow-400 flex items-center gap-1">⚠️ 未同步</span>';
        } else {
          const errorMsg = nodeInfo.whitelist_sync_message || '未知错误';
          syncStatusHtml = `<span class="text-rose-400 flex items-center gap-1">❌ ${errorMsg}</span>`;
        }
      }

      const typeClass = isCIDR ? 'bg-purple-500/20 text-purple-300' :
        isIPv6 ? 'bg-blue-500/20 text-blue-300' :
          'bg-emerald-500/20 text-emerald-300';

      return `
            <tr class="wl-table-row transition border-b border-slate-800">
              <td class="px-4 py-3">
                <code class="text-sm font-mono text-sky-300">${ip}</code>
              </td>
              <td class="px-4 py-3 text-slate-400 text-xs wl-help">${source}</td>
              <td class="px-4 py-3 text-xs">${syncStatusHtml}</td>
              <td class="px-4 py-3">
                <span class="inline-flex items-center px-2 py-1 rounded-md text-xs font-semibold ${typeClass}">${ipType}</span>
              </td>
              <td class="px-4 py-3 text-right">
                <button 
                  onclick="removeIP('${ip}')" 
                  class="px-3 py-1 rounded-lg border border-rose-700 bg-rose-900/20 text-xs font-semibold text-rose-300 hover:bg-rose-900/40 transition">
                  删除
                </button>
              </td>
            </tr>
          `;
    }).join('');

  } catch (e) {
    showAlert(`获取白名单失败: ${e.message}`, 'error');
    tbody.innerHTML = `
          <tr>
            <td colspan="5" class="px-4 py-8 text-center text-rose-400">
              加载失败: ${e.message}
            </td>
          </tr>
        `;
  }
}

async function addIP() {
  const input = document.getElementById('ip-input');
  if (!input) return;

  const ip = input.value.trim();
  if (!ip) {
    showAlert('请输入 IP 地址', 'error');
    return;
  }

  try {
    const res = await apiFetch('/admin/whitelist/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip })
    });

    const data = await res.json();

    if (res.ok) {
      showAlert(`IP ${ip} 已添加到白名单`, 'success');
      input.value = '';
      await loadWhitelist();
      await checkSyncStatus();
    } else {
      showAlert(data.detail || '添加失败', 'error');
    }
  } catch (e) {
    showAlert(`添加失败: ${e.message}`, 'error');
  }
}

async function removeIP(ip) {
  if (!confirm(`确定要从白名单中移除 IP ${ip} 吗?`)) return;

  try {
    const res = await apiFetch('/admin/whitelist/remove', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip })
    });

    if (res.ok) {
      showAlert(`IP ${ip} 已从白名单移除`, 'success');
      await loadWhitelist();
      await checkSyncStatus();
    } else {
      const data = await res.json();
      showAlert(data.detail || '移除失败', 'error');
    }
  } catch (e) {
    showAlert(`移除失败: ${e.message}`, 'error');
  }
}

// Set all table sync status cells to spinning state
function setTableSyncingState(syncing) {
  const tbody = document.getElementById('tbody');
  if (!tbody) return;

  const statusCells = tbody.querySelectorAll('tr td:nth-child(3)');
  statusCells.forEach(cell => {
    if (syncing) {
      cell.innerHTML = '<span class="text-sky-400 flex items-center gap-1"><span class="spin">🔄</span> 同步中...</span>';
    }
  });
}

async function syncWhitelist() {
  const btn = document.getElementById('sync-btn');
  const statusEl = document.getElementById('sync-status');

  try {
    btn.disabled = true;
    btn.innerHTML = '<span class="spin">🔄</span> 同步中...';

    // Update sync status to show syncing
    statusEl.innerHTML = '<span class="spin">🔄</span> 同步中...';
    statusEl.className = 'text-xl font-semibold text-sky-400';

    // Set all table rows to syncing state
    setTableSyncingState(true);

    const res = await apiFetch('/admin/sync_whitelist', { method: 'POST' });
    const data = await res.json();

    if (data.status === 'ok') {
      showAlert(data.message || '同步完成', 'success');
    } else {
      showAlert(data.detail || '同步失败', 'error');
    }

    await checkSyncStatus();

  } catch (e) {
    showAlert(`同步请求失败: ${e.message}`, 'error');
    statusEl.textContent = '● 同步失败';
    statusEl.className = 'text-xl font-semibold text-rose-400';
  } finally {
    btn.disabled = false;
    btn.innerHTML = '🔄 同步到所有Agent';
  }
}

async function checkSyncStatus() {
  const btn = document.getElementById('check-status-btn');
  const statusEl = document.getElementById('sync-status');

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spin">📊</span> 检查中...';
  }

  // Update sync status to show checking
  statusEl.innerHTML = '<span class="spin">🔄</span> 检查中...';
  statusEl.className = 'text-xl font-semibold text-sky-400';

  // Set table rows to checking state
  setTableSyncingState(true);

  try {
    const res = await apiFetch('/admin/whitelist');
    const data = await res.json();

    if (data.whitelist) {
      document.getElementById('whitelist-total').textContent = data.whitelist.length;
      const cidrCount = data.whitelist.filter(ip => ip.includes('/')).length;
      document.getElementById('whitelist-cidr-count').textContent = cidrCount;
    }

    // Calculate sync status from nodes data
    const nodes = data.nodes || [];
    const syncedCount = nodes.filter(n => n.whitelist_sync_status === 'synced').length;
    const totalCount = nodes.length;

    // Update sync status display
    if (totalCount > 0 && syncedCount === totalCount) {
      statusEl.textContent = '● 已同步';
      statusEl.className = 'text-xl font-semibold text-emerald-400';
    } else if (syncedCount > 0) {
      statusEl.textContent = `● 部分同步 (${syncedCount}/${totalCount})`;
      statusEl.className = 'text-xl font-semibold text-yellow-400';
    } else if (totalCount > 0) {
      statusEl.textContent = '● 未同步';
      statusEl.className = 'text-xl font-semibold text-rose-400';
    } else {
      statusEl.textContent = '● 无节点';
      statusEl.className = 'text-xl font-semibold text-slate-500';
    }

    // Refresh table to show updated sync status
    await loadWhitelist();

  } catch (e) {
    statusEl.textContent = '● 检查失败';
    statusEl.className = 'text-xl font-semibold text-slate-500';
    console.error('Failed to check sync status:', e);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '📊 检查同步状态';
    }
  }
}

function init() {
  loadWhitelist();

  document.getElementById('add-ip-btn').addEventListener('click', addIP);
  document.getElementById('sync-btn').addEventListener('click', syncWhitelist);
  document.getElementById('refresh-btn').addEventListener('click', loadWhitelist);
  document.getElementById('check-status-btn').addEventListener('click', checkSyncStatus);

  // Allow Enter key to add IP
  document.getElementById('ip-input').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') addIP();
  });

  // Initial sync status check
  checkSyncStatus();
}

init();
