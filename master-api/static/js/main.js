console.log('Script loading...');
const apiFetch = (url, options = {}) => fetch(url, { credentials: 'include', ...options });

// Declare all elements in global scope (will be initialized in DOMContentLoaded)
let loginForm, loginCard, appCard, loginAlert, loginButton, passwordInput;
let loginStatus, loginStatusDot, loginStatusLabel, loginHint, authHint;
let originalLoginLabel, configAlert, importConfigsBtn, exportConfigsBtn, configFileInput;
let changePasswordAlert, currentPasswordInput, newPasswordInput, confirmPasswordInput, changePasswordBtn;

// Initialize all elements when DOM is ready

// Settings Modal Functions
function openSettingsTab(tabName) {
    toggleSettingsModal(true);
    setActiveSettingsTab(tabName);
}

function toggleSettingsModal(show) {
    const modal = document.getElementById('settings-modal');
    if (modal) {
        if (show) {
            modal.classList.remove('hidden');
            modal.classList.add('flex');
            // Default to password tab if no active tab or just opening
            if (!document.querySelector('#password-panel:not(.hidden)') &&
                !document.querySelector('#config-panel:not(.hidden)') &&
                !document.querySelector('#whitelist-panel:not(.hidden)')) {
                setActiveSettingsTab('password');
            }
        } else {
            modal.classList.add('hidden');
            modal.classList.remove('flex');
        }
    }
}

// Open Settings Modal directly to Password tab
function togglePasswordModal(show) {
    if (show) {
        toggleSettingsModal(true);
        setActiveSettingsTab('password');
    } else {
        toggleSettingsModal(false);
    }
}

// Traceroute Modal Functions
function toggleTracerouteModal(show) {
    const modal = document.getElementById('traceroute-modal');
    if (modal) {
        if (show) {
            modal.classList.remove('hidden');
            modal.classList.add('flex');
            // Populate node selector
            populateTracerouteNodes();
        } else {
            modal.classList.add('hidden');
            modal.classList.remove('flex');
        }
    }
}

function populateTracerouteNodes() {
    const select = document.getElementById('traceroute-src-node');
    if (!select || !window.nodesCache) return;

    select.innerHTML = '<option value="">请选择节点...</option>';
    window.nodesCache.forEach(node => {
        if (node.status === 'online') {
            const option = document.createElement('option');
            option.value = node.id;
            option.textContent = node.name;
            select.appendChild(option);
        }
    });
}

// Open Test Modal - scrolls to test panel section
function openTestModal() {
    const testPanel = document.querySelector('.panel-card:has(#single-test-tab)');
    if (testPanel) {
        testPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
        // Highlight the panel briefly
        testPanel.style.boxShadow = '0 0 0 2px rgba(56, 189, 248, 0.5)';
        setTimeout(() => {
            testPanel.style.boxShadow = '';
        }, 1500);
    }
}

// ============== Traceroute Functions ==============
async function executeTraceroute() {
    const nodeSelect = document.getElementById('traceroute-src-node');
    const targetInput = document.getElementById('traceroute-target');
    const startBtn = document.getElementById('traceroute-start-btn');
    const statusSpan = document.getElementById('traceroute-status');
    const resultsDiv = document.getElementById('traceroute-results');
    const hopsDiv = document.getElementById('traceroute-hops');
    const metaSpan = document.getElementById('traceroute-meta');

    const nodeId = nodeSelect?.value;
    let target = targetInput?.value?.trim();

    if (!nodeId) {
        alert('请选择源节点');
        return;
    }
    if (!target) {
        alert('请输入目标地址');
        return;
    }

    // Strip protocol and path from URLs - traceroute only needs hostname/IP
    try {
        if (target.includes('://')) {
            const url = new URL(target);
            target = url.hostname;
        } else if (target.includes('/')) {
            target = target.split('/')[0];
        }
    } catch (e) { /* Not a valid URL, use as-is */ }

    // Update UI for loading state
    startBtn.disabled = true;
    startBtn.textContent = '⏳ 追踪中...';
    statusSpan.textContent = '正在执行路由追踪，请稍候...';
    resultsDiv.classList.add('hidden');

    try {
        const response = await apiFetch(`/api/trace/run?node_id=${nodeId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target, max_hops: 30, include_geo: true })
        });

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.detail || 'Traceroute failed');
        }

        // Render results
        metaSpan.textContent = `${data.source_node_name} → ${data.target} | ${data.total_hops} 跳 | ${data.elapsed_ms}ms | ${data.tool_used}`;

        hopsDiv.innerHTML = data.hops.map(hop => {
            const geo = hop.geo;
            const geoStr = geo ? `${geo.country_code ? renderFlagHtml(geo.country_code) : ''} ${geo.city || ''} ${geo.isp || ''}`.trim() : '';
            const rttStr = hop.rtt_avg ? `${hop.rtt_avg.toFixed(1)}ms` : '-';
            const lossStr = hop.loss_pct > 0 ? `<span class="text-rose-400">${hop.loss_pct}%</span>` : '';

            return `
        <div class="flex items-center gap-4 py-2 border-b border-slate-700/50 last:border-b-0">
          <span class="w-8 text-center font-mono text-cyan-400">${hop.hop}</span>
          <span class="flex-1 font-mono text-sm ${hop.ip === '*' ? 'text-slate-500' : 'text-white'}">${hop.ip}</span>
          <span class="w-20 text-right text-sm ${hop.rtt_avg && hop.rtt_avg > 100 ? 'text-amber-400' : 'text-emerald-400'}">${rttStr}</span>
          <span class="w-12 text-right text-xs">${lossStr}</span>
          <span class="flex-1 text-xs text-slate-400 truncate">${geoStr}</span>
        </div>
      `;
        }).join('');

        resultsDiv.classList.remove('hidden');
        statusSpan.textContent = '✅ 追踪完成';

    } catch (error) {
        statusSpan.textContent = `❌ 错误: ${error.message}`;
    } finally {
        startBtn.disabled = false;
        startBtn.textContent = '🚀 开始追踪';
    }
}

function setActiveSettingsTab(tabName) {
    console.log('setActiveSettingsTab called with:', tabName);
    // Buttons
    const passwordTab = document.getElementById('password-tab');
    const configTab = document.getElementById('config-tab');
    const telegramTab = document.getElementById('telegram-tab');
    const adminTab = document.getElementById('admin-tab');

    // Panels
    const passwordPanel = document.getElementById('password-panel');
    const configPanel = document.getElementById('config-panel');
    const telegramPanel = document.getElementById('telegram-panel');
    const adminPanel = document.getElementById('admin-panel');

    console.log('Elements found:', { passwordTab, configTab, telegramTab, adminTab, passwordPanel, configPanel, telegramPanel, adminPanel });

    // Reset all buttons style
    [passwordTab, configTab, telegramTab, adminTab].forEach(btn => {
        if (btn) {
            btn.className = 'rounded-full px-4 py-2 text-sm font-semibold text-slate-300 transition hover:text-white';
        }
    });

    // Reset all panels visibility
    [passwordPanel, configPanel, telegramPanel, adminPanel].forEach(panel => {
        if (panel) panel.classList.add('hidden');
    });

    // Activate selected
    const activeBtnClass = 'rounded-full bg-gradient-to-r from-indigo-500/80 to-purple-500/80 px-4 py-2 text-sm font-semibold text-slate-50 shadow-lg shadow-indigo-500/15 ring-1 ring-indigo-400/40 transition hover:brightness-110';

    if (tabName === 'password' && passwordTab && passwordPanel) {
        passwordTab.className = activeBtnClass;
        passwordPanel.classList.remove('hidden');
    } else if (tabName === 'config' && configTab && configPanel) {
        configTab.className = activeBtnClass;
        configPanel.classList.remove('hidden');
    } else if (tabName === 'telegram' && telegramTab && telegramPanel) {
        telegramTab.className = activeBtnClass;
        telegramPanel.classList.remove('hidden');
        loadTelegramConfig();  // Load saved config when tab is opened
    } else if (tabName === 'admin' && adminTab && adminPanel) {
        adminTab.className = activeBtnClass;
        adminPanel.classList.remove('hidden');
    }
    console.log('setActiveSettingsTab completed');
}

// Telegram Functions - Using Alert API
async function loadTelegramConfig() {
    try {
        const res = await apiFetch('/api/alerts/config');
        if (res.ok) {
            const data = await res.json();
            const telegramConfig = data.configs?.telegram?.value || {};
            const thresholdsConfig = data.configs?.thresholds?.value || {};

            // Bot settings
            document.getElementById('telegram-bot-token').value = telegramConfig.bot_token || '';
            document.getElementById('telegram-chat-id').value = telegramConfig.chat_id || '';

            // Notification types
            document.getElementById('notify-route-change').checked = telegramConfig.notify_route_change ?? true;
            document.getElementById('notify-schedule-failure').checked = telegramConfig.notify_schedule_failure ?? false;
            document.getElementById('notify-node-offline').checked = telegramConfig.notify_node_offline ?? false;
            document.getElementById('notify-ping-high').checked = telegramConfig.notify_ping_high ?? false;
            document.getElementById('notify-daily-report').checked = telegramConfig.notify_daily - report ?? false;

            // Thresholds
            document.getElementById('threshold-ping-high').value = thresholdsConfig.ping_high_ms || 500;
            document.getElementById('threshold-cooldown').value = thresholdsConfig.cooldown_minutes || 5;

            // Node selection
            const nodeScope = thresholdsConfig.node_scope || 'all';
            document.getElementById('alert-all-nodes').checked = nodeScope === 'all';
            document.getElementById('alert-selected-nodes').checked = nodeScope === 'selected';
            toggleNodeSelection();

            // Load and mark selected nodes
            if (thresholdsConfig.selected_nodes && thresholdsConfig.selected_nodes.length > 0) {
                window._alertSelectedNodes = new Set(thresholdsConfig.selected_nodes);
            } else {
                window._alertSelectedNodes = new Set();
            }
            await loadAlertNodeList();
        }
    } catch (e) {
        console.log('No telegram config found or error loading:', e);
    }
}

function toggleNodeSelection() {
    const listEl = document.getElementById('node-selection-list');
    const isSelected = document.getElementById('alert-selected-nodes').checked;
    if (isSelected) {
        listEl.classList.remove('hidden');
        loadAlertNodeList();
    } else {
        listEl.classList.add('hidden');
    }
}

async function loadAlertNodeList() {
    const listEl = document.getElementById('node-selection-list');
    if (!listEl) return;

    listEl.innerHTML = '<div class="text-slate-500 text-sm">加载节点列表中...</div>';

    try {
        const res = await apiFetch('/nodes');
        console.log('[Alert] loadAlertNodeList response status:', res.status);

        if (res.ok) {
            const nodes = await res.json();
            console.log('[Alert] Loaded nodes:', nodes.length);

            if (!nodes || nodes.length === 0) {
                listEl.innerHTML = '<div class="text-slate-400 text-sm">暂无节点</div>';
                return;
            }

            const selectedNodes = window._alertSelectedNodes || new Set();

            listEl.innerHTML = nodes.map(n => `
        <label class="flex items-center gap-2 cursor-pointer p-1 rounded hover:bg-slate-800">
          <input type="checkbox" class="form-checkbox alert-node-checkbox" value="${n.id}" ${selectedNodes.has(n.id) ? 'checked' : ''} />
          <span class="text-sm text-slate-200">${n.name}</span>
          <span class="text-xs text-slate-500">${n.ip || ''}</span>
        </label>
      `).join('');
        } else {
            console.error('[Alert] Failed to load nodes:', res.status);
            listEl.innerHTML = '<div class="text-rose-400 text-sm">加载失败 (' + res.status + ')</div>';
        }
    } catch (e) {
        console.error('[Alert] Error loading nodes:', e);
        listEl.innerHTML = '<div class="text-rose-400 text-sm">加载出错</div>';
    }
}

async function saveTelegramConfig() {
    const telegramValue = {
        bot_token: document.getElementById('telegram-bot-token').value,
        chat_id: document.getElementById('telegram-chat-id').value,
        notify_route_change: document.getElementById('notify-route-change').checked,
        notify_schedule_failure: document.getElementById('notify-schedule-failure').checked,
        notify_node_offline: document.getElementById('notify-node-offline').checked,
        notify_ping_high: document.getElementById('notify-ping-high').checked,
        notify_daily_report: document.getElementById('notify-daily-report').checked,
    };

    // Gather selected nodes
    const selectedNodes = [];
    document.querySelectorAll('.alert-node-checkbox:checked').forEach(cb => {
        selectedNodes.push(parseInt(cb.value));
    });

    const thresholdsValue = {
        ping_high_ms: parseInt(document.getElementById('threshold-ping-high').value) || 500,
        cooldown_minutes: parseInt(document.getElementById('threshold-cooldown').value) || 5,
        node_scope: document.getElementById('alert-selected-nodes').checked ? 'selected' : 'all',
        selected_nodes: selectedNodes,
    };

    try {
        // Save telegram config
        const res1 = await apiFetch('/api/alerts/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: 'telegram', value: telegramValue, enabled: true })
        });

        // Save thresholds config
        const res2 = await apiFetch('/api/alerts/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: 'thresholds', value: thresholdsValue, enabled: true })
        });

        const alertEl = document.getElementById('telegram-alert');
        if (res1.ok && res2.ok) {
            alertEl.className = 'alert mb-4 rounded-xl px-4 py-3 text-sm font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/40';
            alertEl.textContent = '✅ 设置已保存（含阈值和节点选择）';
        } else {
            alertEl.className = 'alert mb-4 rounded-xl px-4 py-3 text-sm font-semibold bg-rose-500/20 text-rose-400 border border-rose-500/40';
            alertEl.textContent = '❌ 保存失败';
        }
        alertEl.classList.remove('hidden');
        setTimeout(() => alertEl.classList.add('hidden'), 3000);
    } catch (e) {
        console.error('Save telegram config error:', e);
    }
}

async function testTelegramConfig() {
    try {
        const res = await apiFetch('/api/alerts/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel: 'telegram' })
        });
        const alertEl = document.getElementById('telegram-alert');
        const data = await res.json();

        if (data.status === 'ok') {
            alertEl.className = 'alert mb-4 rounded-xl px-4 py-3 text-sm font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/40';
            alertEl.textContent = '✅ 测试消息已发送，请检查Telegram';
        } else {
            alertEl.className = 'alert mb-4 rounded-xl px-4 py-3 text-sm font-semibold bg-rose-500/20 text-rose-400 border border-rose-500/40';
            alertEl.textContent = `❌ 发送失败: ${data.message || '请检查配置'}`;
        }
        alertEl.classList.remove('hidden');
        setTimeout(() => alertEl.classList.add('hidden'), 5000);
    } catch (e) {
        console.error('Test telegram error:', e);
    }
}

// Admin Functions
function showAdminAlert(message, isError = false) {
    const el = document.getElementById('admin-alert');
    if (!el) return;
    el.className = `mb-4 rounded-xl px-4 py-3 text-sm font-semibold ${isError ? 'bg-rose-500/20 text-rose-400 border border-rose-500/40' : 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/40'}`;
    el.textContent = message;
    el.classList.remove('hidden');
}

async function clearAllTestData() {
    const confirmed = await showConfirm(
        '清空所有测试数据',
        `<p>确定要清空所有测试数据吗？</p>
     <p>这将删除：</p>
     <ul>
       <li>所有单次测试记录</li>
       <li>所有定时任务执行历史</li>
     </ul>
     <p class="danger-text">⚠️ 此操作不可撤销！</p>`,
        { type: 'danger', confirmText: '确认清空', cancelText: '取消' }
    );
    if (!confirmed) return;

    try {
        const res = await apiFetch('/admin/clear_all_test_data', { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
            showAdminAlert(`✓ 成功清空数据：删除了 ${data.test_results_deleted || 0} 条测试记录，${data.schedule_results_deleted || 0} 条定时任务历史`);
        } else {
            showAdminAlert(`✗ 失败: ${data.detail || '未知错误'}`, true);
        }
    } catch (e) {
        showAdminAlert(`✗ 请求失败: ${e.message}`, true);
    }
}

async function clearScheduleResults() {
    const confirmed = await showConfirm(
        '清空定时任务历史',
        `<p>确定要清空定时任务历史吗？</p>
     <p>这将删除所有定时任务的执行记录。</p>
     <p class="danger-text">⚠️ 此操作不可撤销！</p>`,
        { type: 'danger', confirmText: '确认清空', cancelText: '取消' }
    );
    if (!confirmed) return;

    try {
        const res = await apiFetch('/admin/clear_schedule_results', { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
            showAdminAlert(`✓ 成功清空定时任务历史：删除了 ${data.count || 0} 条记录`);
        } else {
            showAdminAlert(`✗ 失败: ${data.detail || '未知错误'}`, true);
        }
    } catch (e) {
        showAdminAlert(`✗ 请求失败: ${e.message}`, true);
    }
}

// IP Whitelist Functions

// Show alert message
function showWhitelistAlert(message, type = 'info') {
    const alert = document.getElementById('whitelist-alert');
    if (!alert) return;

    alert.classList.remove('hidden', 'border-emerald-500', 'bg-emerald-500/10', 'text-emerald-100',
        'border-rose-500', 'bg-rose-500/10', 'text-rose-100',
        'border-blue-500', 'bg-blue-500/10', 'text-blue-100');

    if (type === 'success') {
        alert.classList.add('border-emerald-500', 'bg-emerald-500/10', 'text-emerald-100');
    } else if (type === 'error') {
        alert.classList.add('border-rose-500', 'bg-rose-500/10', 'text-rose-100');
    } else {
        alert.classList.add('border-blue-500', 'bg-blue-500/10', 'text-blue-100');
    }

    alert.textContent = message;
    alert.classList.remove('hidden');

    // Auto-hide after 5 seconds
    setTimeout(() => alert.classList.add('hidden'), 5000);
}

// Refresh whitelist table
async function refreshWhitelist() {
    const tbody = document.getElementById('whitelist-table-body');
    if (!tbody) return;

    try {
        const res = await apiFetch('/admin/whitelist');
        const data = await res.json();

        if (!data.whitelist || data.whitelist.length === 0) {
            tbody.innerHTML = `
        <tr>
          <td colspan="4" class="px-4 py-8 text-center text-slate-500">
            暂无白名单 IP，点击上方"添加"按钮开始
          </td>
        </tr>
      `;
            return;
        }

        // Update stats
        document.getElementById('whitelist-total').textContent = data.count || data.whitelist.length;
        const cidrCount = data.whitelist.filter(ip => ip.includes('/')).length;
        document.getElementById('whitelist-cidr-count').textContent = cidrCount;

        // Render table rows
        tbody.innerHTML = data.whitelist.map(ip => {
            // Find corresponding node info if available
            const nodeInfo = data.nodes?.find(n => n.ip === ip);
            const isCIDR = ip.includes('/');
            const isIPv6 = ip.includes(':');

            let ipType = 'IPv4';
            if (isCIDR) ipType = 'CIDR';
            else if (isIPv6) ipType = 'IPv6';

            let source = nodeInfo ? `节点: ${nodeInfo.name}` : '手动添加';

            return `
        <tr class="hover:bg-slate-800/40 transition">
          <td class="px-4 py-3">
            <code class="text-sm font-mono text-sky-300">${ip}</code>
          </td>
          <td class="px-4 py-3 text-slate-400 text-xs">
            ${source}
          </td>
          <td class="px-4 py-3 text-xs">
            ${(() => {
                    if (!nodeInfo) return '<span class="text-slate-500">-</span>';

                    // Initial render - default to unknown/unchecked unless we have data
                    if (!nodeInfo.whitelist_sync_status || nodeInfo.whitelist_sync_status === 'unknown') {
                        return '<span class="text-slate-500 flex items-center gap-1">❓ 未检查</span>';
                    }

                    if (nodeInfo.whitelist_sync_status === 'synced') {
                        const timeStr = nodeInfo.whitelist_sync_at ? new Date(nodeInfo.whitelist_sync_at).toLocaleTimeString() : '';
                        return `<span class="text-emerald-400 flex items-center gap-1" title="已同步 ${timeStr}">✅ 已同步</span>`;
                    }

                    if (nodeInfo.whitelist_sync_status === 'not_synced') {
                        return '<span class="text-yellow-400 flex items-center gap-1" title="内容不一致">⚠️ 未同步</span>';
                    }

                    // Display specific error if available
                    const errorMsg = nodeInfo.whitelist_sync_message || '未知错误';
                    return `<span class="text-rose-400 flex items-center gap-1" title="${errorMsg}">❌ ${errorMsg}</span>`;
                })()}
          </td>
          <td class="px-4 py-3">
            <span class="inline-flex items-center px-2 py-1 rounded-md text-xs font-semibold ${isCIDR ? 'bg-purple-500/20 text-purple-300' :
                    isIPv6 ? 'bg-blue-500/20 text-blue-300' :
                        'bg-emerald-500/20 text-emerald-300'
                }">
              ${ipType}
            </span>
          </td>
          <td class="px-4 py-3 text-right">
            <button 
              onclick="removeWhitelistIp('${ip}')" 
              class="px-3 py-1 rounded-lg border border-rose-700 bg-rose-900/20 text-xs font-semibold text-rose-300 hover:bg-rose-900/40 transition"
            >
              删除
            </button>
          </td>
        </tr>
      `;
        }).join('');

    } catch (e) {
        showWhitelistAlert(`获取白名单失败: ${e.message}`, 'error');
        tbody.innerHTML = `
      <tr>
        <td colspan="4" class="px-4 py-8 text-center text-rose-400">
          加载失败: ${e.message}
        </td>
      </tr>
    `;
    }
}

// Add IP to whitelist
async function addWhitelistIp() {
    const input = document.getElementById('whitelist-ip-input');
    if (!input) return;

    const ip = input.value.trim();
    if (!ip) {
        showWhitelistAlert('请输入 IP 地址', 'error');
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
            showWhitelistAlert(`IP ${ip} 已添加到白名单`, 'success');
            input.value = ''; // Clear input
            await refreshWhitelist(); // Refresh list
            await checkWhitelistStatus(); // Update stats
        } else {
            showWhitelistAlert(data.detail || '添加失败', 'error');
        }
    } catch (e) {
        showWhitelistAlert(`添加失败: ${e.message}`, 'error');
    }
}

// Remove IP from whitelist
async function removeWhitelistIp(ip) {
    if (!confirm(`确定要从白名单中移除 IP ${ip} 吗?`)) return;

    try {
        const res = await apiFetch('/admin/whitelist/remove', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ip })
        });

        if (res.ok) {
            showWhitelistAlert(`IP ${ip} 已从白名单移除`, 'success');
            await refreshWhitelist(); // Refresh list
            await checkWhitelistStatus(); // Update stats
        } else {
            const data = await res.json();
            showWhitelistAlert(data.detail || '移除失败', 'error');
        }
    } catch (e) {
        showWhitelistAlert(`移除失败: ${e.message}`, 'error');
    }
}

async function syncWhitelist() {
    const btn = document.getElementById('sync-whitelist-btn');
    // Legacy result display removed as per request

    try {
        btn.disabled = true;
        btn.innerHTML = '<span>🔄</span><span>同步中...</span>';

        const res = await apiFetch('/admin/sync_whitelist', { method: 'POST' });

        // Check status immediately after sync
        await checkWhitelistStatus();

    } catch (e) {
        showWhitelistAlert(`同步请求失败: ${e.message}`, 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<span>🔄</span><span>同步到所有 Agent</span>';
    }
}

async function checkWhitelistStatus() {
    const btn = document.getElementById('check-whitelist-status-btn');
    const contentDiv = document.getElementById('sync-result-content');

    // Hide previous result box if exists
    const resultDiv = document.getElementById('whitelist-sync-result');
    if (resultDiv) resultDiv.classList.add('hidden');

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span>📊</span><span>检查中...</span>';
    }

    try {
        // Fetch stats from Master's whitelist list
        const resStats = await apiFetch('/admin/whitelist');
        const data = await resStats.json();

        if (data.whitelist) {
            const totalEl = document.getElementById('whitelist-total');
            if (totalEl) totalEl.textContent = data.whitelist.length;
            const cidrEl = document.getElementById('whitelist-cidr-count');
            const cidrCount = data.whitelist.filter(ip => ip.includes('/')).length;
            if (cidrEl) cidrEl.textContent = cidrCount;
        }

        // Trigger check on backend (updates DB)
        await apiFetch('/admin/whitelist/status');

        // Refresh main table to show updated status from DB
        await refreshWhitelist();

    } catch (e) {
        console.error('Failed to update whitelist stats', e);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<span>📊</span><span>检查同步状态</span>';
        }
    }
}



// ============== Auth Functions ==============
async function checkAuth(showFeedback = false) {
  try {
    const res = await apiFetch('/auth/status');
    if (!res.ok) {
      let message = '�޷���֤��¼״̬��';
      try {
        const data = await res.json();
        if (data?.detail) message = \��֤ʧ�ܣ�\\;
      } catch (_) {
        try {
          const rawText = await res.text();
          if (rawText) message = \��֤ʧ�ܣ�\\;
        } catch (_) {}
      }

      setLoginState('error', message);
      if (showFeedback && loginAlert) setAlert(loginAlert, message);
      return false;
    }

    const data = await res.json();
    const isGuest = data.isGuest === true;
    window.isGuest = isGuest;
    
    if (data.authenticated || isGuest) {
      if (loginCard) loginCard.classList.add('hidden');
      if (appCard) appCard.classList.remove('hidden');
      showSidebarNavigation();  // Show sidebar after login
      setLoginState('unlocked');
      
      if (authHint) {
        if (isGuest) {
          authHint.textContent = '??? �ÿ�ģʽ - ���ɲ鿴���޷�����';
          authHint.className = 'text-sm text-amber-400';
        } else {
          authHint.textContent = '��ͨ����֤���ɹ����ڵ����������';
          authHint.className = 'text-sm text-slate-400';
        }
      }
      
      if (isGuest) {
        // Hide action buttons for guests
        document.querySelectorAll('.guest-hide').forEach(el => el.classList.add('hidden'));
        document.getElementById('logout-btn')?.classList.remove('hidden');
      } else {
        document.querySelectorAll('.guest-hide').forEach(el => el.classList.remove('hidden'));
      }
      
      await refreshNodes();
      await refreshTests();
      return true;
    } else {
      if (appCard) appCard.classList.add('hidden');
      if (loginCard) loginCard.classList.remove('hidden');
      hideSidebarNavigation();  // Hide sidebar when not authenticated
      setLoginState('idle');
      if (showFeedback && loginAlert) setAlert(loginAlert, '��¼״̬δ�����������µ�¼��');
      return false;
    }
  } catch (err) {
    console.error('Auth check failed:', err);
    if (appCard) appCard.classList.add('hidden');
    if (loginCard) loginCard.classList.remove('hidden');
    hideSidebarNavigation();  // Hide sidebar on error
    const errorMessage = '�޷�������֤�������Ժ����ԡ�';
    setLoginState('error', errorMessage);
    if (showFeedback && loginAlert) setAlert(loginAlert, errorMessage);
    return false;
  }
}

async function login() {
  console.log('Starting login process...');
  if (loginAlert) clearAlert(loginAlert);
  
  // Reset animations
  const card = document.querySelector('.login-card');
  if (card) card.classList.remove('animate-shake', 'animate-success');
  
  const password = (passwordInput?.value || '').trim();
  if (!password) {
    console.warn('Login aborted: empty password');
    if (loginAlert) setAlert(loginAlert, '���������� (Password Required)');
    passwordInput?.focus();
    if (card) {
        card.classList.add('animate-shake');
        setTimeout(() => card.classList.remove('animate-shake'), 400);
    }
    return;
  }

  setLoginButtonLoading(true);
  
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 8000); // 8s timeout

  try {
    console.log('Sending login request to /auth/login...');
    const res = await apiFetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
      signal: controller.signal
    });
    clearTimeout(timeoutId);

    console.log(\Login response status: \\);

    if (res.ok) {
       console.log('Login success. Verifying session...');
       if (loginAlert) {
           loginAlert.className = 'alert alert-success';
           setAlert(loginAlert, '��¼�ɹ� (Success)');
       }
       if (card) card.classList.add('animate-success');
       
       // Hide login card immediately to prevent flash
       if (loginCard) {
           loginCard.style.opacity = '0.5';
           loginCard.style.pointerEvents = 'none';
       }
       
       // Allow more time for the cookie to be processed/saved by the browser
       setTimeout(async () => {
         const authed = await checkAuth(true);
         console.log(\Session check result: \\);
         if (!authed) {
            console.error('Login successful but session check failed.');
            if (loginAlert) {
                loginAlert.className = 'alert alert-error';
                setAlert(loginAlert, '�Ự����ʧ�� (Session Failed) - Cookie Blocked?');
            }
            if (card) {
                card.classList.remove('animate-success');
                card.classList.add('animate-shake');
            }
            if (loginCard) {
                loginCard.style.opacity = '1';
                loginCard.style.pointerEvents = 'auto';
            }
         }
       }, 1200);
       return;
    }

    // Handle HTTP errors
    if (card) {
        card.classList.add('animate-shake');
        setTimeout(() => card.classList.remove('animate-shake'), 400);
    }
    
    if (loginAlert) loginAlert.className = 'alert alert-error';
    let message = '��¼ʧ�� (Login Failed)';
    
    if (res.status === 401) {
        console.warn('Login failed: 401 Unauthorized');
        message = '��¼ʧ�ܣ�������� (Invalid Password)';
    } else if (res.status === 408 || res.status === 504) {
         console.error('Login failed: Timeout');
         message = '��¼��ʱ (Request Timeout)';
    } else {
        try {
            const data = await res.json();
            console.warn('Login failed with details:', data);
            if (data?.detail === 'empty_password') message = '���벻��Ϊ��';
            else if (data?.detail === 'invalid_password') message = '��¼ʧ�ܣ�������� (Invalid Password)';
            else if (data?.detail) message = \��¼ʧ�ܣ�\\;
        } catch (e) {
            console.error('Failed to parse error response:', e);
            message = \��¼ʧ�� (HTTP \)\;
        }
    }
    if (loginAlert) setAlert(loginAlert, message);

  } catch (err) {
    clearTimeout(timeoutId);
    console.error('Login network exception:', err);
    
    if (card) {
        card.classList.add('animate-shake');
        setTimeout(() => card.classList.remove('animate-shake'), 400);
    }
    
    if (loginAlert) {
        loginAlert.className = 'alert alert-error';
        const errorMsg = err.name === 'AbortError' ? '����ʱ (Timeout)' : '�޷����ӷ����� (Network Error)';
        setAlert(loginAlert, errorMsg);
    }
  } finally {
    setLoginButtonLoading(false);
  }
}

async function logout() {
  await apiFetch('/auth/logout', { method: 'POST' });
  // Also clear guest session
  document.cookie = 'guest_session=; Max-Age=0; path=/';
  window.isGuest = false;
  await checkAuth();
}

async function guestLogin() {
  console.log('Starting guest login...');
  try {
    const res = await apiFetch('/auth/guest', { method: 'POST' });
    if (res.ok) {
      console.log('Guest login successful');
      window.isGuest = true;
      await checkAuth();
    } else {
      console.error('Guest login failed');
    }
  } catch (err) {
    console.error('Guest login error:', err);
  }
}

async function changePassword() {
  clearAlert(changePasswordAlert);

  const payload = {
    current_password: currentPasswordInput.value,
    new_password: newPasswordInput.value,
    confirm_password: confirmPasswordInput.value,
  };

  if (!payload.new_password) {
    setAlert(changePasswordAlert, '�����������롣');
    return;
  }

  if (payload.new_password.length < 6) {
    setAlert(changePasswordAlert, '�����볤���費���� 6 λ��');
    return;
  }

  if (payload.new_password !== payload.confirm_password) {
    setAlert(changePasswordAlert, '��������������벻һ�¡�');
    return;
  }

  const res = await apiFetch('/auth/change', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    let feedback = '��������ʧ�ܡ�';
    try {
      const data = await res.json();
      if (data?.detail === 'invalid_password') feedback = '��ǰ���벻��ȷ��Ự�ѹ��ڡ�';
      if (data?.detail === 'password_too_short') feedback = '�����볤�Ȳ��� 6 λ��';
      if (data?.detail === 'password_mismatch') feedback = '��������������벻һ�¡�';
      if (data?.detail === 'empty_password') feedback = '�����������롣';
    } catch (err) {
      feedback = feedback + ' ' + (err?.message || '');
    }
    setAlert(changePasswordAlert, feedback.trim());
    return;
  }

  changePasswordAlert.className = 'alert alert-success';
  setAlert(changePasswordAlert, '? �����ѳɹ�����!��ǰ�Ự��ʹ�������롣');
  currentPasswordInput.value = '';
  newPasswordInput.value = '';
  confirmPasswordInput.value = '';
}

// ============== Helper Functions ==============
function maskIp(ip, hidden) {
  if (!hidden || !ip) return ip;
  
  // Check if it's a domain name (contains non-numeric parts)
  const isIp = /^[\d.:]+$/.test(ip);
  
  if (isIp) {
    // Mask last two segments of IPv4: 1.2.3.4 -> 1.2.*.*
    const parts = ip.split('.');
    if (parts.length === 4) {
        return \\.\.*.*\;
    }
    return ip.replace(/[\d]+$/, '*'); // Fallback for IPv6 or other
  } else {
    // Domain name: keep first subdomain, mask the rest
    // hkt-ty-line-1.sudatech.store -> hkt-ty-line-1.**.**
    const parts = ip.split('.');
    if (parts.length >= 2) {
      const maskedParts = parts.map((part, idx) => idx === 0 ? part : '**');
      return maskedParts.join('.');
    }
    return ip;
  }
}

function maskPort(port, shouldMask) {
  if (!port) return port;
  return shouldMask ? '****' : \\\;
}

async function exportAgentConfigs() {
  clearAlert(configAlert);
  const res = await apiFetch('/agent-configs/export');
  if (!res.ok) {
    setAlert(configAlert, '��������ʧ�ܡ�');
    return;
  }

  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'agent_configs.json';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

async function importAgentConfigs(file) {
  clearAlert(configAlert);
  if (!file) return;

  let payload;
  try {
    payload = JSON.parse(await file.text());
  } catch (err) {
    setAlert(configAlert, 'JSON �ļ���Ч��');
    return;
  }

  const res = await apiFetch('/agent-configs/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });

  if (!res.ok) {
    const msg = await res.text();
    setAlert(configAlert, msg || '��������ʧ�ܡ�');
    return;
  }

  const imported = await res.json();
  setAlert(configAlert, \�ѵ��� \ ���������á�\);
}


// ============== Node Functions ==============
function resetNodeForm() {
    if (typeof nodeName !== 'undefined' && nodeName) nodeName.value = '';
    if (typeof nodeIp !== 'undefined' && nodeIp) nodeIp.value = '';
    if (typeof nodePort !== 'undefined' && nodePort) nodePort.value = 8000;
    if (typeof nodeIperf !== 'undefined' && nodeIperf) nodeIperf.value = DEFAULT_IPERF_PORT;
    if (typeof nodeDesc !== 'undefined' && nodeDesc) nodeDesc.value = '';
    editingNodeId = null;
    if (typeof saveNodeBtn !== 'undefined' && saveNodeBtn) saveNodeBtn.textContent = '保存节点';
    if (typeof addNodeTitle !== 'undefined' && addNodeTitle) addNodeTitle.textContent = '添加节点';
    if (typeof addNodeAlert !== 'undefined' && addNodeAlert) hide(addNodeAlert);
}

async function removeNode(nodeId) {
    if (typeof addNodeAlert !== 'undefined' && addNodeAlert) clearAlert(addNodeAlert);
    const confirmDelete = confirm('确定删除该节点并清理相关测试记录吗？');
    if (!confirmDelete) return;

    const res = await apiFetch(`/nodes/${nodeId}`, { method: 'DELETE' });
    if (!res.ok) {
        if (typeof addNodeAlert !== 'undefined' && addNodeAlert) setAlert(addNodeAlert, '删除节点失败�?);
        return;
    }

    if (editingNodeId === nodeId) {
        resetNodeForm();
        closeAddNodeModal();
    }

    await refreshNodes();
    await refreshTests();
}

async function refreshNodes() {
    if (isRefreshingNodes) return;
    isRefreshingNodes = true;
    try {
        const previousSrc = typeof srcSelect !== 'undefined' && srcSelect ? (Number(srcSelect.value) || null) : null;
        const previousDst = typeof dstSelect !== 'undefined' && dstSelect ? (Number(dstSelect.value) || null) : null;
        const previousSuiteSrc = typeof suiteSrcSelect !== 'undefined' && suiteSrcSelect ? (Number(suiteSrcSelect.value) || null) : null;
        const previousSuiteDst = typeof suiteDstSelect !== 'undefined' && suiteDstSelect ? (Number(suiteDstSelect.value) || null) : null;

        const res = await apiFetch('/nodes/status');
        const nodes = await res.json();
        nodeCache = nodes;
        window.nodesCache = nodes; // Expose for traceroute

        if (typeof nodesList !== 'undefined' && nodesList) nodesList.innerHTML = '';
        if (typeof srcSelect !== 'undefined' && srcSelect) srcSelect.innerHTML = '';
        if (typeof dstSelect !== 'undefined' && dstSelect) dstSelect.innerHTML = '';
        if (typeof suiteSrcSelect !== 'undefined' && suiteSrcSelect) suiteSrcSelect.innerHTML = '';
        if (typeof suiteDstSelect !== 'undefined' && suiteDstSelect) suiteDstSelect.innerHTML = '';

        if (!nodes.length) {
            if (typeof nodesList !== 'undefined' && nodesList) nodesList.textContent = '暂无节点�?;
            return;
        }

        nodes.forEach((node) => {
            cacheStreamingFromNode(node);

            const privacyEnabled = !!ipPrivacyState[node.id];
            const flagInfo = resolveLocalFlag(node);
            const locationBadge = renderFlagSlot(node.id, flagInfo, 'text-base drop-shadow-sm', '服务器所在地�?);

            const statusBadge = node.status === 'online'
                ? `<span class="${styles.badgeOnline}"><span class="h-2 w-2 rounded-full bg-emerald-400"></span><span>在线</span></span>`
                : `<span class="${styles.badgeOffline}"><span class="h-2 w-2 rounded-full bg-rose-400"></span><span>离线</span></span>`;

            // Whitelist Sync Badge
            let syncBadge = '';
            const syncTime = node.whitelist_sync_at ? new Date(node.whitelist_sync_at).toLocaleString() : '未知';
            const errorMsg = node.whitelist_sync_message || '未知错误';

            if (node.whitelist_sync_status === 'synced') {
                syncBadge = `<span class="inline-flex items-center rounded-md bg-emerald-500/10 px-2 py-0.5 text-xs font-medium text-emerald-400 ring-1 ring-inset ring-emerald-500/20 cursor-help" title="白名单已同步 (${syncTime})">🔄 白名�?/span>`;
            } else if (node.whitelist_sync_status === 'not_synced') {
                syncBadge = `<span class="inline-flex items-center rounded-md bg-yellow-500/10 px-2 py-0.5 text-xs font-medium text-yellow-400 ring-1 ring-inset ring-yellow-500/20 cursor-help" title="白名单内容不一�?(${syncTime})">⚠️ 白名�?/span>`;
            } else if (node.whitelist_sync_status === 'failed') {
                syncBadge = `<span class="inline-flex items-center rounded-md bg-rose-500/10 px-2 py-0.5 text-xs font-medium text-rose-400 ring-1 ring-inset ring-rose-500/20 cursor-help" title="同步失败: ${errorMsg} (${syncTime})">�?白名�?/span>`;
            } else {
                syncBadge = `<span class="inline-flex items-center rounded-md bg-slate-500/10 px-2 py-0.5 text-xs font-medium text-slate-400 ring-1 ring-inset ring-slate-500/20" title="白名单同步状态未�?>�?白名�?/span>`;
            }

            // Version Mismatch Badge
            const expectedVersion = '1.4.0';
            let versionBadge = '';
            if (node.agent_version && node.agent_version !== expectedVersion) {
                versionBadge = `<span class="inline-flex items-center rounded-md bg-amber-500/10 px-2 py-0.5 text-xs font-medium text-amber-400 ring-1 ring-inset ring-amber-500/20 cursor-help" title="Agent版本 ${node.agent_version} 与预期版�?${expectedVersion} 不一致，请更�?>⬆️ 需更新</span>`;
            }

            // Internal Agent Badge
            let internalBadge = '';
            if (node.agent_mode === 'reverse') {
                internalBadge = `<span class="inline-flex items-center rounded-md bg-violet-500/10 px-2 py-0.5 text-xs font-medium text-violet-400 ring-1 ring-inset ring-violet-500/20 cursor-help" title="内网穿透模�?(反向注册模式)">🔗 内网穿�?/span>`;
            }

            // Auto-Update Status Badge
            let updateBadge = '';
            if (node.update_status === 'updated') {
                const updateTime = node.update_at ? new Date(node.update_at).toLocaleString('zh-CN') : '';
                updateBadge = `<span class="inline-flex items-center rounded-md bg-emerald-500/10 px-2 py-0.5 text-xs font-medium text-emerald-400 ring-1 ring-inset ring-emerald-500/20 cursor-help" title="${node.update_message || '自动更新成功'} (${updateTime})">�?自动更新</span>`;
            } else if (node.update_status === 'pending') {
                updateBadge = `<span class="inline-flex items-center rounded-md bg-yellow-500/10 px-2 py-0.5 text-xs font-medium text-yellow-400 ring-1 ring-inset ring-yellow-500/20 cursor-help" title="${node.update_message || '更新�?..'}">�?更新�?/span>`;
            } else if (node.update_status === 'failed') {
                updateBadge = `<span class="inline-flex items-center rounded-md bg-rose-500/10 px-2 py-0.5 text-xs font-medium text-rose-400 ring-1 ring-inset ring-rose-500/20 cursor-help" title="${node.update_message || '自动更新失败'}">�?更新失败</span>`;
            }

            const ports = node.detected_iperf_port ? `${node.detected_iperf_port}` : `${node.iperf_port}`;
            const agentPort = node.detected_agent_port || node.agent_port;
            const iperfPortDisplay = maskPort(ports, privacyEnabled || window.isGuest);
            const streamingBadges = renderStreamingBadges(node.id);
            const backboneBadges = renderBackboneBadges(node.backbone_latency, node.id);
            const ipMasked = maskIp(node.ip, privacyEnabled || window.isGuest);

            if (typeof nodesList !== 'undefined' && nodesList) {
                const item = document.createElement('div');
                item.className = styles.rowCard;
                item.innerHTML = `
        <div class="pointer-events-none absolute inset-0 opacity-80">
            <div class="absolute inset-0 bg-gradient-to-br from-emerald-500/8 via-transparent to-sky-500/10"></div>
            <div class="absolute -left-10 top-0 h-32 w-32 rounded-full bg-sky-500/10 blur-3xl"></div>
        </div>
        <div class="relative flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div class="flex-1 space-y-2">
            <div class="flex flex-wrap items-center gap-2">
              ${statusBadge}
              ${locationBadge}
              ${syncBadge}
              ${versionBadge}
              ${internalBadge}
              ${updateBadge}
              <span class="text-base font-semibold text-white drop-shadow">${node.name}</span>
              ${!window.isGuest ? `<button type="button" class="${styles.iconButton}" data-privacy-toggle="${node.id}" aria-label="切换 IP 隐藏">
                <span class="text-base">${ipPrivacyState[node.id] ? '🙈' : '👁�?}</span>
              </button>` : ''}
            </div>
            ${backboneBadges ? `<div class="flex flex-wrap items-center gap-2">${backboneBadges}</div>` : ''}
            <div class="flex flex-wrap items-center gap-2" data-streaming-badges="${node.id}">${streamingBadges || ''}</div>
            <p class="${styles.textMuted} flex items-center gap-2 text-xs">
              <span class="font-mono text-slate-400" data-node-ip-display="${node.id}">${ipMasked}</span>
              <!-- ISP Display -->
              <span class="text-slate-500 border-l border-slate-700 pl-2" id="isp-${node.id}"></span>
            </p>
          </div>
          ${!window.isGuest ? `<div class="flex flex-wrap items-center justify-start gap-2 lg:flex-col lg:items-end lg:justify-center lg:min-w-[170px] opacity-100 md:opacity-0 md:pointer-events-none md:transition md:duration-200 md:group-hover:opacity-100 md:group-hover:pointer-events-auto md:focus-within:opacity-100 md:focus-within:pointer-events-auto">
            <button class="${styles.pillInfo}" onclick="runStreamingCheck(${node.id})">流媒体解锁测�?/button>
            <button class="${styles.pillInfo}" onclick="editNode(${node.id})">编辑</button>
            <button class="${styles.pillDanger}" onclick="removeNode(${node.id})">删除</button>
          </div>` : ''}
        </div>
      `;
                nodesList.appendChild(item);

                const toggleBtn = item.querySelector('[data-privacy-toggle]');
                const ipDisplay = item.querySelector(`[data-node-ip-display="${node.id}"]`);
                const flagDisplay = item.querySelectorAll(`[data-node-flag="${node.id}"]`);
                const iperfPortSpan = item.querySelector(`[data-node-iperf-display="${node.id}"]`);
                toggleBtn?.addEventListener('click', () => {
                    const nextState = !ipPrivacyState[node.id];
                    ipPrivacyState[node.id] = nextState;
                    if (ipDisplay) {
                        ipDisplay.textContent = maskIp(node.ip, nextState);
                    }
                    if (iperfPortSpan) {
                        iperfPortSpan.textContent = `· iperf ${maskPort(ports, nextState)}${node.description ? ' · ' + node.description : ''}`;
                    }
                    toggleBtn.innerHTML = `<span class="text-base">${nextState ? '🙈' : '👁�?}</span>`;
                    toggleBtn.setAttribute('aria-pressed', String(nextState));
                });

                attachFlagUpdater(node, flagDisplay);
            } // end if nodesList

            // Build options for select inputs
            const optionA = document.createElement('option');
            optionA.value = node.id;
            optionA.textContent = `${node.name} (${maskIp(node.ip, privacyEnabled)} | iperf ${maskPort(ports, privacyEnabled)})`;

            if (typeof srcSelect !== 'undefined' && srcSelect) srcSelect.appendChild(optionA);
            if (typeof dstSelect !== 'undefined' && dstSelect) {
                const optionB = optionA.cloneNode(true);
                dstSelect.appendChild(optionB);
            }

            if (typeof suiteSrcSelect !== 'undefined' && suiteSrcSelect && typeof suiteDstSelect !== 'undefined' && suiteDstSelect) {
                const suiteOptionA = optionA.cloneNode(true);
                const suiteOptionB = optionA.cloneNode(true);
                suiteSrcSelect.appendChild(suiteOptionA);
                suiteDstSelect.appendChild(suiteOptionB);
            }
        });

        // Restore previous selection or default to first
        const firstNodeId = nodes[0]?.id;
        if (typeof srcSelect !== 'undefined' && srcSelect) {
            if (previousSrc && nodes.some((n) => n.id === previousSrc)) srcSelect.value = String(previousSrc);
            else if (firstNodeId) srcSelect.value = String(firstNodeId);
        }
        if (typeof dstSelect !== 'undefined' && dstSelect) {
            if (previousDst && nodes.some((n) => n.id === previousDst)) dstSelect.value = String(previousDst);
            else if (firstNodeId) dstSelect.value = String(firstNodeId);
        }

        if (typeof suiteSrcSelect !== 'undefined' && suiteSrcSelect) {
            if (previousSuiteSrc && nodes.some((n) => n.id === previousSuiteSrc)) suiteSrcSelect.value = String(previousSuiteSrc);
            else if (firstNodeId) suiteSrcSelect.value = String(firstNodeId);
        }

        if (typeof suiteDstSelect !== 'undefined' && suiteDstSelect) {
            if (previousSuiteDst && nodes.some((n) => n.id === previousSuiteDst)) suiteDstSelect.value = String(previousSuiteDst);
            else if (firstNodeId) suiteDstSelect.value = String(firstNodeId);
        }

        // Fetch ISP info logic ...
        const ISP_CACHE_KEY = 'isp_cache';
        const ISP_CACHE_TTL = 24 * 60 * 60 * 1000; // 24 hours in ms

        function getIspCache() {
            try {
                const cached = localStorage.getItem(ISP_CACHE_KEY);
                if (cached) {
                    const data = JSON.parse(cached);
                    if (data.expires > Date.now()) return data.ips;
                }
            } catch (e) { }
            return {};
        }

        function saveIspCache(ips) {
            try {
                localStorage.setItem(ISP_CACHE_KEY, JSON.stringify({
                    ips: ips,
                    expires: Date.now() + ISP_CACHE_TTL
                }));
            } catch (e) { }
        }

        const ispCache = getIspCache();

        nodes.forEach(node => {
            if (!ipPrivacyState[node.id]) {
                if (ispCache[node.ip]) {
                    const el = document.getElementById(`isp-${node.id}`);
                    if (el) {
                        el.textContent = ispCache[node.ip].isp;
                        el.title = ispCache[node.ip].country_code || '';
                    }
                } else {
                    // Fetch from API and cache
                    fetch(`/geo?ip=${node.ip}`)
                        .then(r => r.json())
                        .then(d => {
                            const el = document.getElementById(`isp-${node.id}`);
                            if (el && d.isp) {
                                el.textContent = d.isp;
                                el.title = d.country_code || '';
                                ispCache[node.ip] = { isp: d.isp, country_code: d.country_code };
                                saveIspCache(ispCache);
                            }
                        })
                        .catch(() => { });
                }
            }
        });

        // Async fetch ping trends
        nodes.forEach(node => updateNodePingTrends(node.id));

        syncTestPort();
        syncSuitePort();
    } finally {
        isRefreshingNodes = false;
    }
}

function renderStreamingBadges(nodeId) {
    const cache = streamingStatusCache[nodeId];
    if (isStreamingTestRunning && (!cache || cache.inProgress)) {
        return '<span class="text-xs text-emerald-300">流媒体测试中...</span>';
    }
    if (!cache) {
        return '<span class="text-xs text-slate-500">未检�?/span>';
    }

    if (cache.error) {
        return `<span class="text-xs text-amber-300">${cache.message || '检测异�?}</span>`;
    }

    const badges = streamingServices
        .map((svc) => {
            const status = cache[svc.key];
            const tier = status?.tier;
            let unlocked = null;
            if (svc.key === 'netflix' && tier) {
                unlocked = tier === 'full' ? true : (tier === 'originals' ? false : null);
            } else {
                unlocked = status ? (status.unlocked ?? (tier === 'full')) : null;
            }

            const detail = status && status.detail ? status.detail.replace(/"/g, "'") : '';
            const region = status?.region;

            let badgeClass = 'streaming-failed';
            let statusLabel = '未检�?;

            if (unlocked === true) {
                const isDnsUnlock = detail && (detail.toLowerCase().includes('dns') || detail.toLowerCase().includes('proxy'));
                badgeClass = isDnsUnlock ? 'streaming-dns' : 'streaming-native';
                statusLabel = '可解�?;
            } else if (unlocked === false) {
                badgeClass = 'streaming-failed';
                statusLabel = '未解�?;
            }

            if (svc.key === 'netflix' && status) {
                const netflixTier = tier || (unlocked ? 'full' : 'none');
                if (netflixTier === 'full') {
                    statusLabel = '全解�?;
                    badgeClass = 'streaming-native';
                } else if (netflixTier === 'originals') {
                    statusLabel = '自制�?;
                    badgeClass = 'streaming-dns';
                }
            }

            const regionBadge = region ? `<span class="region-corner">${region}</span>` : '';
            const title = `${region ? `[${region}] ` : ''}${svc.label}�?{statusLabel}${detail ? ' · ' + detail : ''}`;
            return `<span class="streaming-badge ${badgeClass}" title="${title}">${regionBadge}${svc.label}</span>`;
        })
        .join('');

    return `<div class="streaming-grid">${badges}</div>`;
}

function editNode(nodeId) {
    const node = nodeCache.find((n) => n.id === nodeId);
    if (!node) return;
    if (typeof nodeName !== 'undefined' && nodeName) nodeName.value = node.name;
    if (typeof nodeIp !== 'undefined' && nodeIp) nodeIp.value = node.ip;
    if (typeof nodePort !== 'undefined' && nodePort) nodePort.value = node.agent_port;
    if (typeof nodeIperf !== 'undefined' && nodeIperf) nodeIperf.value = node.iperf_port;
    if (typeof nodeDesc !== 'undefined' && nodeDesc) nodeDesc.value = node.description || '';
    editingNodeId = nodeId;
    if (typeof saveNodeBtn !== 'undefined' && saveNodeBtn) saveNodeBtn.textContent = '保存修改';
    if (typeof addNodeTitle !== 'undefined' && addNodeTitle) addNodeTitle.textContent = '编辑节点';
    openAddNodeModal();
}

async function saveNode() {
    if (typeof addNodeAlert !== 'undefined' && addNodeAlert) clearAlert(addNodeAlert);
    const payload = {
        name: nodeName.value,
        ip: nodeIp.value,
        agent_port: Number(nodePort.value || 8000),
        iperf_port: Number(nodeIperf.value || DEFAULT_IPERF_PORT),
        description: nodeDesc.value
    };

    const method = editingNodeId ? 'PUT' : 'POST';
    const url = editingNodeId ? `/nodes/${editingNodeId}` : '/nodes';

    const res = await apiFetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });

    if (!res.ok) {
        const msg = editingNodeId ? '更新节点失败，请检查字段�? : '保存节点失败，请检查字段�?;
        if (typeof addNodeAlert !== 'undefined' && addNodeAlert) setAlert(addNodeAlert, msg);
        return;
    }

    resetNodeForm();
    await refreshNodes();
    closeAddNodeModal();
    if (typeof addNodeAlert !== 'undefined' && addNodeAlert) clearAlert(addNodeAlert);
}

function syncTestPort() {
    if (typeof dstSelect === 'undefined' || !dstSelect || typeof testPortInput === 'undefined' || !testPortInput) return;
    const dst = nodeCache.find((n) => n.id === Number(dstSelect.value));
    if (dst) {
        const detected = dst.detected_iperf_port || dst.iperf_port;
        testPortInput.value = detected || DEFAULT_IPERF_PORT;
    }
}

function syncSuitePort() {
    if (typeof suiteDstSelect === 'undefined' || !suiteDstSelect || typeof suitePort === 'undefined' || !suitePort) return;
    const dst = nodeCache.find((n) => n.id === Number(suiteDstSelect.value));
    if (dst) {
        const detected = dst.detected_iperf_port || dst.iperf_port;
        suitePort.value = detected || DEFAULT_IPERF_PORT;
    }
}

async function runStreamingCheck(nodeId) {
    if (isStreamingTestRunning) return;
    const targetNode = nodeCache.find((n) => n.id === nodeId);
    if (!targetNode) {
        if (typeof addNodeAlert !== 'undefined' && addNodeAlert) setAlert(addNodeAlert, '节点不存在或尚未加载�?);
        return;
    }

    isStreamingTestRunning = true;
    if (typeof streamingProgressLabel !== 'undefined' && streamingProgressLabel) streamingProgressLabel.textContent = '流媒体测试中...';
    const expectedMs = Math.max(3500, 2000);
    let stopProgress = null;
    if (typeof streamingProgress !== 'undefined' && streamingProgress && typeof streamingProgressBar !== 'undefined' && streamingProgressBar && typeof streamingProgressLabel !== 'undefined' && streamingProgressLabel) {
        stopProgress = startProgressBar(streamingProgress, streamingProgressBar, streamingProgressLabel, expectedMs, '准备发起检�?..', false);
    }

    try {
        streamingStatusCache[nodeId] = { inProgress: true };
        updateNodeStreamingBadges(nodeId);
        if (typeof streamingProgressLabel !== 'undefined' && streamingProgressLabel) streamingProgressLabel.textContent = `${targetNode.name} 测试中`;
        try {
            const res = await apiFetch(`/nodes/${nodeId}/streaming-test`, { method: 'POST' });
            if (!res.ok) {
                streamingStatusCache[nodeId] = streamingStatusCache[nodeId] || {};
                streamingStatusCache[nodeId].error = true;
                streamingStatusCache[nodeId].message = `请求失败 (${res.status})`;
                updateNodeStreamingBadges(nodeId);
            } else {
                const data = await res.json();
                const byService = {};
                (data.services || []).forEach((svc) => {
                    const key = normalizeServiceKey(svc.key, svc.service);
                    byService[key] = {
                        unlocked: !!svc.unlocked,
                        detail: svc.detail,
                        service: svc.service,
                        tier: svc.tier,
                        region: svc.region,
                    };
                });
                streamingServices.forEach((svc) => {
                    if (!byService[svc.key]) {
                        byService[svc.key] = { unlocked: false, detail: '未检�? };
                    }
                });
                streamingStatusCache[nodeId] = byService;
                updateNodeStreamingBadges(nodeId);
            }
        } catch (err) {
            streamingStatusCache[nodeId] = { error: true, message: err?.message || '请求异常' };
            updateNodeStreamingBadges(nodeId);
        }

        if (stopProgress) stopProgress('检测完�?);
    } finally {
        isStreamingTestRunning = false;
    }
}

// ============== Tests Functions ==============
let testsCurrentPage = 1;
let testsAllData = [];

function getTestsPageSize() {
    const select = document.getElementById('tests-page-size');
    return select ? parseInt(select.value, 10) : 10;
}

function updateTestsPagination() {
    const pageSize = getTestsPageSize();
    const totalPages = Math.max(1, Math.ceil(testsAllData.length / pageSize));
    const pagination = document.getElementById('tests-pagination');
    const pageInfo = document.getElementById('tests-page-info');
    const prevBtn = document.getElementById('tests-prev');
    const nextBtn = document.getElementById('tests-next');

    if (testsAllData.length <= pageSize) {
        if (pagination) pagination.classList.add('hidden');
        return;
    }

    if (pagination) pagination.classList.remove('hidden');
    if (pageInfo) pageInfo.textContent = `�?${testsCurrentPage} �?/ �?${totalPages} 页`;
    if (prevBtn) prevBtn.disabled = testsCurrentPage <= 1;
    if (nextBtn) nextBtn.disabled = testsCurrentPage >= totalPages;
}

function renderTestsPage() {
    const pageSize = getTestsPageSize();
    const start = (testsCurrentPage - 1) * pageSize;
    const pageData = testsAllData.slice(start, start + pageSize);

    if (typeof testsList === 'undefined' || !testsList) return;
    testsList.innerHTML = '';
    if (!pageData.length) {
        testsList.textContent = '暂无测试记录�?;
        const pagination = document.getElementById('tests-pagination');
        if (pagination) pagination.classList.add('hidden');
        return;
    }

    renderTestCards(pageData);
    updateTestsPagination();
}

async function refreshTests() {
    const res = await apiFetch('/tests');
    const tests = await res.json();
    if (!tests.length) {
        if (typeof testsList !== 'undefined' && testsList) testsList.textContent = '暂无测试记录�?;
        const pagination = document.getElementById('tests-pagination');
        if (pagination) pagination.classList.add('hidden');
        testsAllData = [];
        return;
    }

    // Pre-process all test data
    const allEnrichedTests = tests.slice().reverse().map((test) => {
        const metrics = summarizeTestMetrics(test.raw_result || {});
        if (metrics?.isSuite) {
            const suiteEntries = normalizeSuiteEntries(test);
            return { test, metrics, suiteEntries };
        }
        const rateSummary = summarizeRateTable(test.raw_result || {});
        const latencyValue = metrics.latencyStats?.avg ?? (metrics.latencyMs ?? null);
        const jitterValue = metrics.jitterStats?.avg ?? (metrics.jitterMs ?? null);
        return { test, metrics, rateSummary, latencyValue, jitterValue };
    });

    // Store all tests for pagination
    testsAllData = allEnrichedTests;
    testsCurrentPage = 1;

    renderTestsPage();
}

function renderTestCards(enrichedTests) {
    if (typeof testsList === 'undefined' || !testsList) return;
    const detailBlocks = new Map();

    // Helper chips and rows
    const maxRate = Math.max(
        1,
        ...testsAllData
            .filter((item) => !item.metrics?.isSuite)
            .map(({ rateSummary }) => Math.max(rateSummary?.receiverRateValue || 0, rateSummary?.senderRateValue || 0))
    );

    const makeChip = (label) => {
        const span = document.createElement('span');
        span.className = 'inline-flex items-center gap-1 rounded-full border border-slate-800 bg-slate-900/70 px-2.5 py-1 text-[11px] font-semibold text-slate-200';
        span.textContent = label;
        return span;
    };

    const buildRateRow = (label, value, displayValue, gradient) => {
        const wrap = document.createElement('div');
        wrap.className = 'space-y-1 rounded-xl border border-slate-800/60 bg-slate-950/40 p-3';
        const header = document.createElement('div');
        header.className = 'flex items-center justify-between text-xs text-slate-400';
        header.innerHTML = `<span>${label}</span><span class="font-semibold text-slate-100">${displayValue}</span>`;

        const barWrap = document.createElement('div');
        barWrap.className = 'h-2 w-full overflow-hidden rounded-full bg-slate-800/80';
        const bar = document.createElement('div');
        if (value) {
            bar.className = `h-2 rounded-full bg-gradient-to-r ${gradient}`;
            bar.style.width = `${Math.min(100, (value / maxRate) * 100)}%`;
        } else {
            bar.className = 'h-2 rounded-full bg-slate-700';
            bar.style.width = '14%';
        }
        barWrap.appendChild(bar);
        wrap.appendChild(header);
        wrap.appendChild(barWrap);
        return wrap;
    };

    const toggleDetail = (testId, btn) => {
        const block = detailBlocks.get(testId);
        if (!block) return;
        const isHidden = block.classList.contains('hidden');
        if (isHidden) {
            block.classList.remove('hidden');
            btn.textContent = '收起';
        } else {
            block.classList.add('hidden');
            btn.textContent = '详情';
        }
    };

    enrichedTests.forEach(({ test, metrics, rateSummary, latencyValue, jitterValue, suiteEntries }) => {
        const pathLabel = `${formatNodeLabel(test.src_node_id)} �?${formatNodeLabel(test.dst_node_id)}`;

        if (metrics?.isSuite) {
            const card = document.createElement('div');
            card.className = 'group space-y-3 rounded-2xl border border-slate-800/70 bg-slate-900/60 p-4 shadow-sm shadow-black/30 transition hover:border-emerald-400/40 hover:shadow-emerald-500/10';
            const header = document.createElement('div');
            header.className = 'flex flex-wrap items-center justify-between gap-2';
            const title = document.createElement('div');
            title.innerHTML = `<p class="text-xs uppercase tracking-[0.2em] text-emerald-300/70">#${test.id} · TCP/UDP 双向测试</p>` +
                `<p class="text-lg font-semibold text-white">${pathLabel}</p>`;
            header.appendChild(title);

            const hasError = suiteEntries.some((entry) => entry.rateSummary?.status && entry.rateSummary.status !== 'ok');
            const statusPill = document.createElement('span');
            statusPill.className = 'inline-flex items-center gap-2 rounded-full bg-slate-800/70 px-3 py-1 text-xs font-semibold text-slate-200 ring-1 ring-slate-700';
            statusPill.textContent = hasError ? '部分异常' : '完成';
            header.appendChild(statusPill);
            card.appendChild(header);

            const suiteGrid = document.createElement('div');
            suiteGrid.className = 'grid gap-3 md:grid-cols-2';
            suiteEntries.forEach((entry) => {
                const tile = document.createElement('div');
                tile.className = 'space-y-2 rounded-xl border border-slate-800/60 bg-slate-950/40 p-3';
                const heading = document.createElement('div');
                heading.className = 'flex items-center justify-between text-sm text-slate-200';

                const labelGroup = document.createElement('div');
                labelGroup.className = 'flex items-center gap-2';
                const labelText = document.createElement('span');
                labelText.className = 'font-semibold';
                labelText.textContent = entry.label;
                labelGroup.appendChild(labelText);

                const badgeRow = document.createElement('div');
                badgeRow.className = 'flex items-center gap-1';
                const latencyV = entry.metrics?.latencyStats?.avg ?? entry.metrics?.latencyMs;
                if (latencyV !== undefined && latencyV !== null) {
                    badgeRow.appendChild(createMiniStat('RTT', formatMetric(latencyV, 2), 'ms', 'text-sky-200', entry.metrics?.latencyStats));
                }
                const jitterV = entry.metrics?.jitterStats?.avg ?? entry.metrics?.jitterMs;
                if (jitterV !== undefined && jitterV !== null) {
                    badgeRow.appendChild(createMiniStat('抖动', formatMetric(jitterV, 2), 'ms', 'text-amber-200', entry.metrics?.jitterStats));
                }
                const lossV = entry.metrics?.lossStats?.avg ?? entry.metrics?.lostPercent;
                if (lossV !== undefined && lossV !== null) {
                    badgeRow.appendChild(createMiniStat('丢包', formatMetric(lossV, 2), '%', 'text-rose-200', entry.metrics?.lossStats));
                }
                const retransV = entry.metrics?.retransStats?.avg;
                if (retransV !== undefined && retransV !== null) {
                    badgeRow.appendChild(createMiniStat('重传', formatMetric(retransV, 0), '�?, 'text-indigo-200', entry.metrics?.retransStats));
                }
                if (badgeRow.childNodes.length) {
                    labelGroup.appendChild(badgeRow);
                }

                const protoLabel = document.createElement('span');
                protoLabel.className = 'text-[11px] uppercase text-slate-400';
                protoLabel.textContent = `${entry.protocol.toUpperCase()}${entry.reverse ? ' (-R)' : ''}`;

                heading.appendChild(labelGroup);
                heading.appendChild(protoLabel);
                tile.appendChild(heading);

                const rates = document.createElement('div');
                rates.className = 'grid grid-cols-2 gap-2 text-xs text-slate-400';
                rates.innerHTML = `
          <div class="rounded-lg border border-slate-800/60 bg-slate-900/60 p-2">
            <div class="flex items-center justify-between"><span>接收</span><span class="font-semibold text-emerald-200">${entry.rateSummary.receiverRateMbps}</span></div>
          </div>
          <div class="rounded-lg border border-slate-800/60 bg-slate-900/60 p-2">
            <div class="flex items-center justify-between"><span>发�?/span><span class="font-semibold text-amber-200">${entry.rateSummary.senderRateMbps}</span></div>
          </div>`;
                tile.appendChild(rates);
                suiteGrid.appendChild(tile);
            });
            card.appendChild(suiteGrid);

            const actions = document.createElement('div');
            actions.className = 'flex flex-wrap items-center justify-between gap-3';
            const buttons = document.createElement('div');
            buttons.className = 'flex flex-wrap gap-2 translate-y-1 opacity-0 transition duration-200 pointer-events-none group-hover:translate-y-0 group-hover:opacity-100 group-hover:pointer-events-auto';
            const detailsBtn = document.createElement('button');
            detailsBtn.textContent = '详情';
            detailsBtn.className = styles.pillInfo;
            detailsBtn.onclick = () => toggleDetail(test.id, detailsBtn);
            const deleteBtn = document.createElement('button');
            deleteBtn.textContent = '删除';
            deleteBtn.className = styles.pillDanger;
            deleteBtn.onclick = () => deleteTestResult(test.id);
            buttons.appendChild(detailsBtn);
            buttons.appendChild(deleteBtn);
            actions.appendChild(buttons);
            card.appendChild(actions);

            const block = buildSuiteDetailsBlock(test, suiteEntries, pathLabel);
            detailBlocks.set(test.id, block);
            testsList.appendChild(card);
            testsList.appendChild(block);
            return;
        }

        const typeLabel = `${test.protocol.toUpperCase()}${test.params?.reverse ? ' (-R)' : ''}`;
        const card = document.createElement('div');
        card.className = 'group space-y-3 rounded-2xl border border-slate-800/70 bg-slate-900/60 p-4 shadow-sm shadow-black/30 transition hover:border-sky-400/40 hover:shadow-sky-500/10';
        const header = document.createElement('div');
        header.className = 'flex flex-wrap items-center justify-between gap-2';
        const title = document.createElement('div');
        title.innerHTML = `<p class="text-xs uppercase tracking-[0.2em] text-sky-300/70">#${test.id} · ${typeLabel}</p>` +
            `<p class="text-lg font-semibold text-white">${pathLabel}</p>`;
        header.appendChild(title);
        const statusPill = document.createElement('span');
        statusPill.className = 'inline-flex items-center gap-2 rounded-full bg-slate-800/70 px-3 py-1 text-xs font-semibold text-slate-200 ring-1 ring-slate-700';
        statusPill.textContent = rateSummary.status === 'ok' ? '完成' : (rateSummary.status || '未知');
        header.appendChild(statusPill);
        card.appendChild(header);

        const quickStats = document.createElement('div');
        quickStats.className = 'flex flex-wrap items-center gap-2 text-xs';
        if (latencyValue !== undefined && latencyValue !== null) {
            quickStats.appendChild(createMiniStat('RTT', formatMetric(latencyValue, 2), 'ms', 'text-sky-200', metrics.latencyStats));
        }
        if (jitterValue !== undefined && jitterValue !== null) {
            quickStats.appendChild(createMiniStat('抖动', formatMetric(jitterValue, 2), 'ms', 'text-amber-200', metrics.jitterStats));
        }
        const lossValue = metrics.lossStats?.avg ?? metrics.lostPercent;
        if (lossValue !== undefined && lossValue !== null) {
            quickStats.appendChild(createMiniStat('丢包', formatMetric(lossValue, 2), '%', 'text-rose-200', metrics.lossStats));
        }
        const retransValue = metrics.retransStats?.avg;
        if (retransValue !== undefined && retransValue !== null) {
            quickStats.appendChild(createMiniStat('重传', formatMetric(retransValue, 2), '�?, 'text-indigo-200', metrics.retransStats));
        }
        if (quickStats.childNodes.length) card.appendChild(quickStats);

        const ratesGrid = document.createElement('div');
        ratesGrid.className = 'grid gap-3 sm:grid-cols-2';
        ratesGrid.appendChild(buildRateRow('接收速率 (Mbps)', rateSummary.receiverRateValue, rateSummary.receiverRateMbps, 'from-emerald-400 to-sky-500'));
        ratesGrid.appendChild(buildRateRow('发送速率 (Mbps)', rateSummary.senderRateValue, rateSummary.senderRateMbps, 'from-amber-400 to-rose-500'));
        card.appendChild(ratesGrid);

        const metaChips = document.createElement('div');
        metaChips.className = 'flex flex-wrap items-center gap-2 text-xs text-slate-400';
        metaChips.appendChild(makeChip(test.protocol.toLowerCase() === 'udp' ? 'UDP 测试' : 'TCP 测试'));
        if (test.params?.reverse) metaChips.appendChild(makeChip('反向 (-R)'));
        card.appendChild(metaChips);

        const actions = document.createElement('div');
        actions.className = 'flex flex-wrap items-center justify-between gap-3';
        const buttons = document.createElement('div');
        buttons.className = 'flex flex-wrap gap-2 translate-y-1 opacity-0 transition duration-200 pointer-events-none group-hover:translate-y-0 group-hover:opacity-100 group-hover:pointer-events-auto';
        const detailsBtn = document.createElement('button');
        detailsBtn.textContent = '详情';
        detailsBtn.className = styles.pillInfo;
        detailsBtn.onclick = () => toggleDetail(test.id, detailsBtn);
        const deleteBtn = document.createElement('button');
        deleteBtn.textContent = '删除';
        deleteBtn.className = styles.pillDanger;
        deleteBtn.onclick = () => deleteTestResult(test.id);
        buttons.appendChild(detailsBtn);
        buttons.appendChild(deleteBtn);

        const congestion = document.createElement('span');
        congestion.className = 'rounded-full bg-slate-800/80 px-3 py-1 text-xs font-semibold text-slate-300 ring-1 ring-slate-700';
        congestion.textContent = `拥塞�?{rateSummary.senderCongestion} / ${rateSummary.receiverCongestion}`;

        actions.appendChild(buttons);
        actions.appendChild(congestion);
        card.appendChild(actions);

        const block = buildTestDetailsBlock(test, metrics, latencyValue, pathLabel);
        detailBlocks.set(test.id, block);

        testsList.appendChild(card);
        testsList.appendChild(block);
    });
}

function summarizeTestMetrics(raw) {
    if (raw?.mode === 'suite' && Array.isArray(raw.tests)) {
        const entries = raw.tests.map((entry) => {
            const detailed = entry.raw || entry;
            const summary = entry.summary || {};
            const merged = { ...summary, ...detailed };
            if (!merged.server_output_json && detailed.server_output_json) {
                merged.server_output_json = detailed.server_output_json;
            }

            return {
                label: entry.label || '子测�?,
                protocol: entry.protocol || 'tcp',
                reverse: !!entry.reverse,
                metrics: summarizeSingleMetrics(merged),
                raw: detailed,
            };
        });
        const valid = entries.map((e) => e.metrics).filter(Boolean);
        const avgBits = valid.length
            ? valid.reduce((sum, item) => sum + (item.bitsPerSecond || 0), 0) / valid.length
            : null;
        return { isSuite: true, entries, bitsPerSecond: avgBits };
    }
    return summarizeSingleMetrics(raw);
}

function summarizeSingleMetrics(raw) {
    const body = (raw && raw.iperf_result) || raw || {};
    const end = (body && body.end) || {};
    const sumReceived = end.sum_received || end.sum;
    const sumSent = end.sum_sent || end.sum;
    const firstStream = (end.streams && end.streams.length) ? end.streams[0] : null;
    const receiverStream = firstStream && firstStream.receiver ? firstStream.receiver : null;
    const senderStream = firstStream && firstStream.sender ? firstStream.sender : null;
    const pickFirst = (...values) => values.find((v) => v !== undefined && v !== null);

    // Calc loss from packets first
    const lossFromPackets = sumReceived && sumReceived.lost_packets !== undefined && sumReceived.packets
        ? (sumReceived.lost_packets / sumReceived.packets) * 100
        : undefined;

    const stats = collectMetricStats(raw); // Assumed to be defined in next chunk or above

    const bitsPerSecond = pickFirst(sumReceived?.bits_per_second, receiverStream?.bits_per_second, sumSent?.bits_per_second, senderStream?.bits_per_second);
    const jitterMs = stats?.jitter?.avg ?? pickFirst(sumReceived?.jitter_ms, sumSent?.jitter_ms, receiverStream?.jitter_ms, senderStream?.jitter_ms);
    const lostPercent = stats?.loss?.avg ?? pickFirst(sumReceived?.lost_percent, lossFromPackets, sumSent?.lost_percent, receiverStream?.lost_percent, senderStream?.lost_percent);

    let latencyMs = stats?.latency?.avg ?? pickFirst(senderStream?.mean_rtt, senderStream?.rtt, receiverStream?.mean_rtt, receiverStream?.rtt);
    if (latencyMs !== undefined && latencyMs !== null && latencyMs > 1000) latencyMs = latencyMs / 1000;

    return {
        bitsPerSecond,
        jitterMs,
        lostPercent,
        latencyMs,
        jitterStats: stats?.jitter || null,
        lossStats: stats?.loss || null,
        latencyStats: stats?.latency || null,
        retransStats: stats?.retrans || null,
    };
}

// ... Metric Collection Helpers ...
// collectMetricStats, computeStats, normalizeLatency are pure helpers.

function normalizeLatency(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return null;
    return num > 1000 ? num / 1000 : num;
}

function computeStats(values) {
    const filtered = values.filter((v) => Number.isFinite(v));
    if (!filtered.length) return null;
    const max = Math.max(...filtered);
    const min = Math.min(...filtered);
    const avg = filtered.reduce((sum, val) => sum + val, 0) / filtered.length;
    return { min, max, avg };
}

function collectMetricStats(raw) {
    const jitterValues = [];
    const lossValues = [];
    const latencyValues = [];
    const retransValues = [];

    const pushNumber = (arr, value, normalizer = (v) => v) => {
        const normalized = normalizer(value);
        if (Number.isFinite(normalized)) arr.push(normalized);
    };

    const consumeResult = (result) => {
        if (!result) return;
        const intervals = Array.isArray(result.intervals) ? result.intervals : [];
        const end = result.end || {};
        const streams = Array.isArray(end.streams) ? end.streams : [];
        const sumReceived = end.sum_received || end.sum || {};
        const sumSent = end.sum_sent || end.sum || {};

        const appendStreamMetrics = (stream) => {
            if (!stream) return;
            const sender = stream.sender || stream.sum_sent || stream;
            const receiver = stream.receiver || stream.sum_received || stream;
            [sender, receiver].forEach((endpoint) => {
                if (!endpoint) return;
                pushNumber(latencyValues, endpoint.rtt, normalizeLatency);
                pushNumber(latencyValues, endpoint.mean_rtt, normalizeLatency);
                pushNumber(latencyValues, endpoint.max_rtt, normalizeLatency);
                pushNumber(latencyValues, endpoint.min_rtt, normalizeLatency);
                pushNumber(jitterValues, endpoint.jitter_ms, Number);
                pushNumber(retransValues, endpoint.retransmits, Number);
                if (endpoint.lost_percent !== undefined) pushNumber(lossValues, endpoint.lost_percent, Number);
                if (endpoint.lost_packets !== undefined && endpoint.packets) {
                    pushNumber(lossValues, (endpoint.lost_packets / endpoint.packets) * 100, Number);
                }
            });
        };

        intervals.forEach((interval) => {
            const sum = interval?.sum || {};
            pushNumber(jitterValues, sum.jitter_ms, Number);
            if (sum.lost_percent !== undefined) pushNumber(lossValues, sum.lost_percent, Number);
            if (sum.lost_packets !== undefined && sum.packets) {
                pushNumber(lossValues, (sum.lost_packets / sum.packets) * 100, Number);
            }
            const streamsInInterval = Array.isArray(interval?.streams) ? interval.streams : [];
            streamsInInterval.forEach(appendStreamMetrics);
        });

        appendStreamMetrics(streams[0]);
        pushNumber(jitterValues, sumReceived.jitter_ms, Number);
        pushNumber(jitterValues, sumSent.jitter_ms, Number);
        if (sumReceived.lost_percent !== undefined) pushNumber(lossValues, sumReceived.lost_percent, Number);
        if (sumSent.lost_percent !== undefined) pushNumber(lossValues, sumSent.lost_percent, Number);
        if (sumReceived.lost_packets !== undefined && sumReceived.packets) {
            pushNumber(lossValues, (sumReceived.lost_packets / sumReceived.packets) * 100, Number);
        }
    };

    const baseResult = (raw && raw.iperf_result) || raw || {};
    const extraServerResult = baseResult?.server_output_json;
    [baseResult, extraServerResult].forEach(consumeResult);

    return {
        latency: computeStats(latencyValues),
        jitter: computeStats(jitterValues),
        loss: computeStats(lossValues),
        retrans: computeStats(retransValues),
    };
}

function summarizeSingleRateTable(raw) {
    const result = raw && raw.iperf_result ? raw.iperf_result : raw;
    const end = (result && result.end) || {};
    const sumSent = end.sum_sent || end.sum || {};
    const sumReceived = end.sum_received || end.sum || {};

    return {
        senderRateMbps: sumSent.bits_per_second ? formatMetric(sumSent.bits_per_second / 1e6, 2) : 'N/A',
        receiverRateMbps: sumReceived.bits_per_second ? formatMetric(sumReceived.bits_per_second / 1e6, 2) : 'N/A',
        senderRateValue: sumSent.bits_per_second ? sumSent.bits_per_second / 1e6 : null,
        receiverRateValue: sumReceived.bits_per_second ? sumReceived.bits_per_second / 1e6 : null,
        senderCongestion: end.sender_tcp_congestion || 'N/A',
        receiverCongestion: end.receiver_tcp_congestion || 'N/A',
        status: raw && raw.status ? raw.status : 'unknown',
    };
}

function summarizeRateTable(raw) {
    if (raw?.mode === 'suite' && Array.isArray(raw.tests)) {
        return {
            mode: 'suite',
            tests: raw.tests.map((entry) => ({
                label: entry.label || '子测�?,
                protocol: entry.protocol || 'tcp',
                reverse: !!entry.reverse,
                summary: summarizeSingleRateTable(entry.raw || entry),
            })),
        };
    }
    return summarizeSingleRateTable(raw);
}

function normalizeSuiteEntries(test) {
    const raw = test.raw_result || {};
    const metrics = summarizeTestMetrics(raw);
    const rateInfo = summarizeRateTable(raw);
    const rateMap = new Map();
    (rateInfo.tests || []).forEach((entry) => {
        rateMap.set(entry.label, entry.summary);
    });

    return (metrics.entries || []).map((entry, idx) => {
        const key = entry.label || `子测�?${idx + 1}`;
        return {
            label: key,
            protocol: entry.protocol,
            reverse: entry.reverse,
            metrics: entry.metrics,
            rateSummary: rateMap.get(key) || summarizeSingleRateTable(entry.raw || entry),
            raw: entry.raw,
        };
    });
}

function createMiniStat(label, value, unit = '', accent = 'text-sky-200', stats = null) {
    const wrap = document.createElement('div');
    wrap.className = 'relative inline-block';
    const badge = document.createElement('div');
    badge.className = 'inline-flex items-center gap-1 rounded-lg border border-slate-800/80 bg-slate-900/70 px-2 py-1 text-[11px] font-semibold text-slate-200';
    const unitSpan = unit ? `<span class="text-slate-500">${unit}</span>` : '';
    badge.innerHTML = `<span class="text-slate-400">${label}</span><span class="${accent}">${value}</span>${unitSpan}`;
    wrap.appendChild(badge);

    if (stats) {
        const detail = document.createElement('div');
        detail.className = 'pointer-events-none absolute left-1/2 top-full z-20 mt-2 w-max min-w-[180px] -translate-x-1/2 scale-95 rounded-lg border border-slate-800/80 bg-slate-900/95 px-3 py-2 text-[11px] text-slate-200 opacity-0 shadow-2xl shadow-black/30 transition duration-150';
        const primary = stats.avg ?? stats.mean ?? stats.max ?? stats.min;
        const unitLabel = unit ? ` ${unit}` : '';
        detail.innerHTML = `
      <div class="text-[11px] font-semibold text-slate-300">${label} 均�?{unit ? ` (${unit})` : ''}</div>
      <div class="mt-1 text-sm font-bold text-white">${formatMetric(primary)}${unitLabel}</div>
      <div class="mt-1 text-[10px] text-slate-500">max ${formatMetric(stats.max)}${unitLabel} · min ${formatMetric(stats.min)}${unitLabel}</div>
    `;
        wrap.appendChild(detail);
        wrap.onmouseenter = () => { detail.classList.remove('opacity-0', 'scale-95'); detail.classList.add('opacity-100', 'scale-100'); };
        wrap.onmouseleave = () => { detail.classList.add('opacity-0', 'scale-95'); detail.classList.remove('opacity-100', 'scale-100'); };
    }
    return wrap;
}

function renderRawResult(raw) {
    // ... Implement robust rendering of raw JSON/iperf result ...
    const wrap = document.createElement('div');
    wrap.className = 'overflow-auto rounded-xl border border-slate-800/70 bg-slate-950/60 p-3';

    if (!raw) {
        wrap.textContent = '无原始结果�?;
        return wrap;
    }

    const result = raw.iperf_result || raw;
    const end = result.end || {};
    const sumSent = end.sum_sent || {};
    const sumReceived = end.sum_received || {};

    const summaryTable = document.createElement('table');
    summaryTable.className = styles.table + ' mb-3';

    const addSummaryRow = (label, value) => {
        const row = document.createElement('tr');
        const l = document.createElement('th');
        l.textContent = label;
        l.className = styles.tableCell + ' font-semibold text-slate-200';
        const v = document.createElement('td');
        v.textContent = value;
        v.className = styles.tableCell + ' text-slate-100';
        row.appendChild(l);
        row.appendChild(v);
        summaryTable.appendChild(row);
    };

    addSummaryRow('状�?, raw.status || 'unknown');
    addSummaryRow('发送速率 (Mbps)', sumSent.bits_per_second ? formatMetric(sumSent.bits_per_second / 1e6) : 'N/A');
    addSummaryRow('接收速率 (Mbps)', sumReceived.bits_per_second ? formatMetric(sumReceived.bits_per_second / 1e6) : 'N/A');
    addSummaryRow('发送拥塞控�?, end.sender_tcp_congestion || 'N/A');
    addSummaryRow('接收拥塞控制', end.receiver_tcp_congestion || 'N/A');
    wrap.appendChild(summaryTable);

    const intervals = result.intervals || [];
    if (!intervals.length) {
        const fallback = document.createElement('pre');
        fallback.className = styles.codeBlock;
        fallback.textContent = JSON.stringify(result, null, 2);
        wrap.appendChild(fallback);
        return wrap;
    }

    // Interval table rendering ...
    const intervalTable = document.createElement('table');
    intervalTable.className = styles.table;
    const headerRow = document.createElement('tr');
    headerRow.className = styles.tableHeader;
    ['时间区间 (s)', '速率 (Mbps)', '重传', 'RTT (ms)', 'CWND', '窗口'].forEach((label) => {
        const th = document.createElement('th');
        th.textContent = label;
        th.className = styles.tableCell + ' font-semibold';
        headerRow.appendChild(th);
    });
    intervalTable.appendChild(headerRow);

    intervals.forEach((interval) => {
        const stream = (interval.streams && interval.streams[0]) || interval.sum || {};
        const start = stream.start ?? 0;
        const endTime = stream.end ?? (stream.seconds ? start + stream.seconds : start);
        const rate = stream.bits_per_second ? `${formatMetric(stream.bits_per_second / 1e6)} Mbps` : 'N/A';
        let rtt = stream.rtt ?? stream.mean_rtt;
        if (rtt && rtt > 1000) rtt = rtt / 1000;

        const cells = [
            `${formatMetric(start, 3)} - ${formatMetric(endTime, 3)}`,
            rate,
            stream.retransmits ?? 'N/A',
            rtt ? `${formatMetric(rtt)}` : 'N/A',
            stream.snd_cwnd ? `${stream.snd_cwnd}` : 'N/A',
            stream.snd_wnd ? `${stream.snd_wnd}` : 'N/A',
        ];

        const row = document.createElement('tr');
        cells.forEach((value) => {
            const td = document.createElement('td');
            td.textContent = value;
            td.className = styles.tableCell;
            row.appendChild(td);
        });
        intervalTable.appendChild(row);
    });

    wrap.appendChild(intervalTable);
    return wrap;
}

function buildSuiteDetailsBlock(test, suiteEntries, pathLabel) {
    const block = document.createElement('div');
    block.className = 'hidden rounded-xl border border-slate-800/60 bg-slate-900/60 p-3 shadow-inner shadow-black/20';
    block.dataset.testId = test.id;
    // ... header ...
    const header = document.createElement('div');
    header.className = 'flex flex-col gap-2 md:flex-row md:items-center md:justify-between';
    const summary = document.createElement('div');
    summary.innerHTML = `<strong>#${test.id} ${pathLabel}</strong> · 双向测试 · 端口 ${test.params.port} · 时长 ${test.params.duration}s`;
    header.appendChild(summary);
    const deleteBtn = document.createElement('button');
    deleteBtn.textContent = '删除记录';
    deleteBtn.className = styles.pillDanger;
    deleteBtn.onclick = () => deleteTestResult(test.id);
    header.appendChild(deleteBtn);
    block.appendChild(header);

    suiteEntries.forEach((entry) => {
        const section = document.createElement('div');
        section.className = 'mt-3 space-y-2 rounded-xl border border-slate-800/60 bg-slate-950/50 p-3';
        section.innerHTML = `<div class="flex items-center justify-between text-sm text-slate-200"><span class="font-semibold">${entry.label}</span><span class="text-xs uppercase text-slate-400">${entry.protocol.toUpperCase()}${entry.reverse ? ' (-R)' : ''}</span></div>`;
        section.appendChild(renderRawResult(entry.raw || {}));
        block.appendChild(section);
    });

    return block;
}

function buildTestDetailsBlock(test, metrics, latencyValue, pathLabel) {
    const block = document.createElement('div');
    block.className = 'hidden rounded-xl border border-slate-800/60 bg-slate-900/60 p-3 shadow-inner shadow-black/20';
    block.dataset.testId = test.id;
    const header = document.createElement('div');
    header.className = 'flex flex-col gap-3 md:flex-row md:items-center md:justify-between';
    const summary = document.createElement('div');
    const directionLabel = test.params?.reverse ? ' (反向)' : '';
    summary.innerHTML = `<strong>#${test.id} ${pathLabel}</strong> · ${test.protocol.toUpperCase()}${directionLabel} · 端口 ${test.params.port} · 时长 ${test.params.duration}s<br/>` +
        `<span class="${styles.textMutedSm}">速率: ${metrics.bitsPerSecond ? formatMetric(metrics.bitsPerSecond / 1e6, 2) + ' Mbps' : 'N/A'} | 时延: ${latencyValue !== null ? formatMetric(latencyValue) + ' ms' : 'N/A'} | 丢包: ${metrics.lostPercent !== undefined && metrics.lostPercent !== null ? formatMetric(metrics.lostPercent) + '%' : 'N/A'}</span>`;
    header.appendChild(summary);

    const actions = document.createElement('div');
    actions.className = styles.inline;
    const deleteBtn = document.createElement('button');
    deleteBtn.textContent = '删除';
    deleteBtn.className = styles.pillDanger;
    deleteBtn.onclick = () => deleteTestResult(test.id);
    actions.appendChild(deleteBtn);
    header.appendChild(actions);

    block.appendChild(header);
    const rawTable = renderRawResult(test.raw_result);
    rawTable.classList.add('mt-3');
    block.appendChild(rawTable);
    return block;
}

async function runTest() {
    if (typeof testAlert !== 'undefined' && testAlert) clearAlert(testAlert);
    const selectedDst = typeof nodeCache !== 'undefined' ? nodeCache.find((n) => n.id === Number(dstSelect.value)) : null;
    const payload = {
        src_node_id: Number(srcSelect.value),
        dst_node_id: Number(dstSelect.value),
        protocol: protocolSelect.value,
        duration: Number(document.getElementById('duration').value),
        parallel: Number(document.getElementById('parallel').value),
        port: Number(testPortInput.value || (selectedDst ? (selectedDst.detected_iperf_port || selectedDst.iperf_port) : DEFAULT_IPERF_PORT)),
        reverse: reverseToggle?.checked || false,
    };

    const omitValue = Number(omitInput.value || 0);
    if (omitValue > 0) payload.omit = omitValue;

    if (payload.protocol === 'tcp') {
        const tcpBw = tcpBandwidthInput.value.trim();
        if (tcpBw) payload.bandwidth = tcpBw;
    } else {
        const udpBw = udpBandwidthInput.value.trim();
        if (udpBw) payload.bandwidth = udpBw;
        const udpLen = Number(udpLenInput.value || 0);
        if (udpLen > 0) payload.datagram_size = udpLen;
    }

    let finishProgress = null;
    if (typeof testProgress !== 'undefined' && testProgress && typeof testProgressBar !== 'undefined' && testProgressBar && typeof testProgressLabel !== 'undefined' && testProgressLabel) {
        finishProgress = startProgressBar(testProgress, testProgressBar, testProgressLabel, payload.duration * 1000 + 1500, '开始链路测�?..');
    }

    const res = await apiFetch('/tests', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });

    if (!res.ok) {
        const details = await res.text();
        const message = details ? `启动测试失败�?{details}` : '启动测试失败，请确认节点存在且参数有效�?;
        if (typeof testAlert !== 'undefined' && testAlert) setAlert(testAlert, message);
        if (finishProgress) finishProgress('测试失败');
        return;
    }

    await refreshTests();
    if (finishProgress) finishProgress('测试完成');
    if (typeof testAlert !== 'undefined' && testAlert) clearAlert(testAlert);
}

async function runSuiteTest() {
    if (typeof testAlert !== 'undefined' && testAlert) clearAlert(testAlert);
    const selectedDst = typeof nodeCache !== 'undefined' ? nodeCache.find((n) => n.id === Number(suiteDstSelect.value)) : null;

    const payload = {
        src_node_id: Number(suiteSrcSelect.value),
        dst_node_id: Number(suiteDstSelect.value),
        duration: Number(suiteDuration.value || 10),
        parallel: Number(suiteParallel.value || 1),
        port: Number(suitePort.value || (selectedDst ? (selectedDst.detected_iperf_port || selectedDst.iperf_port) : DEFAULT_IPERF_PORT)),
    };

    const omitValue = Number(suiteOmit?.value || 0);
    if (omitValue > 0) payload.omit = omitValue;

    const tcpBw = suiteTcpBandwidth?.value.trim();
    if (tcpBw) payload.tcp_bandwidth = tcpBw;
    const udpBw = suiteUdpBandwidth?.value.trim();
    if (udpBw) payload.udp_bandwidth = udpBw;
    const udpLen = Number(suiteUdpLen?.value || 0);
    if (udpLen > 0) payload.udp_datagram_size = udpLen;

    const expectedMs = payload.duration * 4000 + 3000;
    let finishProgress = null;
    if (typeof testProgress !== 'undefined' && testProgress && typeof testProgressBar !== 'undefined' && testProgressBar && typeof testProgressLabel !== 'undefined' && testProgressLabel) {
        finishProgress = startProgressBar(testProgress, testProgressBar, testProgressLabel, expectedMs, '准备执行 4 轮双向测�?..');
    }

    const res = await apiFetch('/tests/suite', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });

    if (!res.ok) {
        const details = await res.text();
        const message = details ? `启动双向测试失败�?{details}` : '启动双向测试失败，请确认节点存在且参数有效�?;
        if (typeof testAlert !== 'undefined' && testAlert) setAlert(testAlert, message);
        if (finishProgress) finishProgress('测试失败');
        return;
    }

    await refreshTests();
    if (finishProgress) finishProgress('双向测试完成');
    if (typeof testAlert !== 'undefined' && testAlert) clearAlert(testAlert);
}

async function deleteTestResult(testId) {
    if (typeof testAlert !== 'undefined' && testAlert) clearAlert(testAlert);
    const res = await apiFetch(`/tests/${testId}`, { method: 'DELETE' });
    if (!res.ok) {
        if (typeof testAlert !== 'undefined' && testAlert) setAlert(testAlert, '删除记录失败�?);
        return;
    }
    await refreshTests();
}

async function clearAllTests() {
    if (typeof testAlert !== 'undefined' && testAlert) clearAlert(testAlert);
    const res = await apiFetch('/tests', { method: 'DELETE' });
    if (!res.ok) {
        if (typeof testAlert !== 'undefined' && testAlert) setAlert(testAlert, '清空失败�?);
        return;
    }
    await refreshTests();
}

function formatMetric(value, decimals = 2) {
    if (value === undefined || value === null || Number.isNaN(value)) return 'N/A';
    return Number(value).toFixed(decimals);
}

// ============== Initialization ==============
function ensureAutoRefresh() {
    if (typeof nodeRefreshInterval !== 'undefined' && nodeRefreshInterval) return;
    if (typeof refreshNodes === 'function') {
        nodeRefreshInterval = setInterval(() => refreshNodes(), 10000);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    // Other event listeners that wait for DOM elements
    if (document.getElementById('logout-btn')) document.getElementById('logout-btn').addEventListener('click', logout);
    if (document.getElementById('run-test')) document.getElementById('run-test').addEventListener('click', runTest);
    if (document.getElementById('run-suite-test')) document.getElementById('run-suite-test').addEventListener('click', runSuiteTest);

    if (typeof protocolSelect !== 'undefined' && protocolSelect) protocolSelect.addEventListener('change', toggleProtocolOptions);
    if (typeof singleTestTab !== 'undefined' && singleTestTab) singleTestTab.addEventListener('click', () => setActiveTestTab('single'));
    if (typeof suiteTestTab !== 'undefined' && suiteTestTab) suiteTestTab.addEventListener('click', () => setActiveTestTab('suite'));
    if (typeof suiteDstSelect !== 'undefined' && suiteDstSelect) suiteDstSelect.addEventListener('change', syncSuitePort);
    if (typeof suiteSrcSelect !== 'undefined' && suiteSrcSelect) suiteSrcSelect.addEventListener('change', syncSuitePort);
    if (typeof changePasswordBtn !== 'undefined' && changePasswordBtn) changePasswordBtn.addEventListener('click', changePassword);
    if (typeof saveNodeBtn !== 'undefined' && saveNodeBtn) saveNodeBtn.addEventListener('click', saveNode);
    if (typeof importConfigsBtn !== 'undefined' && importConfigsBtn) importConfigsBtn.addEventListener('click', () => configFileInput?.click());
    if (typeof exportConfigsBtn !== 'undefined' && exportConfigsBtn) exportConfigsBtn.addEventListener('click', exportAgentConfigs);
    if (typeof configFileInput !== 'undefined' && configFileInput) configFileInput.addEventListener('change', (e) => importAgentConfigs(e.target.files[0]));

    if (document.getElementById('refresh-tests')) {
        document.getElementById('refresh-tests').addEventListener('click', refreshTests);
    }

    if (typeof deleteAllTestsBtn !== 'undefined' && deleteAllTestsBtn) deleteAllTestsBtn.addEventListener('click', clearAllTests);

    // Pagination
    document.getElementById('tests-prev')?.addEventListener('click', () => {
        if (testsCurrentPage > 1) {
            testsCurrentPage--;
            renderTestsPage();
        }
    });
    document.getElementById('tests-next')?.addEventListener('click', () => {
        const pageSize = getTestsPageSize();
        const totalPages = Math.ceil(testsAllData.length / pageSize);
        if (testsCurrentPage < totalPages) {
            testsCurrentPage++;
            renderTestsPage();
        }
    });
    document.getElementById('tests-page-size')?.addEventListener('change', () => {
        testsCurrentPage = 1;
        renderTestsPage();
    });

    document.querySelectorAll('[data-refresh-nodes]').forEach((btn) => btn.addEventListener('click', refreshNodes));
    if (typeof dstSelect !== 'undefined' && dstSelect) dstSelect.addEventListener('change', syncTestPort);

    // Initial setup
    if (typeof toggleProtocolOptions === 'function') toggleProtocolOptions();
    if (typeof setActiveTestTab === 'function') setActiveTestTab('single');
    if (typeof syncSuitePort === 'function') syncSuitePort();
    if (typeof ensureAutoRefresh === 'function') ensureAutoRefresh();
});
