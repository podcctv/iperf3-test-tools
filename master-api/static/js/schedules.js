import { apiFetch } from './api.js';
    let nodes = [];
    let schedules = [];
    let editingScheduleId = null;
    let charts = {};

    // 加载节点列表
    async function loadNodes() {
      const res = await apiFetch('/nodes');
      nodes = await res.json();
      updateNodeSelects();
    }

    function updateNodeSelects() {
      const srcSelect = document.getElementById('schedule-src');
      const dstSelect = document.getElementById('schedule-dst');
      
      const options = nodes.map(n => `<option value="${n.id}">${n.name} (${maskAddress(n.ip, true)})</option>`).join('');
      srcSelect.innerHTML = options;
      dstSelect.innerHTML = options;
    }

    // 加载定时任务列表
    // Masking Helper (Global for Schedules)
    function maskAddress(addr, hidden) {
        if (!hidden || !addr) return addr;
        // Check if IP
        if (addr.match(/^\d+\.\d+\.\d+\.\d+$/)) {
             const p = addr.split('.');
             return `${p[0]}.${p[1]}.*.*`;
        }
        // Check if Domain (at least one dot)
        if (addr.includes('.')) {
            const parts = addr.split('.');
            if (parts.length > 2) {
                // aa.bb.cc -> aa.**.**
                // aa.bb.cc.dd -> aa.bb.**.**
                // Strategy: Keep first part, mask specific suffix or just last two parts
                // User requirement: aa.bb.cc -> mask bb.cc
                // So keep part[0], mask rest
                return parts[0] + '.*.' + parts.slice(2).map(() => '*').join('.');
            } else if (parts.length === 2) {
                return parts[0] + '.*';
            }
        }
        return addr.substring(0, addr.length/2) + '*'.repeat(Math.ceil(addr.length/2));
    }

    async function loadSchedules() {
      const res = await apiFetch('/schedules');
      schedules = await res.json();
      window.schedulesData = schedules;  // Store globally for VPS card task counts
      renderSchedules();
      updateScheduleTrafficBadges();
      
      // Fetch ISPs
      schedules.forEach(s => {
         const src = nodes.find(n => n.id === s.src_node_id);
         const dst = nodes.find(n => n.id === s.dst_node_id);
         
         const fetchIsp = (ip, elemId) => {
             if (!ip) return;
             fetch(`/geo?ip=${ip}`)
               .then(r => r.json())
               .then(d => {
                   const el = document.getElementById(elemId);
                   if (el && d.isp) el.textContent = d.isp;
               }).catch(()=>void 0);
         };
         
         if (src) fetchIsp(src.ip, `sched-src-isp-${s.id}`);
         if (dst) fetchIsp(dst.ip, `sched-dst-isp-${s.id}`);
      });
    }

    // 渲染定时任务列表
    // Optimized renderSchedules to prevent chart flickering
    function renderSchedules() {
      const container = document.getElementById('schedules-container');
      
      if (schedules.length === 0) {
        container.innerHTML = '<div class="text-center text-slate-400 py-12">暂无定时任务,点击"新建任务"开始</div>';
        return;
      }
      
      // Clear initial loading text if present
      const loadingText = container.querySelector('.text-center.text-slate-400');
      if (loadingText) loadingText.remove();
      
      // Incremental Update Strategy
      // 1. Remove Deleted Cards
      const currentIds = schedules.map(s => s.id);
      Array.from(container.children).forEach(child => {
          const id = parseInt(child.id.replace('schedule-card-', ''));
          if (!isNaN(id) && !currentIds.includes(id)) {
              child.remove();
          }
      });
      
      // 2. Update or Create Cards
      schedules.forEach(schedule => {
        let card = document.getElementById(`schedule-card-${schedule.id}`);
        const srcNode = nodes.find(n => n.id === schedule.src_node_id);
        const dstNode = nodes.find(n => n.id === schedule.dst_node_id);
        
        // Status Badge Logic
        const statusBadge = schedule.enabled 
          ? '<span class="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-emerald-500/20 text-emerald-300 text-xs font-semibold"><span class="h-2 w-2 rounded-full bg-emerald-400"></span>运行中</span>'
          : '<span class="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-slate-700 text-slate-400 text-xs font-semibold"><span class="h-2 w-2 rounded-full bg-slate-500"></span>已暂停</span>';

        const runBtnText = schedule.enabled ? '暂停' : '启用';
        
        // Direction Arrow
        const arrow = schedule.direction === 'download' ? '←' : 
                      schedule.direction === 'bidirectional' ? '↔' : '→';

        const htmlContent = `
            <!-- Schedule Header -->
            <div class="flex items-center justify-between">
              <div class="flex-1">
                <h3 class="text-lg font-bold text-white">${schedule.name}</h3>
                <div class="mt-2 flex items-center gap-4 text-sm text-slate-300">
                  <span id="sched-route-${schedule.id}">${srcNode?.name || 'Unknown'} <span class="w-6 inline-block text-center text-slate-500">${
                    schedule.direction === 'download' ? '←' : 
                    schedule.direction === 'bidirectional' ? '↔' : '→'
                  }</span> ${dstNode?.name || 'Unknown'}</span>
                  <span class="text-slate-500">|</span>
                  <span>${schedule.protocol.toUpperCase()}</span>
                  <span class="text-slate-500">|</span>
                  <span>${schedule.duration}秒</span>
                  <span class="text-slate-500">|</span>
                  <span>${schedule.cron_expression ? cronToChineseLabel(schedule.cron_expression) : (schedule.interval_seconds ? '每' + Math.floor(schedule.interval_seconds / 60) + '分钟' : '--')}</span>
                  <!-- Traffic Badge -->
                  <span class="px-2 py-0.5 rounded-md bg-gradient-to-r from-blue-500/20 to-cyan-500/20 border border-blue-500/30 text-xs font-semibold text-blue-200" id="traffic-badge-${schedule.id}">
                    --
                  </span>
                </div>
              </div>
              
              <div class="flex items-center gap-4">
                <div class="hidden md:block text-xs text-right space-y-0.5">
                   <div class="text-slate-400">Next Run</div>
                   <div class="font-mono text-emerald-400" data-countdown="${schedule.next_run_at || ''}" data-schedule-id="${schedule.id}">Calculating...</div>
                </div>
                ${statusBadge}
                ${!window.isGuest ? `<div class="flex items-center gap-2">
                    <button onclick="toggleSchedule(${schedule.id})" class="px-3 py-1 rounded-lg border border-slate-700 bg-slate-800 text-xs font-semibold text-slate-100 hover:border-sky-500 transition whitespace-nowrap" id="btn-toggle-${schedule.id}">
                    ${schedule.enabled ? '暂停' : '启用'}
                    </button>
                    <button onclick="runSchedule(${schedule.id})" class="px-3 py-1 rounded-lg border border-slate-700 bg-slate-800 text-xs font-semibold text-slate-100 hover:emerald-500 transition whitespace-nowrap">立即运行</button>
                    <button onclick="editSchedule(${schedule.id})" class="px-3 py-1 rounded-lg border border-slate-700 bg-slate-800 text-xs font-semibold text-slate-100 hover:border-sky-500 transition whitespace-nowrap">编辑</button>
                    <button onclick="shareSchedule(${schedule.id})" class="px-3 py-1 rounded-lg border border-cyan-700 bg-cyan-900/20 text-xs font-semibold text-cyan-300 hover:bg-cyan-900/40 transition whitespace-nowrap" title="分享图表截图">📷</button>
                    <button onclick="shareScheduleMarkdown(${schedule.id})" class="px-3 py-1 rounded-lg border border-violet-700 bg-violet-900/20 text-xs font-semibold text-violet-300 hover:bg-violet-900/40 transition whitespace-nowrap" title="复制Markdown信息">📋</button>
                    <button onclick="deleteSchedule(${schedule.id})" class="px-3 py-1 rounded-lg border border-rose-700 bg-rose-900/20 text-xs font-semibold text-rose-300 hover:bg-rose-900/40 transition whitespace-nowrap">删除</button>
                </div>` : ''}
              </div>
            </div>
            
            <!-- Nodes Info with ISP & Masking -->
            <div class="grid grid-cols-2 gap-4 text-xs mt-4">
                <div class="glass-card p-2 rounded-lg bg-slate-900/30 flex flex-col gap-1">
                    <div class="text-slate-400">Source</div>
                    <div class="font-mono text-sky-300">
                        ${srcNode ? maskAddress(srcNode.ip, true) : 'Unknown'}
                        <span id="sched-src-isp-${schedule.id}" class="ml-1 text-[10px] text-slate-500 border-l border-slate-700 pl-1"></span>
                    </div>
                </div>
                 <div class="glass-card p-2 rounded-lg bg-slate-900/30 flex flex-col gap-1">
                    <div class="text-slate-400">Destination</div>
                    <div class="font-mono text-emerald-300">
                        ${dstNode ? maskAddress(dstNode.ip, true) : 'Unknown'}
                        <span id="sched-dst-isp-${schedule.id}" class="ml-1 text-[10px] text-slate-500 border-l border-slate-700 pl-1"></span>
                    </div>
                </div>
            </div>
            
            <!-- Chart Container -->
            <div class="glass-card rounded-xl p-4 mt-4">
              <div class="flex items-center justify-between mb-4">
                <h4 class="text-sm font-bold text-slate-200">24小时带宽监控</h4>
                <div class="flex items-center gap-2">
                   <button onclick="toggleHistory(${schedule.id})" class="flex items-center gap-1 px-2 py-1 rounded bg-slate-700 hover:bg-slate-600 text-xs">
                     <span class="text-lg leading-none">📊</span> 历史记录
                   </button>
                   <div class="flex items-center bg-slate-800 rounded-lg p-0.5 border border-slate-700">
                      <button onclick="changeDate(${schedule.id}, -1)" class="w-6 h-6 flex items-center justify-center hover:bg-slate-700 rounded text-slate-400 hover:text-white transition">◀</button>
                      <span id="date-${schedule.id}" class="text-xs font-mono px-2 min-w-[80px] text-center text-slate-300">今天</span>
                      <button onclick="changeDate(${schedule.id}, 1)" class="w-6 h-6 flex items-center justify-center hover:bg-slate-700 rounded text-slate-400 hover:text-white transition">▶</button>
                   </div>
                </div>
              </div>
              <div class="w-full" style="position: relative; height: 16rem;">
                <canvas id="chart-${schedule.id}" style="width: 100% !important; height: 100% !important;"></canvas>
              </div>
              <div id="stats-${schedule.id}"></div>
              
              <!-- History Panel -->
              <div id="history-panel-${schedule.id}" class="hidden mt-4 pt-4 border-t border-slate-700/50">
                  <div class="flex items-center justify-between mb-3">
                    <h5 class="text-sm font-bold text-slate-300">📜 测试历史</h5>
                    <div id="history-pagination-${schedule.id}" class="flex items-center gap-2 text-xs">
                      <button onclick="historyPage(${schedule.id}, -1)" class="px-2 py-1 rounded bg-slate-700 hover:bg-slate-600 text-slate-300" data-prev>« 上页</button>
                      <span class="text-slate-400" data-info>1/1</span>
                      <button onclick="historyPage(${schedule.id}, 1)" class="px-2 py-1 rounded bg-slate-700 hover:bg-slate-600 text-slate-300" data-next>下页 »</button>
                    </div>
                  </div>
                  <div class="overflow-x-auto custom-scrollbar">
                    <table class="w-full text-xs">
                      <thead>
                        <tr class="text-slate-400 border-b border-slate-700">
                          <th class="py-2 px-2 text-left font-medium">时间</th>
                          <th class="py-2 px-2 text-left font-medium">协议</th>
                          <th class="py-2 px-2 text-right font-medium text-sky-400">上传(Mb)</th>
                          <th class="py-2 px-2 text-right font-medium text-emerald-400">下载(Mb)</th>
                          <th class="py-2 px-2 text-right font-medium">延迟(ms)</th>
                          <th class="py-2 px-2 text-right font-medium">丢包(%)</th>
                          <th class="py-2 px-2 text-center font-medium">状态</th>
                        </tr>
                      </thead>
                      <tbody id="history-${schedule.id}" class="text-slate-300">
                        <tr><td colspan="7" class="py-3 text-center text-slate-500">加载中...</td></tr>
                      </tbody>
                    </table>
                  </div>
              </div>
            </div>`;

        if (!card) {
            // New Card
            const div = document.createElement('div');
            div.id = `schedule-card-${schedule.id}`;
            div.className = "glass-card rounded-2xl p-6 space-y-4 mb-6";
            div.innerHTML = htmlContent;
            container.appendChild(div);
            
            // Initial Chart Load
            loadChartData(schedule.id);
            // loadHistory(schedule.id); // Integrated into loadChartData
            updateCountdowns(); // Ensure countdown starts
            
            // Mask/ISP update for new card
             // Fetch ISPs
             const fetchIsp = (ip, elemId) => {
                 if (!ip) return;
                 fetch(`/geo?ip=${ip}`)
                   .then(r => r.json())
                   .then(d => {
                       const el = document.getElementById(elemId);
                       if (el && d.isp) el.textContent = d.isp;
                   }).catch(()=>void 0);
             };
             
             if (srcNode) fetchIsp(srcNode.ip, `sched-src-isp-${schedule.id}`);
             if (dstNode) fetchIsp(dstNode.ip, `sched-dst-isp-${schedule.id}`);
            
        } else {
            // Existing Card - Diff Updates
            // Update Status Badge
            const statusEl = document.getElementById(`status-badge-${schedule.id}`);
            if (statusEl && statusEl.innerHTML !== statusBadge) statusEl.innerHTML = statusBadge;
            
            // Update Countdown Attribute
            const countdownEl = card.querySelector(`[data-countdown]`);
            if (countdownEl && schedule.next_run_at) {
                 if (countdownEl.dataset.countdown !== schedule.next_run_at) {
                     countdownEl.dataset.countdown = schedule.next_run_at;
                     updateCountdowns(); // Refresh text immediately
                 }
            }
            
            // Update Toggle Button Text
            const btnToggle = document.getElementById(`btn-toggle-${schedule.id}`);
            if (btnToggle && btnToggle.innerText.trim() !== runBtnText) btnToggle.innerText = runBtnText;
        }
      });
      
      // Global Countdown Timer (Ensure only one)
      if (!window.countdownInterval) {
          window.countdownInterval = setInterval(updateCountdowns, 1000);
      }
    }



    function toggleHistory(scheduleId) {
        const panel = document.getElementById(`history-panel-${scheduleId}`);
        if (panel) {
            panel.classList.toggle('hidden');
        }
    }

    // 加载图表数据
    async function loadChartData(scheduleId, date = null) {
      const dateEl = document.getElementById(`date-${scheduleId}`);
      // 如果没有指定date，且当前也没显示日期，则默认今天
      if (!date && (!dateEl || dateEl.textContent === '今天')) {
         const d = new Date();
         date = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
      } else if (!date) {
         // 使用当前显示的日期
         const currentDate = new Date(dateEl.textContent);
         date = `${currentDate.getFullYear()}-${String(currentDate.getMonth()+1).padStart(2,'0')}-${String(currentDate.getDate()).padStart(2,'0')}`;
      }
      
      const tzOffset = new Date().getTimezoneOffset();
      const res = await apiFetch(`/schedules/${scheduleId}/results?date=${date}&tz_offset=${tzOffset}`);
      const data = await res.json();
      
      renderChart(scheduleId, data.results, date);
      renderHistoryTable(scheduleId, data.results);
    }
    
    // 历史记录分页数据存储
    const historyPageData = {};
    const HISTORY_PAGE_SIZE = 10;
    
    // 渲染历史表格（带分页）
    function renderHistoryTable(scheduleId, results, page = 1) {
      const tbody = document.getElementById(`history-${scheduleId}`);
      const pagination = document.getElementById(`history-pagination-${scheduleId}`);
      if (!tbody) return;
      
      // Store results for pagination
      historyPageData[scheduleId] = { results: [...results].reverse(), page };
      
      if (results.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="py-3 text-center text-slate-500">暂无数据</td></tr>';
        if (pagination) pagination.classList.add('hidden');
        return;
      }
      
      // Pagination calculation
      const sorted = historyPageData[scheduleId].results;
      const totalPages = Math.ceil(sorted.length / HISTORY_PAGE_SIZE);
      const currentPage = Math.max(1, Math.min(page, totalPages));
      historyPageData[scheduleId].page = currentPage;
      
      const start = (currentPage - 1) * HISTORY_PAGE_SIZE;
      const pageData = sorted.slice(start, start + HISTORY_PAGE_SIZE);
      
      // Update pagination UI
      if (pagination) {
        pagination.classList.remove('hidden');
        const info = pagination.querySelector('[data-info]');
        const prevBtn = pagination.querySelector('[data-prev]');
        const nextBtn = pagination.querySelector('[data-next]');
        if (info) info.textContent = `${currentPage}/${totalPages}`;
        if (prevBtn) prevBtn.disabled = currentPage <= 1;
        if (nextBtn) nextBtn.disabled = currentPage >= totalPages;
      }
      
      tbody.innerHTML = pageData.map(r => {
          const time = new Date(r.executed_at).toLocaleTimeString('zh-CN', {
            hour: '2-digit', minute: '2-digit', second: '2-digit'
          });
          const statusClass = r.status === 'success' ? 'bg-emerald-500/20 text-emerald-400' : 'bg-rose-500/20 text-rose-400';
          const s = r.test_result?.summary || {};
          const protocol = (r.test_result?.protocol || 'tcp').toUpperCase();
          
          // Determine upload/download speeds
          let up = '-';
          let down = '-';
          
          if (s.upload_bits_per_second) {
              up = (s.upload_bits_per_second / 1000000).toFixed(2);
          }
          if (s.download_bits_per_second) {
              down = (s.download_bits_per_second / 1000000).toFixed(2);
          }
          
          // Fallback compatibility
          if (up === '-' && down === '-' && s.bits_per_second) {
              const bps = (s.bits_per_second / 1000000).toFixed(2);
              const isReverse = r.test_result?.params?.reverse || r.test_result?.raw_result?.start?.test_start?.reverse;
              if (isReverse) down = bps;
              else up = bps;
          }
          
          // TCP 不显示丢包，UDP 才有丢包数据
          const lostPercent = protocol === 'UDP' 
            ? (s.lost_percent?.toFixed(2) || '-')
            : '<span class="text-slate-600">-</span>';
          
          return `
            <tr class="border-b border-slate-800/50 hover:bg-slate-800/30 transition-colors">
              <td class="py-2 px-2 font-mono">${time}</td>
              <td class="py-2 px-2"><span class="px-1.5 py-0.5 rounded text-[10px] font-bold ${protocol === 'TCP' ? 'bg-sky-500/20 text-sky-400' : 'bg-purple-500/20 text-purple-400'}">${protocol}</span></td>
              <td class="py-2 px-2 text-right font-mono text-sky-400">${up}</td>
              <td class="py-2 px-2 text-right font-mono text-emerald-400">${down}</td>
              <td class="py-2 px-2 text-right font-mono">${s.latency_ms?.toFixed(1) || '-'}</td>
              <td class="py-2 px-2 text-right font-mono">${lostPercent}</td>
              <td class="py-2 px-2 text-center">
                <span class="px-2 py-0.5 rounded text-[10px] font-bold ${statusClass}" title="${r.error_message || ''}">
                  ${r.status === 'success' ? '✓' : '✗'}
                </span>
              </td>
            </tr>
          `;
      }).join('');
    }
    
    // 历史分页翻页
    function historyPage(scheduleId, offset) {
      const data = historyPageData[scheduleId];
      if (!data) return;
      const newPage = data.page + offset;
      renderHistoryTable(scheduleId, data.results.slice().reverse(), newPage);
    }



    // 渲染Chart.js图表
    function renderChart(scheduleId, results, date) {
      const canvas = document.getElementById(`chart-${scheduleId}`);
      if (!canvas) return;
      
      // 销毁旧图表
      if (charts[scheduleId]) {
        charts[scheduleId].destroy();
      }
      
      // 准备数据
      // Use map to group by timestamp
      const timeMap = new Map();
      const formatTime = (iso) => {
          const d = new Date(new Date(iso).getTime() + 8*60*60*1000); // UTC+8
          return d.toISOString().substring(11, 16);
      };

      results.forEach(r => {
          // Round to nearest minute to grouping
          const t = formatTime(r.executed_at);
          if (!timeMap.has(t)) {
              timeMap.set(t, { 
                  tcp_up: null, tcp_down: null, 
                  udp_up: null, udp_down: null 
              });
          }
          
          const entry = timeMap.get(t);
          const s = r.test_result?.summary || {};
          const proto = (r.test_result?.protocol || 'tcp').toLowerCase();
          
          // Values in Mbps
          const up = s.upload_bits_per_second ? (s.upload_bits_per_second / 1000000).toFixed(2) : 0;
          const down = s.download_bits_per_second ? (s.download_bits_per_second / 1000000).toFixed(2) : 0;
          
          if (proto === 'tcp') {
             if (parseFloat(up) > 0) entry.tcp_up = up;
             if (parseFloat(down) > 0) entry.tcp_down = down;
          } else if (proto === 'udp') {
             if (parseFloat(up) > 0) entry.udp_up = up;
             if (parseFloat(down) > 0) entry.udp_down = down;
          }
      });
      
      // Sort keys
      const labels = Array.from(timeMap.keys()).sort();
      const tcpUpData = labels.map(t => timeMap.get(t).tcp_up || 0);
      const tcpDownData = labels.map(t => timeMap.get(t).tcp_down || 0);
      const udpUpData = labels.map(t => timeMap.get(t).udp_up || 0);
      const udpDownData = labels.map(t => timeMap.get(t).udp_down || 0);
      
      // Stats Calculation Helper
      const calcStats = (data) => {
          const values = data.map(v => parseFloat(v)||0).filter(v => v > 0);
          if (!values.length) return null;
          const max = Math.max(...values);
          const avg = values.reduce((a, b) => a + b, 0) / values.length;
          const cur = values[values.length - 1];
          return { max: max.toFixed(2), avg: avg.toFixed(2), cur: cur.toFixed(2) };
      };

      const statsData = [
         { label: 'TCP 上传', color: 'sky', val: calcStats(tcpUpData) },
         { label: 'TCP 下载', color: 'emerald', val: calcStats(tcpDownData) },
         { label: 'UDP 上传', color: 'yellow', val: calcStats(udpUpData) },
         { label: 'UDP 下载', color: 'purple', val: calcStats(udpDownData) }
      ].filter(item => item.val);
      
      // 创建图表
      const ctx = canvas.getContext('2d');
      charts[scheduleId] = new Chart(ctx, {
        type: 'line',
        data: {
          labels: labels,
          datasets: [
            {
              label: 'TCP 上传',
              data: tcpUpData,
              borderColor: '#38bdf8', // sky-400
              backgroundColor: 'rgba(56, 189, 248, 0.1)',
              borderWidth: 2,
              pointRadius: 0,
              tension: 0.4,
              fill: true
            },
            {
              label: 'TCP 下载',
              data: tcpDownData,
              borderColor: '#34d399', // emerald-400
              backgroundColor: 'rgba(52, 211, 153, 0.1)',
              borderWidth: 2,
              pointRadius: 0,
              tension: 0.4,
              fill: true
            },
             {
              label: 'UDP 上传',
              data: udpUpData,
              borderColor: '#facc15', // yellow-400
              backgroundColor: 'rgba(250, 204, 21, 0.1)',
              borderWidth: 2,
              pointRadius: 0,
              tension: 0.4,
              fill: true
            },
            {
              label: 'UDP 下载',
              data: udpDownData,
              borderColor: '#c084fc', // purple-400
              backgroundColor: 'rgba(192, 132, 252, 0.1)',
              borderWidth: 2,
              pointRadius: 0,
              tension: 0.4,
              fill: true
            }
          ].filter(ds => ds.data.some(v => parseFloat(v) > 0))
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: {
            mode: 'index',
            intersect: false,
          },
          plugins: {
            legend: {
              display: true,
              position: 'top',
              labels: { 
                color: '#cbd5e1',
                usePointStyle: true,
                padding: 15,
                font: { size: 11 }
              }
            },
            tooltip: {
              backgroundColor: 'rgba(15, 23, 42, 0.9)',
              titleColor: '#cbd5e1',
              bodyColor: '#94a3b8',
              borderColor: 'rgba(148, 163, 184, 0.2)',
              borderWidth: 1,
              padding: 12,
              displayColors: true,
              callbacks: {
                afterLabel: function(context) {
                  const result = results[context.dataIndex];
                  if (!result.test_result?.summary) return '';
                  const s = result.test_result.summary;
                  return [
                    `延迟: ${s.latency_ms?.toFixed(2) || 'N/A'} ms`,
                    `丢包: ${s.lost_percent?.toFixed(2) || 'N/A'} %`
                  ];
                }
              }
            }
          },
          scales: {
            x: { 
              grid: {
                display: true,
                color: 'rgba(148, 163, 184, 0.15)',
                drawBorder: false,
                lineWidth: 0.5,
              },
              ticks: { 
                color: '#94a3b8',
                font: { size: 9 },
                maxRotation: 0,
                autoSkip: true,
                maxTicksLimit: 24,
              }
            },
            y: { 
              grid: {
                display: true,
                color: 'rgba(148, 163, 184, 0.15)',
                drawBorder: false,
                lineWidth: 0.5,
              },
              ticks: { 
                color: '#94a3b8',
                font: { size: 9 }
              },
              beginAtZero: true,
              title: { 
                display: true, 
                text: 'Mbps', 
                color: '#cbd5e1',
                font: { size: 10, weight: 'bold' }
              }
            }
          }
        }
      });
      
      // Force resize after creation to fix width issues
      setTimeout(() => {
        if (charts[scheduleId]) {
          charts[scheduleId].resize();
        }
      }, 100);
      
      // 显示统计信息
      const statsEl = document.getElementById(`stats-${scheduleId}`);
      if (statsEl) {
          if (statsData.length === 0) {
              statsEl.innerHTML = '<div class="text-xs text-slate-500 mt-2 text-center">暂无数据</div>';
          } else {
              statsEl.innerHTML = `<div class="mt-4 grid grid-cols-2 lg:grid-cols-4 gap-3">` + 
              statsData.map(s => `
                <div class="rounded-xl border border-${s.color}-500/20 bg-${s.color}-500/5 p-3 text-xs shadow-sm">
                  <div class="flex items-center gap-2 mb-2 pb-2 border-b border-${s.color}-500/10">
                    <div class="w-1.5 h-1.5 rounded-full bg-${s.color}-400 shadow shadow-${s.color}-400/50"></div>
                    <span class="font-bold text-${s.color}-400">${s.label}</span>
                  </div>
                  <div class="space-y-1">
                    <div class="flex justify-between items-center text-${s.color}-100/70"><span>Max</span><span class="font-mono text-${s.color}-100 font-medium">${s.val.max}<span class="text-[10px] opacity-60 ml-0.5">Mb</span></span></div>
                    <div class="flex justify-between items-center text-${s.color}-100/70"><span>Avg</span><span class="font-mono text-${s.color}-100 font-medium">${s.val.avg}<span class="text-[10px] opacity-60 ml-0.5">Mb</span></span></div>
                    <div class="flex justify-between items-center text-${s.color}-100/70"><span>Cur</span><span class="font-mono text-${s.color}-100 font-medium">${s.val.cur}<span class="text-[10px] opacity-60 ml-0.5">Mb</span></span></div>
                  </div>
                </div>
              `).join('') + 
              `</div>`;
          }
      }
      
      // 更新日期显示
      document.getElementById(`date-${scheduleId}`).textContent = date;
      
      // 更新流量徽章以匹配当前显示的日期
      const trafficBadge = document.getElementById(`traffic-badge-${scheduleId}`);
      if (trafficBadge && results.length > 0) {
        // Calculate total traffic for this day from results
        let totalBytes = 0;
        results.forEach(r => {
          const s = r.test_result?.summary;
          if (s) {
            // Add both upload and download traffic
            if (s.upload_bytes) totalBytes += s.upload_bytes;
            if (s.download_bytes) totalBytes += s.download_bytes;
            // Fallback to bytes_transferred
            if (!s.upload_bytes && !s.download_bytes && s.bytes) {
              totalBytes += s.bytes;
            }
          }
        });
        
        // Format traffic display
        const totalGB = totalBytes / (1024 * 1024 * 1024);
        const today = new Date().toISOString().split('T')[0];
        const dateLabel = (date === today) ? '今日' : date.substring(5); // MM-DD format
        trafficBadge.textContent = `${dateLabel}: ${totalGB.toFixed(2)}G`;
      } else if (trafficBadge) {
        trafficBadge.textContent = '--';
      }
    }

    // 切换日期
    function changeDate(scheduleId, offset) {
      const dateEl = document.getElementById(`date-${scheduleId}`);
      const currentDate = new Date(dateEl.textContent === '今天' ? new Date() : dateEl.textContent);
      currentDate.setDate(currentDate.getDate() + offset);
      const newDate = currentDate.toISOString().split('T')[0];
      loadChartData(scheduleId, newDate);
    }

    let currentTab = 'uni'; // 'uni' | 'bidir'

    function switchScheduleTab(tab) {
        currentTab = tab;
        const uniBtn = document.getElementById('tab-uni');
        const bidirBtn = document.getElementById('tab-bidir');
        const protoSelect = document.getElementById('schedule-protocol');
        const dirWrapper = document.getElementById('direction-wrapper');
        
        // Update Tabs
        if (tab === 'uni') {
            uniBtn.classList.replace('text-slate-400', 'text-sky-400');
            uniBtn.classList.replace('border-transparent', 'border-sky-400');
            bidirBtn.classList.replace('text-sky-400', 'text-slate-400');
            bidirBtn.classList.replace('border-sky-400', 'border-transparent');
            
            // Update Protocol Options (Keep selection if possible)
            const currentProto = protoSelect.value;
            protoSelect.innerHTML = '<option value="tcp">TCP</option><option value="udp">UDP</option>';
            if (['tcp', 'udp'].includes(currentProto)) protoSelect.value = currentProto;
            
            dirWrapper.classList.remove('hidden');
        } else {
            bidirBtn.classList.replace('text-slate-400', 'text-sky-400');
            bidirBtn.classList.replace('border-transparent', 'border-sky-400');
            uniBtn.classList.replace('text-sky-400', 'text-slate-400');
            uniBtn.classList.replace('border-sky-400', 'border-transparent');
            
            const currentProto = protoSelect.value;
            protoSelect.innerHTML = '<option value="tcp">TCP</option><option value="udp">UDP</option><option value="tcp_udp">TCP + UDP</option>';
            protoSelect.value = currentProto || 'tcp';
            
            dirWrapper.classList.add('hidden');
        }
        updateUdpBandwidthVisibility();
    }
    
    // Show/hide UDP bandwidth field based on protocol
    function updateUdpBandwidthVisibility() {
        const proto = document.getElementById('schedule-protocol').value;
        const wrapper = document.getElementById('udp-bandwidth-wrapper');
        if (proto === 'udp' || proto === 'tcp_udp') {
            wrapper.classList.remove('hidden');
        } else {
            wrapper.classList.add('hidden');
        }
    }

    // Set cron expression from preset button
    function setCron(expr) {
        document.getElementById('schedule-cron').value = expr;
    }

    // Translate cron expression to Chinese label
    function cronToChineseLabel(cron) {
        if (!cron) return '--';
        const parts = cron.trim().split(/\s+/);
        if (parts.length < 5) return cron;
        
        const [minute, hour, day, month, weekday] = parts;
        
        // 周几名称映射
        const weekdayNames = ['日', '一', '二', '三', '四', '五', '六'];
        
        // */N * * * * -> 每N分钟
        if (minute.startsWith('*/') && hour === '*' && day === '*' && month === '*' && weekday === '*') {
            const interval = parseInt(minute.substring(2));
            return `每${interval}分钟`;
        }
        // 0 */N * * * -> 每N小时
        if (minute === '0' && hour.startsWith('*/') && day === '*' && month === '*' && weekday === '*') {
            const interval = parseInt(hour.substring(2));
            return `每${interval}小时`;
        }
        // 0 0 * * * -> 每天0点
        if (minute === '0' && hour === '0' && day === '*' && month === '*' && weekday === '*') {
            return '每天0点';
        }
        // 0 H * * * -> 每天H点
        if (minute === '0' && /^\d+$/.test(hour) && day === '*' && month === '*' && weekday === '*') {
            return `每天${hour}点`;
        }
        // M H * * * -> 每天H:M
        if (/^\d+$/.test(minute) && /^\d+$/.test(hour) && day === '*' && month === '*' && weekday === '*') {
            return `每天${hour}:${minute.padStart(2, '0')}`;
        }
        // 0 H-H * * * -> 每天H-H点每小时
        if (minute === '0' && /^\d+-\d+$/.test(hour) && day === '*' && month === '*' && weekday === '*') {
            return `每天${hour}点每小时`;
        }
        // N * * * * -> 每小时第N分钟
        if (/^\d+$/.test(minute) && hour === '*' && day === '*' && month === '*' && weekday === '*') {
            return `每小时第${minute}分`;
        }
        // 0 0 * * N -> 每周N
        if (minute === '0' && hour === '0' && day === '*' && month === '*' && /^\d$/.test(weekday)) {
            const wd = parseInt(weekday);
            return `每周${weekdayNames[wd]}`;
        }
        // 0 H * * N -> 每周N H点
        if (minute === '0' && /^\d+$/.test(hour) && day === '*' && month === '*' && /^\d$/.test(weekday)) {
            const wd = parseInt(weekday);
            return `每周${weekdayNames[wd]}${hour}点`;
        }
        // 0 0 D * * -> 每月D日
        if (minute === '0' && hour === '0' && /^\d+$/.test(day) && month === '*' && weekday === '*') {
            return `每月${day}日`;
        }
        // 0 H D * * -> 每月D日H点
        if (minute === '0' && /^\d+$/.test(hour) && /^\d+$/.test(day) && month === '*' && weekday === '*') {
            return `每月${day}日${hour}点`;
        }
        // 0 0 1,15 * * -> 每月1和15日
        if (minute === '0' && hour === '0' && /^[\d,]+$/.test(day) && month === '*' && weekday === '*') {
            return `每月${day.replace(/,/g, '和')}日`;
        }
        
        return cron; // Fallback to raw expression
    }

    // Modal操作
    function openModal(scheduleId = null) {
      editingScheduleId = scheduleId;
      const modal = document.getElementById('schedule-modal');
      const title = document.getElementById('modal-title');
      
      // 获取需要控制的字段
      const nameInput = document.getElementById('schedule-name');
      const srcSelect = document.getElementById('schedule-src');
      const dstSelect = document.getElementById('schedule-dst');
      const protocolSelect = document.getElementById('schedule-protocol');
      const directionSelect = document.getElementById('schedule-direction');
      const uniTab = document.getElementById('uni-tab');
      const bidirTab = document.getElementById('bidir-tab');
      
      // Default reset
      switchScheduleTab('uni');
      document.getElementById('schedule-direction').value = 'upload';
      document.getElementById('schedule-protocol').value = 'tcp';
      
      // 重置字段启用状态
      const resetFieldState = (disabled) => {
        [nameInput, srcSelect, dstSelect, protocolSelect, directionSelect].forEach(el => {
          if (el) {
            el.disabled = disabled;
            el.classList.toggle('opacity-50', disabled);
            el.classList.toggle('cursor-not-allowed', disabled);
          }
        });
        [uniTab, bidirTab].forEach(el => {
          if (el) {
            el.disabled = disabled;
            el.classList.toggle('pointer-events-none', disabled);
            el.classList.toggle('opacity-50', disabled);
          }
        });
      };
      
      if (scheduleId) {
        const schedule = schedules.find(s => s.id === scheduleId);
        title.textContent = '编辑定时任务';
        title.innerHTML = '编辑定时任务 <span class="text-xs text-amber-400 font-normal ml-2">（仅可修改时长/并行数/间隔）</span>';
        document.getElementById('schedule-name').value = schedule.name;
        document.getElementById('schedule-src').value = schedule.src_node_id;
        document.getElementById('schedule-dst').value = schedule.dst_node_id;
        document.getElementById('schedule-duration').value = schedule.duration;
        document.getElementById('schedule-parallel').value = schedule.parallel;
        document.getElementById('schedule-cron').value = schedule.cron_expression || '*/10 * * * *';
        document.getElementById('schedule-notes').value = schedule.notes || '';
        
        // Restore Tab state
        const direction = schedule.direction || 'upload';
        if (direction === 'bidirectional') {
            switchScheduleTab('bidir');
        } else {
            switchScheduleTab('uni');
            document.getElementById('schedule-direction').value = direction;
        }
        // Restore protocol AFTER switching tab (so options exist)
        document.getElementById('schedule-protocol').value = schedule.protocol;
        document.getElementById('schedule-udp-bandwidth').value = schedule.udp_bandwidth || '';
        updateUdpBandwidthVisibility();
        
        // 编辑模式：禁用核心配置字段
        resetFieldState(true);
        
      } else {
        title.textContent = '新建定时任务';
        document.getElementById('schedule-name').value = '';
        document.getElementById('schedule-duration').value = 10;
        document.getElementById('schedule-parallel').value = 1;
        document.getElementById('schedule-cron').value = '*/10 * * * *';
        document.getElementById('schedule-notes').value = '';
        document.getElementById('schedule-udp-bandwidth').value = '';
        switchScheduleTab('uni');
        updateUdpBandwidthVisibility();
        
        // 新建模式：启用所有字段
        resetFieldState(false);
      }
      
      modal.classList.remove('hidden');
      modal.classList.add('flex');
    }

    function closeModal() {
      document.getElementById('schedule-modal').classList.add('hidden');
      editingScheduleId = null;
    }

    // 验证 cron 表达式格式
    function validateCronExpression(cron) {
      if (!cron || !cron.trim()) {
        return { valid: false, error: 'Cron 表达式不能为空' };
      }
      const parts = cron.trim().split(/\s+/);
      if (parts.length < 5 || parts.length > 6) {
        return { valid: false, error: 'Cron 表达式需要 5 个字段 (分 时 日 月 周)' };
      }
      // 简单验证每个字段
      const patterns = [
        /^(\*|(\*\/\d+)|(\d+(-\d+)?(,\d+(-\d+)?)*))$/, // 分钟
        /^(\*|(\*\/\d+)|(\d+(-\d+)?(,\d+(-\d+)?)*))$/, // 小时
        /^(\*|(\*\/\d+)|(\d+(-\d+)?(,\d+(-\d+)?)*))$/, // 日
        /^(\*|(\*\/\d+)|(\d+(-\d+)?(,\d+(-\d+)?)*))$/, // 月
        /^(\*|(\*\/\d+)|(\d+(-\d+)?(,\d+(-\d+)?)*))$/  // 周
      ];
      for (let i = 0; i < 5; i++) {
        if (!patterns[i].test(parts[i])) {
          const fieldNames = ['分钟', '小时', '日', '月', '周'];
          return { valid: false, error: `${fieldNames[i]}字段格式错误: ${parts[i]}` };
        }
      }
      return { valid: true };
    }

    // 显示 Toast 通知
    function showToast(message, type = 'error') {
      // 移除旧的 toast
      const oldToast = document.getElementById('toast-notification');
      if (oldToast) oldToast.remove();
      
      const toast = document.createElement('div');
      toast.id = 'toast-notification';
      toast.className = `fixed top-4 right-4 z-50 px-4 py-3 rounded-lg shadow-lg transition-all transform ${
        type === 'success' ? 'bg-green-600 text-white' : 
        type === 'warning' ? 'bg-yellow-600 text-white' : 
        'bg-red-600 text-white'
      }`;
      toast.innerHTML = `<div class="flex items-center gap-2">
        <span>${type === 'success' ? '✓' : type === 'warning' ? '⚠' : '✕'}</span>
        <span>${message}</span>
      </div>`;
      document.body.appendChild(toast);
      
      // 3秒后自动消失
      setTimeout(() => {
        toast.classList.add('opacity-0');
        setTimeout(() => toast.remove(), 300);
      }, 3000);
    }

    // Update ping badges with trend arrows for a node
    async function updateNodePingBadges(nodeId) {
      const container = document.getElementById(`vps-ping-${nodeId}`);
      if (!container) return;
      
      try {
        const res = await fetch(`/api/ping/history/${nodeId}`);
        const data = await res.json();
        
        if (data.status !== 'ok' || !data.trends) {
          container.innerHTML = '<span class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-slate-700/40 text-slate-400">暂无数据</span>';
          return;
        }
        
        const carriers = ['CU', 'CM', 'CT'];
        const carrierNames = { CU: '联通', CM: '移动', CT: '电信' };
        const carrierColors = { 
          CU: 'bg-red-500/20 border-red-500/30 text-red-300',
          CM: 'bg-blue-500/20 border-blue-500/30 text-blue-300',
          CT: 'bg-green-500/20 border-green-500/30 text-green-300'
        };
        
        let badgesHtml = '';
        carriers.forEach(carrier => {
          const trend = data.trends[carrier];
          const points = data.carriers?.[carrier] || [];
          const latestMs = points.length > 0 ? points[points.length - 1].ms : null;
          
          if (latestMs === null) return;
          
          const trendSymbol = trend?.symbol || '→';
          const trendColor = trend?.color || '#94a3b8';
          const color = carrierColors[carrier] || 'bg-slate-700/40 text-slate-300';
          
          // Generate mini sparkline SVG for hover
          let sparklineSvg = '';
          if (points.length >= 3) {
            const vals = points.slice(-30).map(p => p.ms);
            const minV = Math.min(...vals);
            const maxV = Math.max(...vals);
            const range = maxV - minV || 1;
            const sparkPoints = vals.map((v, i) => {
              const x = (i / (vals.length - 1)) * 80;
              const y = 20 - ((v - minV) / range) * 18;
              return `${x},${y}`;
            }).join(' ');
            sparklineSvg = `<svg class="w-20 h-5" viewBox="0 0 80 22"><polyline points="${sparkPoints}" fill="none" stroke="${trendColor}" stroke-width="1.5"/></svg>`;
          }
          
          badgesHtml += `
            <div class="relative group/ping">
              <span class="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-mono border ${color} cursor-help">
                <span style="color: ${trendColor}">${trendSymbol}</span>
                <span class="font-bold">${carrier}</span>
                <span>${latestMs}ms</span>
              </span>
              ${points.length >= 3 ? `
                <div class="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 hidden group-hover/ping:block z-50">
                  <div class="bg-slate-800 border border-slate-600 rounded-lg p-2 shadow-lg">
                    <div class="text-[9px] text-slate-400 mb-1 text-center">${carrierNames[carrier]} 24h趋势</div>
                    ${sparklineSvg}
                    <div class="text-[8px] text-center mt-1" style="color: ${trendColor}">${trend?.diff > 0 ? '+' : ''}${trend?.diff || 0}ms</div>
                  </div>
                </div>
              ` : ''}
            </div>
          `;
        });
        
        container.innerHTML = badgesHtml || '<span class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-slate-700/40 text-slate-400">暂无数据</span>';
      } catch (e) {
        container.innerHTML = '<span class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-slate-700/40 text-slate-400">获取失败</span>';
      }
    }

    // 保存定时任务
    async function saveSchedule() {
      // Determine direction based on tab
      let direction = 'upload';
      if (currentTab === 'bidir') {
          direction = 'bidirectional';
      } else {
          direction = document.getElementById('schedule-direction').value;
      }

      const cronValue = document.getElementById('schedule-cron').value.trim();
      
      // 验证 cron 表达式
      const cronValidation = validateCronExpression(cronValue);
      if (!cronValidation.valid) {
        showToast(cronValidation.error, 'error');
        document.getElementById('schedule-cron').focus();
        return;
      }

      const data = {
        name: document.getElementById('schedule-name').value,
        src_node_id: parseInt(document.getElementById('schedule-src').value),
        dst_node_id: parseInt(document.getElementById('schedule-dst').value),
        protocol: document.getElementById('schedule-protocol').value,
        duration: parseInt(document.getElementById('schedule-duration').value),
        parallel: parseInt(document.getElementById('schedule-parallel').value),
        port: 62001,
        cron_expression: cronValue,
        enabled: true,
        direction: direction,
        udp_bandwidth: document.getElementById('schedule-udp-bandwidth').value || null,
        notes: document.getElementById('schedule-notes').value || null,
      };
      
      try {
        if (editingScheduleId) {
          await apiFetch(`/schedules/${editingScheduleId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
          });
          showToast('任务更新成功', 'success');
        } else {
          await apiFetch('/schedules', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
          });
          showToast('任务创建成功', 'success');
        }
        
        closeModal();
        await loadSchedules();
      } catch (err) {
        showToast('保存失败: ' + err.message, 'error');
      }
    }

    // 切换启用/禁用 - 动态更新状态徽章，不刷新整页
    async function toggleSchedule(scheduleId) {
      const btn = document.getElementById(`btn-toggle-${scheduleId}`);
      const originalText = btn?.textContent;
      
      try {
        if (btn) {
          btn.disabled = true;
          btn.textContent = '处理中...';
        }
        
        const res = await apiFetch(`/schedules/${scheduleId}/toggle`, { method: 'POST' });
        const data = await res.json();
        
        // 更新本地数据
        const schedule = schedules.find(s => s.id === scheduleId);
        if (schedule) {
          schedule.enabled = data.enabled;
          schedule.next_run_at = data.next_run_at;
        }
        
        // 动态更新状态徽章
        updateScheduleCardStatus(scheduleId, data.enabled, data.next_run_at);
        
      } catch (err) {
        showToast('操作失败: ' + err.message, 'error');
        if (btn) btn.textContent = originalText;
      } finally {
        if (btn) btn.disabled = false;
      }
    }
    
    // 动态更新单个卡片的状态（不刷新整页）
    function updateScheduleCardStatus(scheduleId, enabled, nextRunAt) {
      const card = document.getElementById(`schedule-card-${scheduleId}`);
      if (!card) return;
      
      // 更新按钮文本
      const btn = document.getElementById(`btn-toggle-${scheduleId}`);
      if (btn) btn.textContent = enabled ? '暂停' : '启用';
      
      // 更新状态徽章
      const badgeHtml = enabled 
        ? '<span class="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-emerald-500/20 text-emerald-300 text-xs font-semibold"><span class="h-2 w-2 rounded-full bg-emerald-400"></span>运行中</span>'
        : '<span class="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-slate-700 text-slate-400 text-xs font-semibold"><span class="h-2 w-2 rounded-full bg-slate-500"></span>已暂停</span>';
      
      // 找到状态徽章并替换
      const existingBadge = card.querySelector('.inline-flex.items-center.gap-1.px-2.py-1.rounded-full');
      if (existingBadge) {
        existingBadge.outerHTML = badgeHtml;
      }
      
      // 更新倒计时元素
      const countdownEl = card.querySelector('[data-countdown]');
      if (countdownEl) {
        if (enabled && nextRunAt) {
          countdownEl.dataset.countdown = nextRunAt;
        } else {
          countdownEl.dataset.countdown = '';
          countdownEl.textContent = enabled ? 'Pending...' : '--';
        }
      }
    }

    // 编辑
    function editSchedule(scheduleId) {
      openModal(scheduleId);
    }

    // 删除
    async function deleteSchedule(scheduleId) {
      if (!confirm('确定要删除这个定时任务吗?')) return;
      await apiFetch(`/schedules/${scheduleId}`, { method: 'DELETE' });
      await loadSchedules();
    }

    // 立即执行
    // 立即执行
    async function runSchedule(scheduleId) {
      if (!confirm('确定要立即执行此任务吗?')) return;
      try {
        await apiFetch(`/schedules/${scheduleId}/execute`, { method: 'POST' });
        showToast('✅ 任务已触发，请稍后刷新查看结果', 'success');
      } catch (err) {
        showToast('执行失败: ' + err.message, 'error');
      }
    }

    // Toggle history panel visibility
    function toggleHistory(scheduleId) {
      const panel = document.getElementById(`history-panel-${scheduleId}`);
      if (panel) {
        panel.classList.toggle('hidden');
      }
    }

    function updateCountdowns() {
      const now = new Date();
      document.querySelectorAll('[data-countdown]').forEach(el => {
        const nextRun = el.dataset.countdown;
        if (!nextRun) {
          el.textContent = '';
          return;
        }
        
        // 解析 ISO 字符串，确保时区正确
        let target;
        try {
          let dateStr = nextRun.trim();
          
          // 如果字符串不以 Z 或 +/- 时区结尾，添加 Z 后缀确保 UTC 解析
          if (!dateStr.endsWith('Z') && !/[+-]\d{2}:\d{2}$/.test(dateStr)) {
            dateStr += 'Z';
          }
          
          target = new Date(dateStr);
          
          if (isNaN(target.getTime())) {
            el.textContent = '--';
            return;
          }
        } catch (e) {
          el.textContent = '--';
          return;
        }
        
        const diff = target - now;
        
        if (diff <= 0) {
          el.textContent = '运行中...';
          return;
        }
        
        // 格式化显示：根据时间长短使用不同格式
        if (diff < 60000) {
          // 小于1分钟
          const s = Math.floor(diff / 1000);
          el.textContent = `${s}秒后`;
        } else if (diff < 3600000) {
          // 小于1小时
          const m = Math.floor(diff / 60000);
          const s = Math.floor((diff % 60000) / 1000);
          el.textContent = `${m}分${s}秒后`;
        } else if (diff < 86400000) {
          // 小于24小时
          const h = Math.floor(diff / 3600000);
          const m = Math.floor((diff % 3600000) / 60000);
          el.textContent = `${h}小时${m}分后`;
        } else {
          // 超过24小时，显示日期
          el.textContent = target.toLocaleDateString('zh-CN', {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
        }
      });
      
      // 智能检测Running状态，主动轮询获取新数据
      document.querySelectorAll('[data-countdown]').forEach(el => {
        const scheduleId = el.dataset.scheduleId;
        if (!scheduleId) return;
        
        const isRunning = el.textContent === '运行中...';
        const lastPolled = parseInt(el.dataset.lastPolled || '0');
        const now = Date.now();
        
        // 在运行中状态时，每5秒轮询一次后端
        if (isRunning && (now - lastPolled > 5000)) {
          el.dataset.lastPolled = now.toString();
          
          // 异步获取新数据
          refreshSingleSchedule(parseInt(scheduleId)).then(updated => {
            if (updated) {
              loadChartData(parseInt(scheduleId));
            }
          });
        }
      });
    }
    
    // 静默刷新单个任务数据（避免页面抖动），返回是否有更新
    async function refreshSingleSchedule(scheduleId) {
      try {
        const res = await apiFetch(`/schedules/${scheduleId}`);
        const newData = await res.json();
        
        if (newData && newData.next_run_at) {
          // 更新本地数据
          const idx = schedules.findIndex(s => s.id === scheduleId);
          const oldNextRunAt = idx >= 0 ? schedules[idx].next_run_at : null;
          
          if (idx >= 0) {
            schedules[idx] = newData;
          }
          
          // 更新倒计时元素
          const countdownEl = document.querySelector(`[data-schedule-id="${scheduleId}"]`);
          if (countdownEl) {
            const hadUpdate = oldNextRunAt !== newData.next_run_at;
            countdownEl.dataset.countdown = newData.next_run_at;
            return hadUpdate; // 返回是否有更新
          }
        }
        return false;
      } catch (e) {
        console.error('Failed to refresh single schedule:', e);
        return false;
      }
    }

    // 事件绑定
    document.getElementById('create-schedule-btn').addEventListener('click', () => openModal());
    document.getElementById('close-modal').addEventListener('click', closeModal);
    document.getElementById('cancel-modal').addEventListener('click', closeModal);
    document.getElementById('save-schedule').addEventListener('click', saveSchedule);
    document.getElementById('refresh-btn').addEventListener('click', loadSchedules);

    // 更新定时任务卡片的流量徽章
    async function updateScheduleTrafficBadges() {
      try {
        // 1. Fetch Global Stats for VPS Summary
        const res = await fetch('/api/daily_traffic_stats');
        const data = await res.json();
        
        if (data.status === 'ok') {
            const vpsContainer = document.getElementById('vps-daily-summary');
            if (vpsContainer) {
                vpsContainer.innerHTML = data.nodes.map((n, idx) => {
                    // 智能格式化流量显示
                    let displayTraffic = n.total_gb + 'G';
                    if (n.total_bytes > 0 && n.total_gb < 0.01) {
                        const mb = (n.total_bytes / (1024 * 1024)).toFixed(1);
                        displayTraffic = mb + 'M';
                    }
                    
                    // Check online status
                    const isOnline = n.status === 'online';
                    
                    // Color config - use grayed out for offline nodes
                    const gradients = isOnline ? [
                        'from-sky-500/20 to-blue-600/10',
                        'from-emerald-500/20 to-teal-600/10',
                        'from-purple-500/20 to-pink-600/10',
                        'from-amber-500/20 to-orange-600/10'
                    ] : ['from-slate-600/20 to-slate-700/10'];
                    
                    const borderColors = isOnline 
                        ? ['border-sky-500/30', 'border-emerald-500/30', 'border-purple-500/30', 'border-amber-500/30']
                        : ['border-slate-600/30'];
                    const textColors = isOnline 
                        ? ['text-sky-400', 'text-emerald-400', 'text-purple-400', 'text-amber-400']
                        : ['text-slate-500'];
                    
                    const gradient = gradients[idx % gradients.length];
                    const borderColor = borderColors[idx % borderColors.length];
                    const textColor = textColors[idx % textColors.length];
                    
                    // Status indicator - Green for online, Red for offline
                    const statusDot = isOnline 
                        ? '<span class="w-2 h-2 rounded-full bg-emerald-400 shadow shadow-emerald-400/50 animate-pulse"></span>'
                        : '<span class="w-2 h-2 rounded-full bg-rose-500"></span>';
                    
                    // Card opacity for offline
                    const cardOpacity = isOnline ? '' : 'opacity-60';
                    
                    return `
                      <div class="relative overflow-hidden rounded-2xl bg-gradient-to-br ${gradient} border ${borderColor} p-4 transition-all hover:scale-[1.02] hover:shadow-lg group ${cardOpacity}">
                        <div class="absolute top-0 right-0 w-20 h-20 bg-gradient-to-br from-white/5 to-transparent rounded-full -translate-y-6 translate-x-6"></div>
                        
                        <!-- Header: Flag + Status + Name -->
                        <div class="flex items-center gap-2 mb-3">
                          ${statusDot}
                          <span id="vps-flag-${n.node_id}" class="text-xs font-bold text-slate-400 bg-slate-700/50 px-1.5 py-0.5 rounded">--</span>
                          <h4 class="text-sm font-bold ${isOnline ? 'text-white' : 'text-slate-400'} truncate flex-1">${n.name}</h4>
                          <span id="vps-tasks-${n.node_id}" class="px-2 py-0.5 rounded-full bg-slate-700/50 text-xs text-slate-400 font-bold" title="运行中的任务数">0 任务</span>
                        </div>
                        
                        <!-- Traffic Display -->
                        <div class="flex items-end justify-between">
                          <div class="space-y-1">
                            <div class="text-3xl font-bold ${textColor} drop-shadow-sm">${displayTraffic}</div>
                            <div class="text-[10px] text-slate-500 uppercase tracking-wider">今日流量</div>
                          </div>
                          <div class="text-right space-y-1 opacity-0 group-hover:opacity-100 transition-opacity">
                            <div class="text-xs text-slate-400 font-mono truncate max-w-[120px]" title="${maskAddress(n.ip, true)}">${maskAddress(n.ip, true)}</div>
                            <div id="vps-isp-${n.node_id}" class="text-[10px] text-slate-500 truncate max-w-[120px]"></div>
                          </div>
                        </div>
                        
                        <!-- ISP Ping Badges with Trends -->
                        <div class="flex flex-wrap gap-1.5 mt-2 pt-2 border-t border-slate-700/30">
                          ${(n.backbone_latency && n.backbone_latency.length > 0) ? 
                            n.backbone_latency.map(lat => {
                              const key = lat.key?.toUpperCase() || 'N/A';
                              const colorMap = {
                                'CU': 'bg-red-500/20 border-red-500/40 text-red-300',
                                'CM': 'bg-blue-500/20 border-blue-500/40 text-blue-300',
                                'CT': 'bg-green-500/20 border-green-500/40 text-green-300'
                              };
                              const color = colorMap[key] || 'bg-slate-700/40 border-slate-600/40 text-slate-300';
                              const trendSymbol = lat.trend_symbol || '→';
                              const trendColor = lat.trend_color || '#94a3b8';
                              const trendDiff = lat.trend_diff;
                              const diffText = (trendDiff === undefined || trendDiff === null) ? '采集中...' : (trendDiff === 0 ? '稳定' : (trendDiff > 0 ? `+${trendDiff}ms` : `${trendDiff}ms`));
                              return `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono border ${color} cursor-help" title="趋势: ${diffText}">
                                <span style="color: ${trendColor}">${trendSymbol}</span>
                                <span class="font-bold">${key}</span>
                                <span>${lat.ms}ms</span>
                              </span>`;
                            }).join('') 
                            : '<span class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-slate-700/40 text-slate-500">暂无延迟数据</span>'
                          }
                        </div>
                      </div>
                    `;
                }).join('');

                // Fetch ISPs and flags for these nodes
                data.nodes.forEach(n => {
                    fetch(`/geo?ip=${n.ip}`)
                      .then(r => r.json())
                      .then(d => {
                          // Update ISP
                          const ispEl = document.getElementById(`vps-isp-${n.node_id}`);
                          if (ispEl && d.isp) {
                              ispEl.textContent = d.isp;
                              ispEl.title = d.isp;
                          }
                          
                          // Update flag with text country code (Windows compatible)
                          const flagEl = document.getElementById(`vps-flag-${n.node_id}`);
                          if (flagEl && d.country_code) {
                              flagEl.textContent = d.country_code.toUpperCase();
                              flagEl.title = d.country_code;
                          }
                      })
                      .catch(() => void 0);
                });

                // Update task counts for each node
                if (window.schedulesData) {
                    const taskCounts = {};
                    window.schedulesData.forEach(s => {
                        if (s.enabled) {
                            if (s.src_node_id) taskCounts[s.src_node_id] = (taskCounts[s.src_node_id] || 0) + 1;
                            if (s.dst_node_id) taskCounts[s.dst_node_id] = (taskCounts[s.dst_node_id] || 0) + 1;
                        }
                    });
                    
                    data.nodes.forEach(n => {
                        const tasksEl = document.getElementById(`vps-tasks-${n.node_id}`);
                        if (tasksEl) {
                            const count = taskCounts[n.node_id] || 0;
                            tasksEl.textContent = count > 0 ? `${count} 任务` : '无任务';
                            if (count > 0) {
                                tasksEl.classList.remove('bg-slate-700/50', 'text-slate-400');
                                tasksEl.classList.add('bg-sky-500/20', 'text-sky-400');
                            }
                        }
                    });
                }
            }
        }

        // 2. Fetch Schedule-specific traffic stats and update badges
        // Use chart date if available, otherwise today
        const chartDate = window.chartCurrentDate || new Date().toISOString().slice(0, 10);
        const isToday = chartDate === new Date().toISOString().slice(0, 10);
        const dateLabel = isToday ? '今日' : chartDate.slice(5); // MM-DD format
        
        const scheduleStatsRes = await fetch(`/api/daily_schedule_traffic_stats?date=${chartDate}`);
        const scheduleStats = await scheduleStatsRes.json();
        
        if (scheduleStats.status === 'ok') {
            const stats = scheduleStats.stats || {};
            
            schedules.forEach(schedule => {
                const badgeEl = document.getElementById(`traffic-badge-${schedule.id}`);
                if (badgeEl) {
                    const bytes = stats[schedule.id] || 0;
                    if (bytes > 0) {
                        // Format bytes to appropriate unit
                        let displayValue;
                        if (bytes >= 1024 * 1024 * 1024) {
                            displayValue = (bytes / (1024 * 1024 * 1024)).toFixed(2) + 'G';
                        } else if (bytes >= 1024 * 1024) {
                            displayValue = (bytes / (1024 * 1024)).toFixed(1) + 'M';
                        } else if (bytes >= 1024) {
                            displayValue = (bytes / 1024).toFixed(0) + 'K';
                        } else {
                            displayValue = bytes + 'B';
                        }
                        badgeEl.textContent = `${dateLabel}: ${displayValue}`;
                        badgeEl.style.display = 'inline-block';
                    } else {
                        badgeEl.textContent = `${dateLabel}: 0`;
                        badgeEl.style.display = 'inline-block';
                    }
                }
            });
        }
        
      } catch (err) {
        console.error('Failed to update traffic stats:', err);
      }
    }


    // Share schedule chart as screenshot
    async function shareSchedule(scheduleId) {
      const cardEl = document.getElementById(`schedule-card-${scheduleId}`);
      if (!cardEl) {
        alert('找不到卡片元素');
        return;
      }
      
      try {
        // Show loading indicator
        const btn = event?.target;
        const originalText = btn?.innerHTML;
        if (btn) btn.innerHTML = '⏳ 生成中...';
        
        // Use html2canvas to capture the card
        const canvas = await html2canvas(cardEl, {
          backgroundColor: '#0f172a',
          scale: 2,  // Higher quality
          logging: false,
          useCORS: true,
          allowTaint: true
        });
        
        // Convert to blob and download
        canvas.toBlob(function(blob) {
          const schedule = schedules.find(s => s.id === scheduleId);
          const fileName = `${schedule?.name || 'schedule'}_${new Date().toISOString().slice(0,10)}.png`;
          
          // Create download link
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = fileName;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          URL.revokeObjectURL(url);
          
          if (btn) btn.innerHTML = originalText;
          
          // Also try to copy to clipboard if supported
          if (navigator.clipboard && navigator.clipboard.write) {
            navigator.clipboard.write([new ClipboardItem({'image/png': blob})]).then(() => {
              showToast(`✅ 图片已保存并复制到剪贴板: ${fileName}`, 'success');
            }).catch(() => {
              showToast(`✅ 图片已保存: ${fileName}`, 'success');
            });
          } else {
            showToast(`✅ 图片已保存: ${fileName}`, 'success');
          }
        }, 'image/png', 1.0);
        
      } catch (err) {
        console.error('Screenshot failed:', err);
        showToast('截图失败: ' + err.message, 'error');
      }
    }


    // Share schedule info as Markdown
    async function shareScheduleMarkdown(scheduleId) {
      const schedule = schedules.find(s => s.id === scheduleId);
      if (!schedule) {
        showToast('找不到任务信息', 'error');
        return;
      }
      
      const srcNode = nodes.find(n => n.id === schedule.src_node_id);
      const dstNode = nodes.find(n => n.id === schedule.dst_node_id);
      
      // Get latest result data
      let latestStats = '';
      try {
        const res = await apiFetch(`/test_results?schedule_id=${scheduleId}&limit=1`);
        const results = await res.json();
        if (results && results.length > 0) {
          const r = results[0];
          latestStats = `
| 指标 | 数值 |
|------|------|
| 上传速率 | ${r.upload_mbps ? r.upload_mbps.toFixed(2) + ' Mbps' : '--'} |
| 下载速率 | ${r.download_mbps ? r.download_mbps.toFixed(2) + ' Mbps' : '--'} |
| 执行时间 | ${new Date(r.created_at).toLocaleString()} |`;
        }
      } catch (e) { console.error(e); }
      
      const markdown = `## 📊 ${schedule.name}

**测试配置**
- 源节点: ${srcNode?.name || 'Unknown'} (${srcNode?.ip || '--'})
- 目标节点: ${dstNode?.name || 'Unknown'} (${dstNode?.ip || '--'})
- 协议: ${schedule.protocol?.toUpperCase() || 'TCP'}
- 时长: ${schedule.duration || 10} 秒
- 周期: ${schedule.cron_expression || '每' + Math.floor((schedule.interval_seconds || 600) / 60) + '分钟'}
- 状态: ${schedule.enabled ? '✅ 运行中' : '⏸️ 已暂停'}
${latestStats}

---
*Generated at ${new Date().toLocaleString()}*`;

      try {
        await navigator.clipboard.writeText(markdown);
        showToast('✅ Markdown 信息已复制到剪贴板', 'success');
      } catch (e) {
        // Fallback for older browsers
        const textarea = document.createElement('textarea');
        textarea.value = markdown;
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
        showToast('✅ Markdown 信息已复制到剪贴板', 'success');
      }
    }

    // 初始化
    (async () => {
      // Check guest status first
      try {
        const authRes = await apiFetch('/auth/status');
        const authData = await authRes.json();
        window.isGuest = authData.isGuest === true;
        
        if (window.isGuest) {
          // Hide create schedule button for guests
          document.getElementById('create-schedule-btn')?.classList.add('hidden');
        }
      } catch (e) {
        console.error('Auth check failed:', e);
      }
      
      await loadNodes();
      await loadSchedules();
      await updateScheduleTrafficBadges();
      
      // 每5分钟更新一次流量统计
      setInterval(updateScheduleTrafficBadges, 5 * 60 * 1000);
    })();

window.loadNodes = loadNodes;
window.loadSchedules = loadSchedules;
window.updateNodeSelects = updateNodeSelects;
window.openModal = openModal;
window.closeModal = closeModal;
window.switchScheduleTab = switchScheduleTab;
window.updateUdpBandwidthVisibility = updateUdpBandwidthVisibility;
window.setCron = setCron;
window.showToast = showToast;
window.saveSchedule = saveSchedule;
window.toggleSchedule = toggleSchedule;
window.editSchedule = editSchedule;
window.deleteSchedule = deleteSchedule;
window.runSchedule = runSchedule;
window.toggleHistory = toggleHistory;
window.shareSchedule = shareSchedule;
window.shareScheduleMarkdown = shareScheduleMarkdown;
