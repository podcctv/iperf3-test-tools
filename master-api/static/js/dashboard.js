import { apiFetch } from './api.js';
    console.log('Script loading...');
    
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
      } catch(e) { /* Not a valid URL, use as-is */ }
      
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
          document.getElementById('notify-daily-report').checked = telegramConfig.notify_daily_report ?? false;
          
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
                <span class="inline-flex items-center px-2 py-1 rounded-md text-xs font-semibold ${
                  isCIDR ? 'bg-purple-500/20 text-purple-300' :
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



    // Event listeners binding specific for whitelist buttons
    // We bind these here because these elements might be inside the modal which is statically defined in HTML
    document.addEventListener('DOMContentLoaded', () => {
        console.log('DOM fully loaded. Initializing elements...');
        
        // Login elements
        loginForm = document.getElementById('login-form');
        loginCard = document.getElementById('login-card');
        appCard = document.getElementById('app-card');
        loginAlert = document.getElementById('login-alert');
        loginButton = document.getElementById('login-btn');
        passwordInput = document.getElementById('password-input');
        loginStatus = document.getElementById('login-status');
        loginStatusDot = document.getElementById('login-status-dot');
        loginStatusLabel = document.getElementById('login-status-label');
        loginHint = document.getElementById('login-hint');
        authHint = document.getElementById('auth-hint');
        originalLoginLabel = loginButton?.textContent || 'Login';


        // Note: We removed the old whitelist-display close button
        // The new UI uses a table-based display that doesn't need manual closing

        // NOTE: Settings modal tab buttons use inline onclick handlers
        // Do NOT add addEventListener here as it will conflict with onclick
        
        // Config elements
        configAlert = document.getElementById('config-alert');
        importConfigsBtn = document.getElementById('import-configs');
        exportConfigsBtn = document.getElementById('export-configs');
        configFileInput = document.getElementById('config-file-input');
        
        // Password change elements
        changePasswordAlert = document.getElementById('change-password-alert');
        currentPasswordInput = document.getElementById('current-password');
        newPasswordInput = document.getElementById('new-password');
        confirmPasswordInput = document.getElementById('confirm-password');
        changePasswordBtn = document.getElementById('change-password-btn');
        
        console.log('Elements initialized. Login button:', loginButton);
        console.log('Password input:', passwordInput);
        
        // Attach event listeners
        if (loginButton) {
            loginButton.addEventListener('click', (e) => {
                e.preventDefault();
                console.log('Login button clicked via addEventListener');
                login();
            });
        }
        
        if (passwordInput) {
            passwordInput.addEventListener('keyup', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    console.log('Enter key pressed in password field');
                    login();
                }
            });
            passwordInput.focus();
        }
        
        // Guest login button
        const guestBtn = document.getElementById('guest-login-btn');
        if (guestBtn) {
            guestBtn.addEventListener('click', (e) => {
                e.preventDefault();
                console.log('Guest login button clicked');
                guestLogin();
            });
        }
        
        // Note: Whitelist buttons now use inline onclick handlers
        // Do NOT add addEventListener here as it conflicts with onclick
        
        // Run initial checks
        checkAuth();
    });

    // Other element references (not in DOMContentLoaded because they're not used in login flow)
    const nodeName = document.getElementById('node-name');
    const nodeIp = document.getElementById('node-ip');
    const nodePort = document.getElementById('node-port');
    const nodeIperf = document.getElementById('node-iperf-port');
    const nodeDesc = document.getElementById('node-desc');
    const nodesList = document.getElementById('nodes-list');
    const streamingProgress = document.getElementById('streaming-progress');
    const streamingProgressBar = document.getElementById('streaming-progress-bar');
    const streamingProgressLabel = document.getElementById('streaming-progress-label');
    const testsList = document.getElementById('tests-list');
    const saveNodeBtn = document.getElementById('save-node');
    const srcSelect = document.getElementById('src-select');
    const dstSelect = document.getElementById('dst-select');
    const addNodeAlert = document.getElementById('add-node-alert');
    const testAlert = document.getElementById('test-alert');
    const deleteAllTestsBtn = document.getElementById('delete-all-tests');
    const testPortInput = document.getElementById('test-port');
    const testProgress = document.getElementById('test-progress');
    const testProgressBar = document.getElementById('test-progress-bar');
    const testProgressLabel = document.getElementById('test-progress-label');
    const reverseToggle = document.getElementById('reverse');
    const omitInput = document.getElementById('omit');
    const protocolSelect = document.getElementById('protocol');
    const tcpBandwidthInput = document.getElementById('tcp-bandwidth');
    const udpBandwidthInput = document.getElementById('udp-bandwidth');
    const udpLenInput = document.getElementById('udp-len');
    const tcpOptions = document.getElementById('tcp-options');
    const udpOptions = document.getElementById('udp-options');
    const singleTestPanel = document.getElementById('single-test-panel');
    const suiteTestPanel = document.getElementById('suite-test-panel');
    const singleTestTab = document.getElementById('single-test-tab');
    const suiteTestTab = document.getElementById('suite-test-tab');
    const testPanelIntro = document.getElementById('test-panel-intro');
    const suiteSrcSelect = document.getElementById('suite-src-select');
    const suiteDstSelect = document.getElementById('suite-dst-select');
    const suiteDuration = document.getElementById('suite-duration');
    const suiteParallel = document.getElementById('suite-parallel');
    const suitePort = document.getElementById('suite-port');
    const suiteOmit = document.getElementById('suite-omit');
    const suiteTcpBandwidth = document.getElementById('suite-tcp-bandwidth');
    const suiteUdpBandwidth = document.getElementById('suite-udp-bandwidth');
    const suiteUdpLen = document.getElementById('suite-udp-len');
    const addNodeModal = document.getElementById('add-node-modal');
    const addNodeTitle = document.getElementById('add-node-title');
    const closeAddNodeBtn = document.getElementById('close-add-node');
    const cancelAddNodeBtn = document.getElementById('cancel-add-node');
    const openAddNodeBtn = document.getElementById('open-add-node');
    const DEFAULT_IPERF_PORT = 62001;
    let nodeCache = [];
    let editingNodeId = null;
    const ipPrivacyState = {};
    let nodeRefreshInterval = null;
    let isRefreshingNodes = false;
    const streamingServices = [
      { key: 'youtube', label: 'YouTube', color: 'text-rose-300', bg: 'border-rose-500/30 bg-rose-500/10', indicator: 'bg-rose-400' },
      { key: 'netflix', label: 'Netflix', color: 'text-red-400', bg: 'border-red-500/40 bg-red-500/10', indicator: 'bg-red-400' },
      { key: 'disney_plus', label: 'Disney+', color: 'text-sky-300', bg: 'border-sky-500/40 bg-sky-500/10', indicator: 'bg-sky-400' },
      { key: 'tiktok', label: 'TikTok', color: 'text-pink-300', bg: 'border-pink-500/40 bg-pink-500/10', indicator: 'bg-pink-400' },
      { key: 'openai', label: 'ChatGPT', color: 'text-emerald-300', bg: 'border-emerald-500/40 bg-emerald-500/10', indicator: 'bg-emerald-400' },
      { key: 'gemini', label: 'Gemini', color: 'text-sky-200', bg: 'border-sky-400/40 bg-sky-400/10', indicator: 'bg-sky-300' },
      { key: 'sora', label: 'Sora', color: 'text-violet-300', bg: 'border-violet-500/40 bg-violet-500/10', indicator: 'bg-violet-400' },
      { key: 'claude', label: 'Claude', color: 'text-orange-300', bg: 'border-orange-500/40 bg-orange-500/10', indicator: 'bg-orange-400' },
      { key: 'copilot', label: 'Copilot', color: 'text-blue-300', bg: 'border-blue-500/40 bg-blue-500/10', indicator: 'bg-blue-400' },
    ];
    let streamingStatusCache = {};
    let isStreamingTestRunning = false;
    const styles = {
      rowCard: 'group relative overflow-hidden rounded-2xl border border-slate-800/80 bg-gradient-to-br from-slate-900/80 via-slate-900/60 to-slate-950/80 p-5 shadow-[0_25px_80px_rgba(0,0,0,0.5)] ring-1 ring-white/5 transition hover:border-sky-500/40 hover:shadow-sky-500/10',
      inline: 'flex flex-wrap items-center gap-3',
      badgeOnline: 'inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-emerald-500/20 to-teal-400/15 px-3 py-1 text-xs font-semibold text-emerald-100 ring-1 ring-emerald-400/40 shadow-[0_10px_30px_rgba(16,185,129,0.15)]',
      badgeOffline: 'inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-rose-500/15 to-orange-400/10 px-3 py-1 text-xs font-semibold text-rose-100 ring-1 ring-rose-400/35 shadow-[0_10px_30px_rgba(244,63,94,0.12)]',
      pillInfo: 'inline-flex items-center justify-center gap-2 rounded-lg bg-gradient-to-r from-sky-500/20 via-sky-500/15 to-indigo-500/20 px-3 py-2 text-xs font-semibold text-sky-50 ring-1 ring-sky-400/40 shadow-[0_12px_30px_rgba(14,165,233,0.18)] transition hover:scale-[1.01] hover:ring-sky-300/60',
      pillDanger: 'inline-flex items-center justify-center gap-2 rounded-lg bg-gradient-to-r from-rose-500/20 via-rose-500/15 to-orange-500/20 px-3 py-2 text-xs font-semibold text-rose-50 ring-1 ring-rose-400/40 shadow-[0_12px_30px_rgba(244,63,94,0.18)] transition hover:scale-[1.01] hover:ring-rose-300/60',
      pillWarn: 'inline-flex items-center justify-center gap-2 rounded-lg bg-gradient-to-r from-amber-500/20 via-amber-500/15 to-yellow-500/20 px-3 py-2 text-xs font-semibold text-amber-50 ring-1 ring-amber-400/40 shadow-[0_12px_30px_rgba(251,191,36,0.18)] transition hover:scale-[1.01] hover:ring-amber-300/60',
      pillMuted: 'inline-flex items-center justify-center gap-2 rounded-lg bg-slate-800/70 px-3 py-2 text-xs font-semibold text-slate-200 ring-1 ring-slate-700/70 shadow-inner shadow-black/20',
      iconButton: 'inline-flex items-center justify-center gap-1 rounded-full border border-slate-700/70 bg-slate-800/80 px-2 py-1 text-xs font-semibold text-slate-200 transition hover:border-sky-400 hover:text-sky-100 hover:shadow-[0_10px_30px_rgba(14,165,233,0.15)]',
      textMuted: 'text-slate-300/90 text-sm drop-shadow-sm',
      textMutedSm: 'text-slate-500 text-xs',
      table: 'w-full border-collapse overflow-hidden rounded-xl border border-slate-800/60 bg-slate-900/50 text-sm text-slate-100',
      tableHeader: 'bg-slate-900/70 text-slate-300',
      tableCell: 'border-b border-slate-800 px-3 py-2',
      codeBlock: 'overflow-auto rounded-lg border border-slate-800 bg-slate-950/80 p-3 text-xs text-slate-200 shadow-inner shadow-black/30',
    };

    function show(el) { el.classList.remove('hidden'); }
    function hide(el) { el.classList.add('hidden'); }
    function setAlert(el, message) { el.textContent = message; show(el); }
    function clearAlert(el) { el.textContent = ''; hide(el); }

    // Sidebar Navigation Functions
    function toggleSidebar() {
      const sidebar = document.getElementById('sidebar');
      const overlay = document.getElementById('sidebar-overlay');
      if (sidebar) {
        sidebar.classList.toggle('open');
        if (overlay) overlay.classList.toggle('active');
      }
    }
    
    function closeSidebar() {
      const sidebar = document.getElementById('sidebar');
      const overlay = document.getElementById('sidebar-overlay');
      if (sidebar) {
        sidebar.classList.remove('open');
        if (overlay) overlay.classList.remove('active');
      }
    }
    
    function openSidebar() {
      const sidebar = document.getElementById('sidebar');
      const overlay = document.getElementById('sidebar-overlay');
      if (sidebar) {
        sidebar.classList.add('open');
        if (overlay) overlay.classList.add('active');
      }
    }
    
    // Highlight current page in sidebar
    function highlightCurrentNavItem() {
      const currentPath = window.location.pathname;
      const navItems = document.querySelectorAll('.sidebar .nav-item');
      navItems.forEach(item => {
        const href = item.getAttribute('href');
        if (href === currentPath || (currentPath === '/web' && href === '/web')) {
          item.classList.add('active');
        } else {
          item.classList.remove('active');
        }
      });
    }
    
    // Update sidebar user info based on auth state
    function updateSidebarUserInfo(isGuest = false) {
      const avatar = document.getElementById('sidebar-avatar');
      const username = document.getElementById('sidebar-username');
      const role = document.getElementById('sidebar-role');
      if (avatar && username && role) {
        if (isGuest) {
          avatar.textContent = '👤';
          username.textContent = '访客';
          role.textContent = '只读模式';
          avatar.style.background = 'linear-gradient(135deg, #64748b, #475569)';
        } else {
          avatar.textContent = 'A';
          username.textContent = '管理员';
          role.textContent = '已登录';
          avatar.style.background = 'linear-gradient(135deg, #10b981, #3b82f6)';
        }
      }
    }
    
    // Show sidebar navigation (called after login)
    function showSidebarNavigation() {
      const sidebar = document.getElementById('sidebar');
      const menuBtn = document.getElementById('mobile-menu-btn');
      if (sidebar) sidebar.classList.remove('hidden');
      if (menuBtn) menuBtn.classList.remove('hidden');
    }
    
    // Hide sidebar navigation (called when logging out or showing login)
    function hideSidebarNavigation() {
      const sidebar = document.getElementById('sidebar');
      const menuBtn = document.getElementById('mobile-menu-btn');
      const overlay = document.getElementById('sidebar-overlay');
      if (sidebar) sidebar.classList.add('hidden');
      if (menuBtn) menuBtn.classList.add('hidden');
      if (overlay) overlay.classList.add('hidden');
    }
    
    // Initialize sidebar on load
    document.addEventListener('DOMContentLoaded', () => {
      highlightCurrentNavItem();
      const isGuest = document.cookie.includes('guest_session=readonly');
      updateSidebarUserInfo(isGuest);
    });

    function setLoginState(state, message) {
      if (!loginStatus) return;

      const presets = {
        idle: {
          text: '等待解锁',
          dot: 'warning',
          className: 'warning',
          hint: '输入共享密码以进入运维面板。',
        },
        unlocking: {
          text: '正在解锁...',
          dot: 'info',
          className: 'info',
          hint: '正在验证密码，请稍候。',
        },
        unlocked: {
          text: '已解锁',
          dot: 'success',
          className: 'success',
          hint: '已通过认证，可管理节点与测速任务。',
        },
        error: {
          text: '验证失败',
          dot: 'danger',
          className: 'danger',
          hint: '验证未通过，请重新输入。',
        },
      };

      const next = presets[state] || presets.idle;
      loginStatus.className = `status-chip ${next.className}`;
      if (loginStatusDot) {
        loginStatusDot.className = `status-dot ${next.dot}`;
      }
      if (loginStatusLabel) {
        loginStatusLabel.textContent = message || next.text;
      }
      if (loginHint) {
        loginHint.textContent = message || next.hint;
      }
    }

    function setLoginButtonLoading(isLoading) {
      if (!loginButton) return;
      loginButton.disabled = isLoading;
      loginButton.classList.toggle('opacity-70', isLoading);
      loginButton.classList.toggle('cursor-not-allowed', isLoading);
      
      if (isLoading) {
        loginButton.innerHTML = '<span class="inline-flex items-center justify-center gap-2">Logging in...</span>';
      } else {
        loginButton.textContent = 'Login';
      }
    }


    function toggleProtocolOptions() {
      const proto = (protocolSelect?.value || 'tcp').toLowerCase();
      if (proto === 'udp') {
        udpOptions?.classList.remove('hidden');
        tcpOptions?.classList.add('hidden');
      } else {
        tcpOptions?.classList.remove('hidden');
        udpOptions?.classList.add('hidden');
      }
    }

    function setActiveTestTab(mode) {
      const isSuite = mode === 'suite';
      if (singleTestPanel) singleTestPanel.classList.toggle('hidden', isSuite);
      if (suiteTestPanel) suiteTestPanel.classList.toggle('hidden', !isSuite);
      if (testProgress) testProgress.classList.add('hidden');

      if (singleTestTab) {
        singleTestTab.className = isSuite
          ? 'rounded-full px-4 py-1.5 text-xs font-semibold text-slate-300 transition hover:text-white'
          : 'rounded-full bg-gradient-to-r from-sky-500/80 to-indigo-500/80 px-4 py-1.5 text-xs font-semibold text-slate-50 shadow-lg shadow-sky-500/15 ring-1 ring-sky-400/40 transition hover:brightness-110';
      }
      if (suiteTestTab) {
        suiteTestTab.className = isSuite
          ? 'rounded-full bg-gradient-to-r from-emerald-500/80 to-cyan-500/80 px-4 py-1.5 text-xs font-semibold text-slate-50 shadow-lg shadow-emerald-500/15 ring-1 ring-emerald-400/40 transition hover:brightness-110'
          : 'rounded-full px-4 py-1.5 text-xs font-semibold text-slate-300 transition hover:text-white';
      }

      if (testPanelIntro) {
        testPanelIntro.textContent = isSuite
          ? '双向 TCP/UDP 测试一次完成四轮去回，方便基线和互联质量核验。'
          : '快速发起单条 TCP/UDP 链路测试，支持限速、并行与反向 (-R)。';
      }
    }

    function toggleAddNodeModal(isOpen) {
      if (!addNodeModal) return;
      if (isOpen) {
        addNodeModal.classList.remove('hidden');
        addNodeModal.classList.add('flex');
      } else {
        addNodeModal.classList.add('hidden');
        addNodeModal.classList.remove('flex');
      }
    }

    function openAddNodeModal() {
      toggleAddNodeModal(true);
      addNodeTitle.textContent = editingNodeId ? '编辑节点' : '添加节点';
      nodeName?.focus({ preventScroll: true });
    }

    function closeAddNodeModal() {
      toggleAddNodeModal(false);
    }



    function startProgressBar(container, bar, label, expectedMs, initialText, showCountdown = true) {
      const start = Date.now();
      const target = Math.max(expectedMs || 0, 1200);
      if (initialText) label.textContent = initialText;
      show(container);
      bar.style.width = '6%';
      const timer = setInterval(() => {
        const elapsed = Date.now() - start;
        const pct = Math.min(96, Math.max(10, (elapsed / target) * 100));
        bar.style.width = `${pct}%`;
        const remain = Math.max(target - elapsed, 0);
        if (remain > 0 && showCountdown) {
          label.textContent = `预计 ${Math.ceil(remain / 1000)}s 完成`;
        }
      }, 250);

      return (finalText) => {
        clearInterval(timer);
        bar.style.width = '100%';
        if (finalText) label.textContent = finalText;
        setTimeout(() => hide(container), 1200);
      };
    }

    function normalizeServiceKey(key, label) {
      return (key || label || 'unknown').toLowerCase().replace(/[^a-z0-9+]+/g, '_');
    }

    function cacheStreamingFromNode(node) {
      if (!node?.streaming?.length) return;

      const byService = {};
      node.streaming.forEach((svc) => {
        const normalized = normalizeServiceKey(svc.key, svc.service);
        byService[normalized] = {
          unlocked: svc.unlocked,
          detail: svc.detail,
          tier: svc.tier,
          service: svc.service,
          region: svc.region,
        };
      });
      streamingStatusCache[node.id] = byService;
    }

    const flagCache = {};
    const FLAG_CACHE_TTL = 1000 * 60 * 60 * 6;

    function extractCountryCode(text) {
      const match = (text || '').match(/\b([A-Za-z]{2})\b/);
      return match ? match[1].toUpperCase() : null;
    }

    function countryCodeToFlag(code) {
      if (!code || code.length !== 2) return null;
      const base = 127397;
      const chars = code.toUpperCase().split('');
      return String.fromCodePoint(...chars.map((c) => c.codePointAt(0) + base));
    }

    function isPrivateIp(ip) {
      if (!ip) return false;
      return (
        /^10\./.test(ip) ||
        /^192\.168\./.test(ip) ||
        /^172\.(1[6-9]|2\d|3[0-1])\./.test(ip) ||
        ip === '127.0.0.1'
      );
    }

    function resolveLocalFlag(node) {
      const codeFromDescription = extractCountryCode(node.description);
      const codeFromName = extractCountryCode(node.name);
      const code = codeFromDescription || codeFromName || null;
      const flag = countryCodeToFlag(code) || '🌐';
      return { flag, code };
    }

    function renderFlagHtml(info) {
      const flag = (info?.flag || '🌐').replace(/'/g, '');
      const code = info?.code;
      if (code) {
        // Use local proxy with server-side caching instead of direct flagcdn access
        const url = `/flags/${code.toLowerCase()}`;
        return `<img src=\"${url}\" alt=\"${code} flag\" class=\"h-4 w-6 rounded-sm border border-white/10 bg-slate-800/50 shadow-sm\" loading=\"lazy\" onerror=\"this.replaceWith(document.createTextNode('${flag}'))\">`;
      }
      return flag;
    }

    function renderFlagSlot(nodeId, info, extraClass = '', title = '') {
      const classAttr = extraClass ? ` ${extraClass}` : '';
      const titleAttr = title ? ` title=\"${title}\"` : '';
      const codeAttr = info?.code ? ` data-flag-code=\"${info.code}\"` : '';
      return `<span class=\"inline-flex items-center${classAttr}\" data-node-flag=\"${nodeId}\"${codeAttr}${titleAttr}>${renderFlagHtml(info)}</span>`;
    }

    function updateFlagElement(el, info) {
      if (!el || !info) return;
      if (info.code) {
        el.dataset.flagCode = info.code;
      }
      el.innerHTML = renderFlagHtml(info);
    }

    async function getNodeFlag(node) {
      const now = Date.now();
      const cacheKey = node.ip || node.id;
      const cached = flagCache[cacheKey];
      if (cached && now - cached.timestamp < FLAG_CACHE_TTL) {
        return cached;
      }

      const fallback = resolveLocalFlag(node);
      if (!node.ip || isPrivateIp(node.ip)) {
        flagCache[cacheKey] = { ...fallback, timestamp: now };
        return flagCache[cacheKey];
      }

      try {
        const res = await apiFetch(`/geo?ip=${encodeURIComponent(node.ip)}`);
        if (!res.ok) {
          throw new Error('geo lookup failed');
        }
        const data = await res.json();
        const code = (data?.country_code || '').toUpperCase() || fallback.code;
        const flag = countryCodeToFlag(code) || fallback.flag;
        flagCache[cacheKey] = { flag, code, timestamp: now };
        return flagCache[cacheKey];
      } catch (error) {
        console.warn('无法获取 IP 归属地国旗，将使用回退旗帜。', error);
        return fallback;
      }
    }

    function attachFlagUpdater(node, elements) {
      if (!elements) return;
      const targets = elements instanceof NodeList ? Array.from(elements) : [elements];
      if (!targets.length) return;

      const fallback = resolveLocalFlag(node);
      targets.forEach((el) => updateFlagElement(el, fallback));

      getNodeFlag(node).then((info) => {
        if (!info) return;
        targets.forEach((el) => updateFlagElement(el, info));
      });
    }

    function maskIp(ip, shouldMask) {
      // Always mask for guests
      const isGuest = window.isGuest === true;
      if ((!shouldMask && !isGuest) || !ip) return ip;
      if (ip.includes(':')) {
        const segments = ip.split(':');
        const kept = segments.slice(0, 2).join(':');
        return `${kept}:****:****`;
      }
      const parts = ip.split('.');
      if (parts.length >= 4) {
        return `${parts[0]}.***.***.***`;
      }
      return `${parts[0] || '***'}.***.***.***`;
    }

    function maskPort(port, shouldMask) {
      if (!port) return port;
      return shouldMask ? '****' : `${port}`;
    }

    function renderStreamingBadges(nodeId) {
      const cache = streamingStatusCache[nodeId];
      if (isStreamingTestRunning && (!cache || cache.inProgress)) {
        return '<span class="text-xs text-emerald-300">流媒体测试中...</span>';
      }
      if (!cache) {
        return '<span class="text-xs text-slate-500">未检测</span>';
      }

      if (cache.error) {
        return `<span class=\"text-xs text-amber-300\">${cache.message || '检测异常'}</span>`;
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
          
          // Determine badge style class
          let badgeClass = 'streaming-failed';
          let statusLabel = '未检测';
          
          if (unlocked === true) {
            // Check if it's DNS/proxy unlock based on detail
            const isDnsUnlock = detail && (detail.toLowerCase().includes('dns') || detail.toLowerCase().includes('proxy'));
            badgeClass = isDnsUnlock ? 'streaming-dns' : 'streaming-native';
            statusLabel = '可解锁';
          } else if (unlocked === false) {
            badgeClass = 'streaming-failed';
            statusLabel = '未解锁';
          }
          
          // Special handling for Netflix tiers
          if (svc.key === 'netflix' && status) {
            const netflixTier = tier || (unlocked ? 'full' : 'none');
            if (netflixTier === 'full') {
              statusLabel = '全解锁';
              badgeClass = 'streaming-native';
            } else if (netflixTier === 'originals') {
              statusLabel = '自制剧';
              badgeClass = 'streaming-dns';
            }
          }
          
          // Region as corner badge
          const regionBadge = region ? `<span class=\"region-corner\">${region}</span>` : '';
          
          const title = `${region ? `[${region}] ` : ''}${svc.label}：${statusLabel}${detail ? ' · ' + detail : ''}`;
          return `<span class=\"streaming-badge ${badgeClass}\" title=\"${title}\">${regionBadge}${svc.label}</span>`;
        })
        .join('');
      
      return `<div class=\"streaming-grid\">${badges}</div>`;
    }


    async function exportAgentConfigs() {
      clearAlert(configAlert);
      const res = await apiFetch('/agent-configs/export');
      if (!res.ok) {
        setAlert(configAlert, '导出配置失败。');
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
        setAlert(configAlert, 'JSON 文件无效。');
        return;
      }

      const res = await apiFetch('/agent-configs/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        const msg = await res.text();
        setAlert(configAlert, msg || '导入配置失败。');
        return;
      }

      const imported = await res.json();
      setAlert(configAlert, `已导入 ${imported.length} 条代理配置。`);
    }

    function resetNodeForm() {
      nodeName.value = '';
      nodeIp.value = '';
      nodePort.value = 8000;
      nodeIperf.value = DEFAULT_IPERF_PORT;
      nodeDesc.value = '';
      editingNodeId = null;
      saveNodeBtn.textContent = '保存节点';
      addNodeTitle.textContent = '添加节点';
      hide(addNodeAlert);
    }

    async function removeNode(nodeId) {
      clearAlert(addNodeAlert);
      const confirmDelete = confirm('确定删除该节点并清理相关测试记录吗？');
      if (!confirmDelete) return;

      const res = await apiFetch(`/nodes/${nodeId}`, { method: 'DELETE' });
      if (!res.ok) {
        setAlert(addNodeAlert, '删除节点失败。');
        return;
      }

      if (editingNodeId === nodeId) {
        resetNodeForm();
        closeAddNodeModal();
      }

      await refreshNodes();
      await refreshTests();
    }

    async function checkAuth(showFeedback = false) {
      try {
        const res = await apiFetch('/auth/status');
        if (!res.ok) {
          let message = '无法验证登录状态。';
          try {
            const data = await res.json();
            if (data?.detail) message = `认证失败：${data.detail}`;
          } catch (_) {
            try {
              const rawText = await res.text();
              if (rawText) message = `认证失败：${rawText}`;
            } catch (_) {}
          }

          setLoginState('error', message);
          if (showFeedback) setAlert(loginAlert, message);
          return false;
        }

        const data = await res.json();
        const isGuest = data.isGuest === true;
        window.isGuest = isGuest;
        
        if (data.authenticated || isGuest) {
          loginCard?.classList.add('hidden');
          appCard?.classList.remove('hidden');
          showSidebarNavigation();  // Show sidebar after login
          setLoginState('unlocked');
          
          if (isGuest) {
            if (authHint) {
              authHint.textContent = '👁️ 访客模式 - 仅可查看，无法操作';
              authHint.className = 'text-sm text-amber-400';
            }
            // Hide action buttons for guests
            document.querySelectorAll('.guest-hide').forEach(el => el.classList.add('hidden'));
            document.getElementById('logout-btn')?.classList.remove('hidden');
          } else {
            if (authHint) {
              authHint.textContent = '已通过认证，可管理节点与测速任务。';
              authHint.className = 'text-sm text-slate-400';
            }
            document.querySelectorAll('.guest-hide').forEach(el => el.classList.remove('hidden'));
          }
          
          await refreshNodes();
          await refreshTests();
          return true;
        } else {
          appCard?.classList.add('hidden');
          loginCard?.classList.remove('hidden');
          hideSidebarNavigation();  // Hide sidebar when not authenticated
          setLoginState('idle');
          if (showFeedback) setAlert(loginAlert, '登录状态未建立，请重新登录。');
          return false;
        }
      } catch (err) {
        console.error('Auth check failed:', err);
        appCard?.classList.add('hidden');
        loginCard?.classList.remove('hidden');
        hideSidebarNavigation();  // Hide sidebar on error
        const errorMessage = '无法连接认证服务，请稍后重试。';
        setLoginState('error', errorMessage);
        if (showFeedback) setAlert(loginAlert, errorMessage);
        return false;
      }
    }

    async function login() {
      console.log('Starting login process...');
      clearAlert(loginAlert);
      
      // Reset animations
      const card = document.querySelector('.login-card');
      card.classList.remove('animate-shake', 'animate-success');
      
      const password = (passwordInput?.value || '').trim();
      if (!password) {
        console.warn('Login aborted: empty password');
        setAlert(loginAlert, '请输入密码 (Password Required)');
        passwordInput?.focus();
        card.classList.add('animate-shake');
        setTimeout(() => card.classList.remove('animate-shake'), 400);
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

        console.log(`Login response status: ${res.status}`);

        if (res.ok) {
           console.log('Login success. Verifying session...');
           loginAlert.className = 'alert alert-success';
           setAlert(loginAlert, '登录成功 (Success)');
           card.classList.add('animate-success');
           
           // Hide login card immediately to prevent flash
           loginCard.style.opacity = '0.5';
           loginCard.style.pointerEvents = 'none';
           
           // Allow more time for the cookie to be processed/saved by the browser
           setTimeout(async () => {
             const authed = await checkAuth(true);
             console.log(`Session check result: ${authed}`);
             if (!authed) {
                console.error('Login successful but session check failed.');
                loginAlert.className = 'alert alert-error';
                setAlert(loginAlert, '会话建立失败 (Session Failed) - Cookie Blocked?');
                card.classList.remove('animate-success');
                card.classList.add('animate-shake');
                loginCard.style.opacity = '1';
                loginCard.style.pointerEvents = 'auto';
             }
           }, 1200);
           return;
        }

        // Handle HTTP errors
        card.classList.add('animate-shake');
        setTimeout(() => card.classList.remove('animate-shake'), 400);
        
        loginAlert.className = 'alert alert-error';
        let message = '登录失败 (Login Failed)';
        
        if (res.status === 401) {
            console.warn('Login failed: 401 Unauthorized');
            message = '登录失败：密码错误 (Invalid Password)';
        } else if (res.status === 408 || res.status === 504) {
             console.error('Login failed: Timeout');
             message = '登录超时 (Request Timeout)';
        } else {
            try {
                const data = await res.json();
                console.warn('Login failed with details:', data);
                if (data?.detail === 'empty_password') message = '密码不能为空';
                else if (data?.detail === 'invalid_password') message = '登录失败：密码错误 (Invalid Password)';
                else if (data?.detail) message = `登录失败：${data.detail}`;
            } catch (e) {
                console.error('Failed to parse error response:', e);
                message = `登录失败 (HTTP ${res.status})`;
            }
        }
        setAlert(loginAlert, message);

      } catch (err) {
        clearTimeout(timeoutId);
        console.error('Login network exception:', err);
        
        card.classList.add('animate-shake');
        setTimeout(() => card.classList.remove('animate-shake'), 400);
        
        loginAlert.className = 'alert alert-error';
        const errorMsg = err.name === 'AbortError' ? '请求超时 (Timeout)' : '无法连接服务器 (Network Error)';
        setAlert(loginAlert, errorMsg);
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
        setAlert(changePasswordAlert, '请输入新密码。');
        return;
      }

      if (payload.new_password.length < 6) {
        setAlert(changePasswordAlert, '新密码长度需不少于 6 位。');
        return;
      }

      if (payload.new_password !== payload.confirm_password) {
        setAlert(changePasswordAlert, '两次输入的新密码不一致。');
        return;
      }

      const res = await apiFetch('/auth/change', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        let feedback = '更新密码失败。';
        try {
          const data = await res.json();
          if (data?.detail === 'invalid_password') feedback = '当前密码不正确或会话已过期。';
          if (data?.detail === 'password_too_short') feedback = '新密码长度不足 6 位。';
          if (data?.detail === 'password_mismatch') feedback = '两次输入的新密码不一致。';
          if (data?.detail === 'empty_password') feedback = '请输入新密码。';
        } catch (err) {
          feedback = feedback + ' ' + (err?.message || '');
        }
        setAlert(changePasswordAlert, feedback.trim());
        return;
      }

      changePasswordAlert.className = 'alert alert-success';
      setAlert(changePasswordAlert, '✅ 密码已成功更新!当前会话已使用新密码。');
      currentPasswordInput.value = '';
      newPasswordInput.value = '';
      confirmPasswordInput.value = '';
    }

    function syncTestPort() {
      const dst = nodeCache.find((n) => n.id === Number(dstSelect.value));
      if (dst) {
        const detected = dst.detected_iperf_port || dst.iperf_port;
        testPortInput.value = detected || DEFAULT_IPERF_PORT;
      }
    }

    // syncWhitelist is defined in the settings modal JavaScript section

    function syncSuitePort() {
      const dst = nodeCache.find((n) => n.id === Number(suiteDstSelect?.value));
      if (dst && suitePort) {
        const detected = dst.detected_iperf_port || dst.iperf_port;
        suitePort.value = detected || DEFAULT_IPERF_PORT;
      }
    }


    function maskIp(ip, hidden) {
      if (!hidden || !ip) return ip;
      
      // Check if it's a domain name (contains non-numeric parts)
      const isIp = /^[\d.:]+$/.test(ip);
      
      if (isIp) {
        // Mask last two segments of IPv4: 1.2.3.4 -> 1.2.*.*
        const parts = ip.split('.');
        if (parts.length === 4) {
            return `${parts[0]}.${parts[1]}.*.*`;
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

    async function refreshNodes() {
      if (isRefreshingNodes) return;
      isRefreshingNodes = true;
      try {
        const previousSrc = Number(srcSelect.value) || null;
        const previousDst = Number(dstSelect.value) || null;
        const previousSuiteSrc = Number(suiteSrcSelect?.value) || null;
        const previousSuiteDst = Number(suiteDstSelect?.value) || null;
        const res = await apiFetch('/nodes/status');
        const nodes = await res.json();
        nodeCache = nodes;
        nodesList.innerHTML = '';
        srcSelect.innerHTML = '';
        dstSelect.innerHTML = '';
        if (suiteSrcSelect) suiteSrcSelect.innerHTML = '';
        if (suiteDstSelect) suiteDstSelect.innerHTML = '';

        if (!nodes.length) {
          nodesList.textContent = '暂无节点。';
          return;
        }

        nodes.forEach((node) => {
          cacheStreamingFromNode(node);

          const privacyEnabled = !!ipPrivacyState[node.id];
        const flagInfo = resolveLocalFlag(node);
        const locationBadge = renderFlagSlot(node.id, flagInfo, 'text-base drop-shadow-sm', '服务器所在地区');
        const statusBadge = node.status === 'online'
          ? `<span class="${styles.badgeOnline}"><span class=\"h-2 w-2 rounded-full bg-emerald-400\"></span><span>在线</span></span>`
          : `<span class="${styles.badgeOffline}"><span class=\"h-2 w-2 rounded-full bg-rose-400\"></span><span>离线</span></span>`;
          
          // Whitelist Sync Badge
          let syncBadge = '';
          const syncTime = node.whitelist_sync_at ? new Date(node.whitelist_sync_at).toLocaleString() : '未知';
          const errorMsg = node.whitelist_sync_message || '未知错误';
          
          if (node.whitelist_sync_status === 'synced') {
              syncBadge = `<span class="inline-flex items-center rounded-md bg-emerald-500/10 px-2 py-0.5 text-xs font-medium text-emerald-400 ring-1 ring-inset ring-emerald-500/20 cursor-help" title="白名单已同步 (${syncTime})">🔄 白名单</span>`;
          } else if (node.whitelist_sync_status === 'not_synced') {
              syncBadge = `<span class="inline-flex items-center rounded-md bg-yellow-500/10 px-2 py-0.5 text-xs font-medium text-yellow-400 ring-1 ring-inset ring-yellow-500/20 cursor-help" title="白名单内容不一致 (${syncTime})">⚠️ 白名单</span>`;
          } else if (node.whitelist_sync_status === 'failed') {
             syncBadge = `<span class="inline-flex items-center rounded-md bg-rose-500/10 px-2 py-0.5 text-xs font-medium text-rose-400 ring-1 ring-inset ring-rose-500/20 cursor-help" title="同步失败: ${errorMsg} (${syncTime})">❌ 白名单</span>`;
          } else {
             syncBadge = `<span class="inline-flex items-center rounded-md bg-slate-500/10 px-2 py-0.5 text-xs font-medium text-slate-400 ring-1 ring-inset ring-slate-500/20" title="白名单同步状态未知">❓ 白名单</span>`;
          }
          
          // Version Mismatch Badge - only show when agent reports a different version
          const expectedVersion = '1.4.0';
          let versionBadge = '';
          if (node.agent_version && node.agent_version !== expectedVersion) {
              versionBadge = `<span class="inline-flex items-center rounded-md bg-amber-500/10 px-2 py-0.5 text-xs font-medium text-amber-400 ring-1 ring-inset ring-amber-500/20 cursor-help" title="Agent版本 ${node.agent_version} 与预期版本 ${expectedVersion} 不一致，请更新">⬆️ 需更新</span>`;
          }
          // Note: If agent_version is null/missing, we don't show the badge to avoid false positives
          
          // Internal Agent Badge - for NAT/reverse connection agents
          let internalBadge = '';
          if (node.agent_mode === 'reverse') {
              internalBadge = `<span class="inline-flex items-center rounded-md bg-violet-500/10 px-2 py-0.5 text-xs font-medium text-violet-400 ring-1 ring-inset ring-violet-500/20 cursor-help" title="内网穿透模式 (反向注册模式)">🔗 内网穿透</span>`;
          }
          
          // Auto-Update Status Badge
          let updateBadge = '';
          if (node.update_status === 'updated') {
              const updateTime = node.update_at ? new Date(node.update_at).toLocaleString('zh-CN') : '';
              updateBadge = `<span class="inline-flex items-center rounded-md bg-emerald-500/10 px-2 py-0.5 text-xs font-medium text-emerald-400 ring-1 ring-inset ring-emerald-500/20 cursor-help" title="${node.update_message || '自动更新成功'} (${updateTime})">✅ 自动更新</span>`;
          } else if (node.update_status === 'pending') {
              updateBadge = `<span class="inline-flex items-center rounded-md bg-yellow-500/10 px-2 py-0.5 text-xs font-medium text-yellow-400 ring-1 ring-inset ring-yellow-500/20 cursor-help" title="${node.update_message || '更新中...'}">⏳ 更新中</span>`;
          } else if (node.update_status === 'failed') {
              updateBadge = `<span class="inline-flex items-center rounded-md bg-rose-500/10 px-2 py-0.5 text-xs font-medium text-rose-400 ring-1 ring-inset ring-rose-500/20 cursor-help" title="${node.update_message || '自动更新失败'}">❌ 更新失败</span>`;
          }

          const ports = node.detected_iperf_port ? `${node.detected_iperf_port}` : `${node.iperf_port}`;
          const agentPort = node.detected_agent_port || node.agent_port;
          const agentPortDisplay = maskPort(agentPort, privacyEnabled || window.isGuest);
          const iperfPortDisplay = maskPort(ports, privacyEnabled || window.isGuest);
          const streamingBadges = renderStreamingBadges(node.id);
          const backboneBadges = renderBackboneBadges(node.backbone_latency, node.id);
          const ipMasked = maskIp(node.ip, privacyEnabled || window.isGuest);

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
                  <span class="text-base">${ipPrivacyState[node.id] ? '🙈' : '👁️'}</span>
                </button>` : ''}
              </div>
              ${backboneBadges ? `<div class=\"flex flex-wrap items-center gap-2\">${backboneBadges}</div>` : ''}
              <div class="flex flex-wrap items-center gap-2" data-streaming-badges="${node.id}">${streamingBadges || ''}</div>
              <p class="${styles.textMuted} flex items-center gap-2 text-xs">
                <span class="font-mono text-slate-400" data-node-ip-display="${node.id}">${ipMasked}</span>
                
                <!-- ISP Display -->
                <span class="text-slate-500 border-l border-slate-700 pl-2" id="isp-${node.id}"></span>
              </p>
            </div>
            ${!window.isGuest ? `<div class="flex flex-wrap items-center justify-start gap-2 lg:flex-col lg:items-end lg:justify-center lg:min-w-[170px] opacity-100 md:opacity-0 md:pointer-events-none md:transition md:duration-200 md:group-hover:opacity-100 md:group-hover:pointer-events-auto md:focus-within:opacity-100 md:focus-within:pointer-events-auto">
              <button class="${styles.pillInfo}" onclick="runStreamingCheck(${node.id})">流媒体解锁测试</button>
              <button class="${styles.pillInfo}" onclick="editNode(${node.id})">编辑</button>
              <button class="${styles.pillDanger}" onclick="removeNode(${node.id})">删除</button>
            </div>` : ''}
          </div>
        `;
        nodesList.appendChild(item);

        const toggleBtn = item.querySelector('[data-privacy-toggle]');
        const ipDisplay = item.querySelector(`[data-node-ip-display="${node.id}"]`);
        const flagDisplay = item.querySelectorAll(`[data-node-flag="${node.id}"]`);
        const agentPortSpan = item.querySelector(`[data-node-agent-port="${node.id}"]`);
        const iperfPortSpan = item.querySelector(`[data-node-iperf-display="${node.id}"]`);
        toggleBtn?.addEventListener('click', () => {
          const nextState = !ipPrivacyState[node.id];
          ipPrivacyState[node.id] = nextState;
          if (ipDisplay) {
            ipDisplay.textContent = maskIp(node.ip, nextState);
          }
          if (agentPortSpan) {
            const agentPort = node.detected_agent_port || node.agent_port;
            agentPortSpan.textContent = `:${maskPort(agentPort, nextState)}`;
          }
          if (iperfPortSpan) {
            iperfPortSpan.textContent = `· iperf ${maskPort(ports, nextState)}${node.description ? ' · ' + node.description : ''}`;
          }
          toggleBtn.innerHTML = `<span class="text-base">${nextState ? '🙈' : '👁️'}</span>`;
          toggleBtn.setAttribute('aria-pressed', String(nextState));
        });

        attachFlagUpdater(node, flagDisplay);

        const optionA = document.createElement('option');
        optionA.value = node.id;
        optionA.textContent = `${node.name} (${maskIp(node.ip, privacyEnabled)} | iperf ${maskPort(ports, privacyEnabled)})`;
        srcSelect.appendChild(optionA);

        const optionB = optionA.cloneNode(true);
        dstSelect.appendChild(optionB);

        if (suiteSrcSelect && suiteDstSelect) {
          const suiteOptionA = optionA.cloneNode(true);
          const suiteOptionB = optionA.cloneNode(true);
          suiteSrcSelect.appendChild(suiteOptionA);
          suiteDstSelect.appendChild(suiteOptionB);
        }
      });

      const firstNodeId = nodes[0]?.id;
      if (previousSrc && nodes.some((n) => n.id === previousSrc)) {
        srcSelect.value = String(previousSrc);
      } else if (firstNodeId) {
        srcSelect.value = String(firstNodeId);
      }

      if (previousDst && nodes.some((n) => n.id === previousDst)) {
        dstSelect.value = String(previousDst);
      } else if (firstNodeId) {
        dstSelect.value = String(firstNodeId);
      }

      if (suiteSrcSelect) {
        if (previousSuiteSrc && nodes.some((n) => n.id === previousSuiteSrc)) {
          suiteSrcSelect.value = String(previousSuiteSrc);
        } else if (firstNodeId) {
          suiteSrcSelect.value = String(firstNodeId);
        }
      }

      if (suiteDstSelect) {
        if (previousSuiteDst && nodes.some((n) => n.id === previousSuiteDst)) {
          suiteDstSelect.value = String(previousSuiteDst);
        } else if (firstNodeId) {
          suiteDstSelect.value = String(firstNodeId);
        }
      }

      // Fetch ISP info (with localStorage caching)
      const ISP_CACHE_KEY = 'isp_cache';
      const ISP_CACHE_TTL = 24 * 60 * 60 * 1000; // 24 hours in ms
      
      function getIspCache() {
        try {
          const cached = localStorage.getItem(ISP_CACHE_KEY);
          if (cached) {
            const data = JSON.parse(cached);
            // Check if cache is still valid
            if (data.expires > Date.now()) {
              return data.ips;
            }
          }
        } catch (e) {}
        return {};
      }
      
      function saveIspCache(ips) {
        try {
          localStorage.setItem(ISP_CACHE_KEY, JSON.stringify({
            ips: ips,
            expires: Date.now() + ISP_CACHE_TTL
          }));
        } catch (e) {}
      }
      
      const ispCache = getIspCache();
      
      nodes.forEach(node => {
          if (!ipPrivacyState[node.id]) {
             // Check cache first
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
                         // Save to cache
                         ispCache[node.ip] = { isp: d.isp, country_code: d.country_code };
                          saveIspCache(ispCache);
                      }
                  })
                  .catch(() => {});
             }
          }
      });
      
      // Async fetch ping trends for each node
      nodes.forEach(node => {
        updateNodePingTrends(node.id);
      });

      syncTestPort();
      syncSuitePort();
      } finally {
        isRefreshingNodes = false;
      }
    }

    async function runStreamingCheck(nodeId) {
      if (isStreamingTestRunning) return;
      const targetNode = nodeCache.find((n) => n.id === nodeId);
      if (!targetNode) {
        setAlert(addNodeAlert, '节点不存在或尚未加载。');
        return;
      }

      isStreamingTestRunning = true;
      streamingProgressLabel.textContent = '流媒体测试中...';
      const expectedMs = Math.max(3500, 2000);
      const stopProgress = startProgressBar(streamingProgress, streamingProgressBar, streamingProgressLabel, expectedMs, '准备发起检测...', false);

      try {
        streamingStatusCache[nodeId] = { inProgress: true };
        updateNodeStreamingBadges(nodeId);
        streamingProgressLabel.textContent = `${targetNode.name} 测试中`;
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
                byService[svc.key] = { unlocked: false, detail: '未检测' };
              }
            });
            streamingStatusCache[nodeId] = byService;
            updateNodeStreamingBadges(nodeId);
          }
        } catch (err) {
          streamingStatusCache[nodeId] = { error: true, message: err?.message || '请求异常' };
          updateNodeStreamingBadges(nodeId);
        }

        stopProgress('检测完成');
      } finally {
        isStreamingTestRunning = false;
      }
    }

    function editNode(nodeId) {
      const node = nodeCache.find((n) => n.id === nodeId);
      if (!node) return;
      nodeName.value = node.name;
      nodeIp.value = node.ip;
      nodePort.value = node.agent_port;
      nodeIperf.value = node.iperf_port;
      nodeDesc.value = node.description || '';
      editingNodeId = nodeId;
      saveNodeBtn.textContent = '保存修改';
      addNodeTitle.textContent = '编辑节点';
      openAddNodeModal();
    }

    async function saveNodeInline(nodeId, payload) {
      const res = await apiFetch(`/nodes/${nodeId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        alert('保存失败，请检查字段。');
        return;
      }
      await refreshNodes();
    }
    // Pagination state for tests
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
        pagination?.classList.add('hidden');
        return;
      }
      
      pagination?.classList.remove('hidden');
      if (pageInfo) pageInfo.textContent = `第 ${testsCurrentPage} 页 / 共 ${totalPages} 页`;
      if (prevBtn) prevBtn.disabled = testsCurrentPage <= 1;
      if (nextBtn) nextBtn.disabled = testsCurrentPage >= totalPages;
    }
    
    function renderTestsPage() {
      const pageSize = getTestsPageSize();
      const start = (testsCurrentPage - 1) * pageSize;
      const pageData = testsAllData.slice(start, start + pageSize);
      
      testsList.innerHTML = '';
      if (!pageData.length) {
        testsList.textContent = '暂无测试记录。';
        document.getElementById('tests-pagination')?.classList.add('hidden');
        return;
      }
      
      renderTestCards(pageData);
      updateTestsPagination();
    }

    async function refreshTests() {
      const res = await apiFetch('/tests');
      const tests = await res.json();
      if (!tests.length) {
        testsList.textContent = '暂无测试记录。';
        document.getElementById('tests-pagination')?.classList.add('hidden');
        testsAllData = [];
        return;
      }
      testsList.innerHTML = '';

      const detailBlocks = new Map();
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
      
      // Slice for current page
      const pageSize = getTestsPageSize();
      const start = (testsCurrentPage - 1) * pageSize;
      const enrichedTests = allEnrichedTests.slice(start, start + pageSize);
      
      // Update pagination controls
      updateTestsPagination();

      const maxRate = Math.max(
        1,
        ...allEnrichedTests
          .filter((item) => !item.metrics?.isSuite)
          .map(({ rateSummary }) => Math.max(rateSummary.receiverRateValue || 0, rateSummary.senderRateValue || 0))
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
        const pathLabel = `${formatNodeLabel(test.src_node_id)} → ${formatNodeLabel(test.dst_node_id)}`;

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
            const latencyValue = entry.metrics?.latencyStats?.avg ?? entry.metrics?.latencyMs;
            if (latencyValue !== undefined && latencyValue !== null) {
              badgeRow.appendChild(createMiniStat('RTT', formatMetric(latencyValue, 2), 'ms', 'text-sky-200', entry.metrics?.latencyStats));
            }
            const jitterValue = entry.metrics?.jitterStats?.avg ?? entry.metrics?.jitterMs;
            if (jitterValue !== undefined && jitterValue !== null) {
              badgeRow.appendChild(createMiniStat('抖动', formatMetric(jitterValue, 2), 'ms', 'text-amber-200', entry.metrics?.jitterStats));
            }
            const lossValue = entry.metrics?.lossStats?.avg ?? entry.metrics?.lostPercent;
            if (lossValue !== undefined && lossValue !== null) {
              badgeRow.appendChild(createMiniStat('丢包', formatMetric(lossValue, 2), '%', 'text-rose-200', entry.metrics?.lossStats));
            }
            const retransValue = entry.metrics?.retransStats?.avg;
            if (retransValue !== undefined && retransValue !== null) {
              badgeRow.appendChild(createMiniStat('重传', formatMetric(retransValue, 0), '次', 'text-indigo-200', entry.metrics?.retransStats));
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
                <div class="flex items-center justify-between"><span>发送</span><span class="font-semibold text-amber-200">${entry.rateSummary.senderRateMbps}</span></div>
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
          quickStats.appendChild(createMiniStat('重传', formatMetric(retransValue, 2), '次', 'text-indigo-200', metrics.retransStats));
        }
        if (quickStats.childNodes.length) {
          card.appendChild(quickStats);
        }

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
        congestion.textContent = `拥塞：${rateSummary.senderCongestion} / ${rateSummary.receiverCongestion}`;

        actions.appendChild(buttons);
        actions.appendChild(congestion);
        card.appendChild(actions);

        const block = buildTestDetailsBlock(test, metrics, latencyValue, pathLabel);
        detailBlocks.set(test.id, block);

        testsList.appendChild(card);
        testsList.appendChild(block);
      });
    }

    async function deleteTestResult(testId) {
      clearAlert(testAlert);
      const res = await apiFetch(`/tests/${testId}`, { method: 'DELETE' });
      if (!res.ok) {
        setAlert(testAlert, '删除记录失败。');
        return;
      }
      await refreshTests();
    }

    async function clearAllTests() {
      clearAlert(testAlert);
      const res = await apiFetch('/tests', { method: 'DELETE' });
      if (!res.ok) {
        setAlert(testAlert, '清空失败。');
        return;
      }
      await refreshTests();
    }

    async function saveNode() {
      clearAlert(addNodeAlert);
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
        const msg = editingNodeId ? '更新节点失败，请检查字段。' : '保存节点失败，请检查字段。';
        setAlert(addNodeAlert, msg);
        return;
      }

      resetNodeForm();
      await refreshNodes();
      closeAddNodeModal();
      clearAlert(addNodeAlert);
    }

    async function runTest() {
      clearAlert(testAlert);
      const selectedDst = nodeCache.find((n) => n.id === Number(dstSelect.value));
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

      const finishProgress = startProgressBar(
        testProgress,
        testProgressBar,
        testProgressLabel,
        payload.duration * 1000 + 1500,
        '开始链路测试...'
      );

      const res = await apiFetch('/tests', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        const details = await res.text();
        const message = details ? `启动测试失败：${details}` : '启动测试失败，请确认节点存在且参数有效。';
        setAlert(testAlert, message);
        finishProgress('测试失败');
        return;
      }

      await refreshTests();
      finishProgress('测试完成');
      clearAlert(testAlert);
    }

    async function runSuiteTest() {
      clearAlert(testAlert);
      const selectedDst = nodeCache.find((n) => n.id === Number(suiteDstSelect.value));

      const payload = {
        src_node_id: Number(suiteSrcSelect.value),
        dst_node_id: Number(suiteDstSelect.value),
        duration: Number(suiteDuration.value || 10),
        parallel: Number(suiteParallel.value || 1),
        port: Number(suitePort.value || (selectedDst ? (selectedDst.detected_iperf_port || selectedDst.iperf_port) : DEFAULT_IPERF_PORT)),
      };

      const omitValue = Number(suiteOmit.value || 0);
      if (omitValue > 0) payload.omit = omitValue;

      const tcpBw = suiteTcpBandwidth.value.trim();
      if (tcpBw) payload.tcp_bandwidth = tcpBw;
      const udpBw = suiteUdpBandwidth.value.trim();
      if (udpBw) payload.udp_bandwidth = udpBw;
      const udpLen = Number(suiteUdpLen.value || 0);
      if (udpLen > 0) payload.udp_datagram_size = udpLen;

      const expectedMs = payload.duration * 4000 + 3000;
      const finishProgress = startProgressBar(
        testProgress,
        testProgressBar,
        testProgressLabel,
        expectedMs,
        '准备执行 4 轮双向测试...'
      );

      const res = await apiFetch('/tests/suite', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        const details = await res.text();
        const message = details ? `启动双向测试失败：${details}` : '启动双向测试失败，请确认节点存在且参数有效。';
        setAlert(testAlert, message);
        finishProgress('测试失败');
        return;
      }

      await refreshTests();
      finishProgress('双向测试完成');
      clearAlert(testAlert);
    }

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
          <div class="text-[11px] font-semibold text-slate-300">${label} 均值${unit ? ` (${unit})` : ''}</div>
          <div class="mt-1 text-sm font-bold text-white">${formatMetric(primary)}${unitLabel}</div>
          <div class="mt-1 text-[10px] text-slate-500">max ${formatMetric(stats.max)}${unitLabel} · min ${formatMetric(stats.min)}${unitLabel}</div>
        `;
        wrap.appendChild(detail);

        wrap.onmouseenter = () => {
          detail.classList.remove('opacity-0', 'scale-95');
          detail.classList.add('opacity-100', 'scale-100');
        };
        wrap.onmouseleave = () => {
          detail.classList.add('opacity-0', 'scale-95');
          detail.classList.remove('opacity-100', 'scale-100');
        };
      }

      return wrap;
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

    function summarizeSingleMetrics(raw) {
      const body = (raw && raw.iperf_result) || raw || {};
      const end = (body && body.end) || {};
      const sumReceived = end.sum_received || end.sum;
      const sumSent = end.sum_sent || end.sum;
      const firstStream = (end.streams && end.streams.length) ? end.streams[0] : null;
      const receiverStream = firstStream && firstStream.receiver ? firstStream.receiver : null;
      const senderStream = firstStream && firstStream.sender ? firstStream.sender : null;
      const pickFirst = (...values) => values.find((v) => v !== undefined && v !== null);

      const lossFromPackets = sumReceived && sumReceived.lost_packets !== undefined && sumReceived.packets
        ? (sumReceived.lost_packets / sumReceived.packets) * 100
        : undefined;

      const stats = collectMetricStats(raw);

      const bitsPerSecond = pickFirst(
        sumReceived?.bits_per_second,
        receiverStream?.bits_per_second,
        sumSent?.bits_per_second,
        senderStream?.bits_per_second,
      );

      const jitterMs = stats?.jitter?.avg ?? pickFirst(
        sumReceived?.jitter_ms,
        sumSent?.jitter_ms,
        receiverStream?.jitter_ms,
        senderStream?.jitter_ms,
      );

      const lostPercent = stats?.loss?.avg ?? pickFirst(
        sumReceived?.lost_percent,
        lossFromPackets,
        sumSent?.lost_percent,
        receiverStream?.lost_percent,
        senderStream?.lost_percent,
      );

      let latencyMs = stats?.latency?.avg ?? pickFirst(
        senderStream?.mean_rtt,
        senderStream?.rtt,
        receiverStream?.mean_rtt,
        receiverStream?.rtt,
      );
      if (latencyMs !== undefined && latencyMs !== null && latencyMs > 1000) {
        latencyMs = latencyMs / 1000;
      }

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
            label: entry.label || '子测试',
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
            label: entry.label || '子测试',
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
        const key = entry.label || `子测试 ${idx + 1}`;
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

    function formatMetric(value, decimals = 2) {
      if (value === undefined || value === null || Number.isNaN(value)) return 'N/A';
      return Number(value).toFixed(decimals);
    }

    function renderMetricStat(label, stats, unit = '') {
      if (!stats) return null;
      const unitLabel = unit ? ` ${unit}` : '';
      const primary = stats.avg ?? stats.mean ?? stats.max ?? stats.min;
      const wrap = document.createElement('div');
      wrap.className = 'border border-slate-800 bg-slate-950/70 p-3 text-xs text-slate-300 shadow-inner shadow-black/10';
      wrap.innerHTML = `
        <div class="flex items-center justify-between">
          <span class="font-medium">${label}</span>
          <span class="text-sm font-semibold text-slate-50">${formatMetric(primary)}${unitLabel}</span>
        </div>
        <div class="mt-1 text-[10px] text-slate-500">max ${formatMetric(stats.max)}${unitLabel} · min ${formatMetric(stats.min)}${unitLabel}</div>
      `;
      return wrap;
    }

    function buildMetricGrid(metrics) {
      if (!metrics) return null;
      const grid = document.createElement('div');
      grid.className = 'grid gap-2 sm:grid-cols-2 lg:grid-cols-4';

      [
        renderMetricStat('RTT 均值 (ms)', metrics.latencyStats, 'ms'),
        renderMetricStat('抖动均值 (ms)', metrics.jitterStats, 'ms'),
        renderMetricStat('丢包均值 (%)', metrics.lossStats, '%'),
        renderMetricStat('重传次数', metrics.retransStats, '次'),
      ]
        .filter(Boolean)
        .forEach((node) => grid.appendChild(node));

      return grid.childNodes.length ? grid : null;
    }

    // ============ Ping History Shared Cache System ============
    // Shared cache for both trend updates and sparkline popovers
    const pingHistoryCache = {};
    const PING_CACHE_TTL = 60000; // 60 seconds
    const TREND_CACHE_KEY = 'pingTrendCache';
    
    // Request throttling to prevent concurrent API calls
    const pendingRequests = {};
    const REQUEST_TIMEOUT = 8000; // 8 second timeout
    
    // Fetch ping history with caching and deduplication
    async function fetchPingHistory(nodeId) {
      const cacheKey = `node-${nodeId}`;
      const now = Date.now();
      
      // Return cached data if fresh
      if (pingHistoryCache[cacheKey] && now - pingHistoryCache[cacheKey].ts < PING_CACHE_TTL) {
        return pingHistoryCache[cacheKey].data;
      }
      
      // If request already pending, wait for it
      if (pendingRequests[cacheKey]) {
        return pendingRequests[cacheKey];
      }
      
      // Create new request with timeout
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), REQUEST_TIMEOUT);
      
      pendingRequests[cacheKey] = (async () => {
        try {
          const res = await fetch(`/api/ping/history/${nodeId}`, { signal: controller.signal });
          clearTimeout(timeoutId);
          
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const data = await res.json();
          
          if (data.status === 'ok') {
            pingHistoryCache[cacheKey] = { data, ts: now };
            return data;
          }
          return null;
        } catch (e) {
          if (e.name === 'AbortError') {
            console.warn(`Ping history request timeout for node ${nodeId}`);
          } else {
            console.warn(`Failed to fetch ping history for node ${nodeId}:`, e.message);
          }
          return null;
        } finally {
          delete pendingRequests[cacheKey];
        }
      })();
      
      return pendingRequests[cacheKey];
    }
    
    // LocalStorage trend cache for persistence across page loads
    function getTrendCache() {
      try {
        return JSON.parse(localStorage.getItem(TREND_CACHE_KEY) || '{}');
      } catch { return {}; }
    }
    function setTrendCache(nodeId, carrier, trend) {
      try {
        const cache = getTrendCache();
        const key = `${nodeId}-${carrier}`;
        cache[key] = { symbol: trend.symbol, color: trend.color, ts: Date.now() };
        localStorage.setItem(TREND_CACHE_KEY, JSON.stringify(cache));
      } catch {}
    }
    function getCachedTrend(nodeId, carrier) {
      const cache = getTrendCache();
      const key = `${nodeId}-${carrier}`;
      const item = cache[key];
      // Use cache if less than 5 minutes old
      if (item && Date.now() - item.ts < 300000) {
        return item;
      }
      return null;
    }
    
    // Generate human-readable trend description
    function getTrendDescription(trend) {
      if (!trend) return '数据不足';
      const pct = trend.pct !== undefined ? trend.pct : 0;
      const avg = trend.avg_24h !== undefined ? trend.avg_24h : null;
      
      const descriptions = {
        'sharp_down': `延迟显著改善 (${pct > 0 ? '+' : ''}${pct}%)`,
        'down': `延迟改善 (${pct > 0 ? '+' : ''}${pct}%)`,
        'slight_down': `延迟略有改善 (${pct > 0 ? '+' : ''}${pct}%)`,
        'stable': '延迟稳定',
        'slight_up': `延迟略有上升 (+${Math.abs(pct)}%)`,
        'up': `延迟上升 (+${Math.abs(pct)}%)`,
        'sharp_up': `延迟显著恶化 (+${Math.abs(pct)}%)`
      };
      
      let desc = descriptions[trend.direction] || '延迟稳定';
      if (avg !== null) desc += ` · 24h均值: ${avg}ms`;
      return desc;
    }

    function renderBackboneBadges(entries, nodeId) {
      if (!entries || !entries.length) return '';

      const labelMap = { zj_cu: 'CU', zj_ct: 'CT', zj_cm: 'CM' };
      const colorMap = {
        'CU': 'bg-red-500/15 text-red-200 border-red-500/40',
        'CT': 'bg-green-500/15 text-green-200 border-green-500/40',
        'CM': 'bg-blue-500/15 text-blue-200 border-blue-500/40'
      };
      return entries
        .map((item) => {
          const label = labelMap[item.key] || (item.name || item.key || '').slice(0, 2).toUpperCase();
          const hasLatency = item.latency_ms !== undefined && item.latency_ms !== null;
          const chipStyle = hasLatency
            ? (colorMap[label] || 'bg-sky-500/10 text-sky-100 border-sky-500/40')
            : 'bg-rose-500/15 text-rose-200 border-rose-500/40';
          const latencyLabel = hasLatency ? `${formatMetric(item.latency_ms, 0)} ms` : '不可达';
          const trendSpanId = nodeId ? ` id="trend-${nodeId}-${label}"` : '';
          
          // Use cached trend if available to prevent flicker
          const cached = nodeId ? getCachedTrend(nodeId, label) : null;
          const trendSymbol = cached ? cached.symbol : '·';
          const trendColor = cached ? cached.color : '#64748b';
          // No title attribute - using sparkline popover instead
          
          return `<span class="inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-[11px] font-semibold ${chipStyle}">` +
            `<span${trendSpanId} class="cursor-help" style="color: ${trendColor}; transition: all 0.3s">${trendSymbol}</span>` +
            `<span>${label}</span>` +
            `<span class=\"text-[10px] text-slate-300\">${latencyLabel}</span></span>`;
        })
        .join('');
    }
    
    // Async fetch and update ping trends for a node with smooth animation
    async function updateNodePingTrends(nodeId) {
      const data = await fetchPingHistory(nodeId);
      
      // Handle fetch failure - show error state
      if (!data || !data.trends) {
        ['CU', 'CM', 'CT'].forEach(carrier => {
          const trendEl = document.getElementById(`trend-${nodeId}-${carrier}`);
          if (trendEl && trendEl.textContent !== '?' && !getCachedTrend(nodeId, carrier)) {
            trendEl.textContent = '?';
            trendEl.style.color = '#64748b';
            trendEl.title = '数据获取失败';
          }
        });
        return;
      }
      
      // Update each carrier trend arrow with smooth transition
      Object.entries(data.trends).forEach(([carrier, trend]) => {
        const trendEl = document.getElementById(`trend-${nodeId}-${carrier}`);
        if (trendEl && trend) {
          const newSymbol = trend.symbol || '→';
          const newColor = trend.color || '#94a3b8';
          const oldSymbol = trendEl.textContent;
          
          // Store trend data for popover description
          trendEl.dataset.trend = JSON.stringify(trend);
          
          // Only animate if value changed
          if (oldSymbol !== newSymbol || trendEl.style.color !== newColor) {
            // Add transition style for smooth color change
            trendEl.style.transition = 'all 0.3s ease-out';
            
            // Subtle pulse animation for updates
            trendEl.animate([
              { opacity: 1, transform: 'scale(1)' },
              { opacity: 0.5, transform: 'scale(0.8)' },
              { opacity: 1, transform: 'scale(1.1)' },
              { opacity: 1, transform: 'scale(1)' }
            ], { duration: 400, easing: 'ease-out' });
            
            // Update content with slight delay for smoother effect
            setTimeout(() => {
              trendEl.textContent = newSymbol;
              trendEl.style.color = newColor;
            }, 150);
          }
          
          // Remove native tooltip - using sparkline popover instead
          trendEl.removeAttribute('title');
          
          // Save to cache for next page load
          setTrendCache(nodeId, carrier, { symbol: newSymbol, color: newColor });
        }
      });
    }

    // ============ Sparkline Popover System ============
    const sparklineCache = {};
    let sparklineTimeout = null;
    
    function drawSparkline(canvas, data, carrier) {
      const ctx = canvas.getContext('2d');
      const width = canvas.width;
      const height = canvas.height;
      const padding = 4;
      
      // Clear canvas
      ctx.clearRect(0, 0, width, height);
      
      if (!data || data.length < 2) {
        ctx.fillStyle = '#64748b';
        ctx.font = '11px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('数据不足', width/2, height/2 + 4);
        return;
      }
      
      const values = data.map(d => d.ms);
      const min = Math.min(...values);
      const max = Math.max(...values);
      const range = max - min || 1;
      
      // Carrier colors
      const colors = { 
        CU: { line: '#f87171', fill: 'rgba(248, 113, 113, 0.15)' },
        CM: { line: '#60a5fa', fill: 'rgba(96, 165, 250, 0.15)' },
        CT: { line: '#4ade80', fill: 'rgba(74, 222, 128, 0.15)' }
      };
      const color = colors[carrier] || { line: '#94a3b8', fill: 'rgba(148, 163, 184, 0.1)' };
      
      // Draw filled area
      ctx.beginPath();
      ctx.moveTo(padding, height - padding);
      data.forEach((d, i) => {
        const x = padding + (i / (data.length - 1)) * (width - padding * 2);
        const y = height - padding - ((d.ms - min) / range) * (height - padding * 2);
        ctx.lineTo(x, y);
      });
      ctx.lineTo(width - padding, height - padding);
      ctx.closePath();
      ctx.fillStyle = color.fill;
      ctx.fill();
      
      // Draw line
      ctx.beginPath();
      data.forEach((d, i) => {
        const x = padding + (i / (data.length - 1)) * (width - padding * 2);
        const y = height - padding - ((d.ms - min) / range) * (height - padding * 2);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.strokeStyle = color.line;
      ctx.lineWidth = 1.5;
      ctx.stroke();
      
      // Draw latest point
      const lastX = width - padding;
      const lastY = height - padding - ((values[values.length - 1] - min) / range) * (height - padding * 2);
      ctx.beginPath();
      ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
      ctx.fillStyle = color.line;
      ctx.fill();
    }
    
    async function showSparklinePopover(nodeId, carrier, targetEl) {
      const popover = document.getElementById('sparkline-popover');
      const canvas = document.getElementById('sparkline-canvas');
      const carrierEl = document.getElementById('sparkline-carrier');
      const descEl = document.getElementById('sparkline-desc');
      
      // Smart positioning: detect edges and adjust
      const rect = targetEl.getBoundingClientRect();
      const popoverWidth = 220;
      const popoverHeight = 140;
      const margin = 8;
      
      let left = rect.left - 50;
      let top = rect.bottom + margin;
      
      // Adjust if too close to right edge
      if (left + popoverWidth > window.innerWidth - margin) {
        left = window.innerWidth - popoverWidth - margin;
      }
      // Adjust if too close to left edge
      if (left < margin) {
        left = margin;
      }
      // Adjust if too close to bottom edge - show above instead
      if (top + popoverHeight > window.innerHeight - margin) {
        top = rect.top - popoverHeight - margin;
      }
      
      popover.style.left = `${left}px`;
      popover.style.top = `${top}px`;
      
      // Update carrier label
      carrierEl.textContent = carrier;
      carrierEl.className = `sparkline-carrier ${carrier.toLowerCase()}`;
      
      // Show loading state
      document.getElementById('sparkline-current').textContent = '...';
      document.getElementById('sparkline-avg').textContent = '...';
      document.getElementById('sparkline-min').textContent = '...';
      document.getElementById('sparkline-max').textContent = '...';
      if (descEl) descEl.textContent = '加载中...';
      
      popover.classList.add('show');
      
      // Get trend data from element for description
      let trendData = null;
      try {
        if (targetEl.dataset.trend) {
          trendData = JSON.parse(targetEl.dataset.trend);
        }
      } catch {}
      
      // Use shared cache via fetchPingHistory
      const data = await fetchPingHistory(nodeId);
      
      if (data && data.carriers && data.carriers[carrier]) {
        renderSparklineData(data.carriers[carrier], carrier, canvas, trendData);
      } else {
        // Show error state
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = '#ef4444';
        ctx.font = '11px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('数据获取失败', canvas.width/2, canvas.height/2 + 4);
        if (descEl) descEl.textContent = '无法获取数据';
      }
    }
    
    function renderSparklineData(data, carrier, canvas, trendData = null) {
      if (!data || !data.length) return;
      
      drawSparkline(canvas, data, carrier);
      
      const values = data.map(d => d.ms);
      const current = values[values.length - 1];
      const avg = Math.round(values.reduce((a, b) => a + b, 0) / values.length);
      const min = Math.min(...values);
      const max = Math.max(...values);
      
      document.getElementById('sparkline-current').textContent = current;
      document.getElementById('sparkline-avg').textContent = avg;
      document.getElementById('sparkline-min').textContent = min;
      document.getElementById('sparkline-max').textContent = max;
      
      // Show trend description
      const descEl = document.getElementById('sparkline-desc');
      if (descEl) {
        descEl.textContent = getTrendDescription(trendData);
      }
    }
    
    function hideSparklinePopover() {
      document.getElementById('sparkline-popover').classList.remove('show');
    }
    
    // Event delegation for trend badge hover
    document.addEventListener('mouseover', (e) => {
      const trendEl = e.target.closest('[id^="trend-"]');
      if (trendEl && trendEl.id.startsWith('trend-')) {
        const parts = trendEl.id.split('-');
        if (parts.length >= 3) {
          const nodeId = parts[1];
          const carrier = parts[2];
          clearTimeout(sparklineTimeout);
          sparklineTimeout = setTimeout(() => {
            showSparklinePopover(nodeId, carrier, trendEl);
          }, 300);
        }
      }
    });
    
    document.addEventListener('mouseout', (e) => {
      const trendEl = e.target.closest('[id^="trend-"]');
      if (trendEl) {
        clearTimeout(sparklineTimeout);
        hideSparklinePopover();
      }
    });

    function formatNodeLabel(nodeId) {
      const node = nodeCache.find((n) => n.id === Number(nodeId));
      if (node && node.name) return node.name;
      return `节点 ${nodeId}`;
    }

    function renderRawResult(raw) {
      const wrap = document.createElement('div');
      wrap.className = 'overflow-auto rounded-xl border border-slate-800/70 bg-slate-950/60 p-3';

      if (!raw) {
        wrap.textContent = '无原始结果。';
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

      addSummaryRow('状态', raw.status || 'unknown');
      addSummaryRow('发送速率 (Mbps)', sumSent.bits_per_second ? formatMetric(sumSent.bits_per_second / 1e6) : 'N/A');
      addSummaryRow('接收速率 (Mbps)', sumReceived.bits_per_second ? formatMetric(sumReceived.bits_per_second / 1e6) : 'N/A');
      addSummaryRow('发送拥塞控制', end.sender_tcp_congestion || 'N/A');
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

    loginButton?.addEventListener('click', (event) => { event.preventDefault(); login(); });
    loginForm?.addEventListener('submit', (event) => { event.preventDefault(); login(); });
    document.getElementById('logout-btn')?.addEventListener('click', logout);
    document.getElementById('run-test')?.addEventListener('click', runTest);
    document.getElementById('run-suite-test')?.addEventListener('click', runSuiteTest);
    protocolSelect?.addEventListener('change', toggleProtocolOptions);
    singleTestTab?.addEventListener('click', () => setActiveTestTab('single'));
    suiteTestTab?.addEventListener('click', () => setActiveTestTab('suite'));
    suiteDstSelect?.addEventListener('change', syncSuitePort);
    suiteSrcSelect?.addEventListener('change', syncSuitePort);
    changePasswordBtn?.addEventListener('click', changePassword);
    saveNodeBtn?.addEventListener('click', saveNode);

    if (openAddNodeBtn) {
      openAddNodeBtn.addEventListener('click', () => {
        resetNodeForm();
        openAddNodeModal();
      });
    }

    if (closeAddNodeBtn) {
      closeAddNodeBtn.addEventListener('click', () => {
        closeAddNodeModal();
        resetNodeForm();
      });
    }

    if (cancelAddNodeBtn) {
      cancelAddNodeBtn.addEventListener('click', () => {
        closeAddNodeModal();
        resetNodeForm();
      });
    }

    if (addNodeModal) {
      addNodeModal.addEventListener('click', (event) => {
        if (event.target === addNodeModal) {
          closeAddNodeModal();
          resetNodeForm();
        }
      });
    }

    importConfigsBtn?.addEventListener('click', () => configFileInput?.click());
    exportConfigsBtn?.addEventListener('click', exportAgentConfigs);
    configFileInput?.addEventListener('change', (e) => importAgentConfigs(e.target.files[0]));
    document.getElementById('refresh-tests')?.addEventListener('click', refreshTests);
    deleteAllTestsBtn?.addEventListener('click', clearAllTests);
    
    // Pagination event listeners
    document.getElementById('tests-prev')?.addEventListener('click', () => {
      if (testsCurrentPage > 1) {
        testsCurrentPage--;
        refreshTests();
      }
    });
    document.getElementById('tests-next')?.addEventListener('click', () => {
      const pageSize = getTestsPageSize();
      const totalPages = Math.ceil(testsAllData.length / pageSize);
      if (testsCurrentPage < totalPages) {
        testsCurrentPage++;
        refreshTests();
      }
    });
    document.getElementById('tests-page-size')?.addEventListener('change', () => {
      testsCurrentPage = 1;
      refreshTests();
    });

    document.querySelectorAll('[data-refresh-nodes]').forEach((btn) => btn.addEventListener('click', refreshNodes));
    dstSelect?.addEventListener('change', syncTestPort);
    passwordInput?.addEventListener('keyup', (e) => { if (e.key === 'Enter') login(); });

    function updateNodeStreamingBadges(nodeId) {
      const container = document.querySelector(`[data-streaming-badges="${nodeId}"]`);
      if (container) {
        container.innerHTML = renderStreamingBadges(nodeId);
      }
    }

    function ensureAutoRefresh() {
      if (nodeRefreshInterval) return;
      nodeRefreshInterval = setInterval(() => refreshNodes(), 10000);
    }

    toggleProtocolOptions();
    setActiveTestTab('single');
    syncSuitePort();
    checkAuth();
    ensureAutoRefresh();

window.openSettingsTab = openSettingsTab;
window.toggleSettingsModal = toggleSettingsModal;
window.togglePasswordModal = togglePasswordModal;
window.toggleTracerouteModal = toggleTracerouteModal;
window.openTestModal = openTestModal;
window.executeTraceroute = executeTraceroute;
window.setActiveSettingsTab = setActiveSettingsTab;
window.toggleNodeSelection = toggleNodeSelection;
window.saveTelegramConfig = saveTelegramConfig;
window.testTelegramConfig = testTelegramConfig;
window.clearAllTestData = clearAllTestData;
window.clearScheduleResults = clearScheduleResults;
window.refreshWhitelist = refreshWhitelist;
window.addWhitelistIp = addWhitelistIp;
window.removeWhitelistIp = removeWhitelistIp;
window.syncWhitelist = syncWhitelist;
window.checkWhitelistStatus = checkWhitelistStatus;
window.login = login;
window.guestLogin = guestLogin;
window.changePassword = changePassword;
window.refreshNodes = refreshNodes;
window.runStreamingCheck = runStreamingCheck;
window.editNode = editNode;
window.refreshTests = refreshTests;
window.deleteTestResult = deleteTestResult;
window.clearAllTests = clearAllTests;
window.saveNode = saveNode;
window.runTest = runTest;
window.runSuiteTest = runSuiteTest;
window.exportAgentConfigs = exportAgentConfigs;
window.importAgentConfigs = importAgentConfigs;
