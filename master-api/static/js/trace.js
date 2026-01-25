    import { apiFetch } from './api.js';
    let nodes = [];
    window.isGuest = false;
    
    // Guest mode initialization - hide operation tabs/panels for guests
    (async function initGuestMode() {
      try {
        const res = await apiFetch('/auth/status');
        const data = await res.json();
        window.isGuest = data.isGuest === true;
        
        if (window.isGuest) {
          // Hide operation panels for guests (tabs already hidden by CSS)
          document.getElementById('panel-single')?.classList.add('hidden');
          document.getElementById('panel-schedules')?.classList.add('hidden');
          document.getElementById('panel-multisrc')?.classList.add('hidden');
          // Auto-switch to history tab
          switchTab('history');
        } else {
          // Show tabs for authenticated users
          document.body.classList.add('authenticated');
          handleHashChange(); // Sync initial tab with hash
        }
      } catch (e) {
        console.error('Guest mode check failed:', e);
        // Default to show for error case (fail-open for usability)
        document.body.classList.add('authenticated');
      }
    })();

    // ASN Tier cache - populated from API
    const _asnTierCache = {};
    
    // Fetch ASN tier from API (async)
    async function fetchAsnTier(asn) {
      if (!asn || _asnTierCache[asn]) return _asnTierCache[asn];
      try {
        const res = await fetch(`/api/asn/${asn}`);
        const data = await res.json();
        if (data.status === 'ok' && data.tier) {
          _asnTierCache[asn] = { tier: data.tier, name: data.name };
          return _asnTierCache[asn];
        }
      } catch (e) { console.log('ASN lookup failed:', asn); }
      return null;
    }
    
    // Pre-fetch ASN tiers for all hops (batched)
    async function prefetchAsnTiers(hops) {
      const asns = [...new Set(hops.filter(h => h.geo?.asn).map(h => h.geo.asn))];
      await Promise.all(asns.map(a => fetchAsnTier(a)));
    }
    
    const ISP_RULES = [
      // China Telecom (电信) - CN2 must be checked before 163
      { match: /cn2|ctgnet|next\s*carr|next\s*gen|as4809/i, asn: [4809], badge: 'cn2', label: 'CN2' },
      { match: /chinanet|china\s*telecom(?!.*next)|ct\.net|163data|no\.31|as4134/i, asn: [4134, 4812], badge: '163', label: '163' },
      // China Unicom (联通) - 9929/10099 must be checked before 4837
      { match: /9929|as9929|unicom.*premium|cuii|cu\s*vip/i, asn: [9929], badge: '9929', label: '9929' },
      { match: /10099|as10099|unicom.*global|cu.*international/i, asn: [10099], badge: '9929', label: '10099' },
      { match: /4837|as4837|169.*backbone|chinaunicom|cncgroup|china\s*unicom(?!.*(9929|premium|global|international))/i, asn: [4837, 17621, 17622], badge: '4837', label: '4837' },
      // China Mobile (移动) - CMIN2 must be checked before CMI
      { match: /cmin2|as58807|mobile.*international.*2/i, asn: [58807], badge: 'cmin2', label: 'CMIN2' },
      { match: /cmi(?!n2)|as58453|china\s*mobile.*international(?!.*2)/i, asn: [58453], badge: 'cmi', label: 'CMI' },
      { match: /chinamobile|cmnet|china\s*mobile(?!.*international)/i, asn: [9808, 56040, 56041, 56042, 56044, 56046, 56047, 56048], badge: 'cmi', label: 'CM' },
      // ============ Tier 1 - Transit-Free Global Backbones ============
      { match: /ntt.*comm|ntt\s*com|ntt\s*america|ntt\s*global/i, asn: [2914], badge: 'ntt', label: 'T1:NTT' },
      { match: /telia|arelion/i, asn: [1299], badge: 'telia', label: 'T1:Telia' },
      { match: /cogent/i, asn: [174], badge: 'cogent', label: 'T1:Cogent' },
      { match: /lumen|level\s*3|centurylink/i, asn: [3356, 3549], badge: 'lumen', label: 'T1:Lumen' },
      { match: /gtt(?!\s*express)/i, asn: [3257], badge: 'gtt', label: 'T1:GTT' },
      { match: /zayo/i, asn: [6461], badge: 'zayo', label: 'T1:Zayo' },
      { match: /hurricane|he\.net/i, asn: [6939], badge: 'he', label: 'T1:HE' },
      { match: /telecom\s*italia\s*sparkle|seabone/i, asn: [6762], badge: 'telia', label: 'T1:TI-S' },
      { match: /liberty\s*global/i, asn: [6830], badge: 'cogent', label: 'T1:LG' },
      // ============ Japan ============
      { match: /softbank|bbtec|yahoo\s*bb/i, asn: [17676, 9143], badge: 'softbank', label: 'SoftBank' },
      { match: /kddi/i, asn: [2516, 2519], badge: 'kddi', label: 'KDDI' },
      { match: /iij|internet\s*initiative/i, asn: [2497], badge: 'iij', label: 'IIJ' },
      { match: /ntt\s*docomo/i, asn: [9605], badge: 'ntt', label: 'Docomo' },
      { match: /ocn/i, asn: [4713], badge: 'ntt', label: 'OCN' },
      // ============ Hong Kong / Asia Pacific ============
      { match: /pccw/i, asn: [3491], badge: 'pccw', label: 'PCCW' },
      { match: /hkt/i, asn: [4515, 9304], badge: 'hkt', label: 'HKT' },
      { match: /hgc|hutchison/i, asn: [10103], badge: 'pccw', label: 'HGC' },
      { match: /telstra/i, asn: [1221, 4637], badge: 'telstra', label: 'Telstra' },
      { match: /singtel/i, asn: [7473, 7474], badge: 'singtel', label: 'Singtel' },
      { match: /starhub/i, asn: [4657], badge: 'singtel', label: 'StarHub' },
      // ============ Korea ============
      { match: /korea\s*telecom|kt\s*corp/i, asn: [4766], badge: 'kddi', label: 'KT' },
      { match: /sk\s*broadband/i, asn: [9318], badge: 'kddi', label: 'SKB' },
      { match: /lg\s*uplus/i, asn: [3786], badge: 'kddi', label: 'LGU+' },
      // ============ Taiwan ============
      { match: /chunghwa|cht/i, asn: [3462], badge: 'pccw', label: 'CHT' },
      { match: /taiwanmobile|twm/i, asn: [9924], badge: 'pccw', label: 'TWM' },
      // ============ Europe ============
      { match: /deutsche\s*telekom|dtag/i, asn: [3320], badge: 'telia', label: 'DTAG' },
      { match: /orange/i, asn: [5511], badge: 'cogent', label: 'Orange' },
      { match: /vodafone/i, asn: [1273, 3209], badge: 'cogent', label: 'Vodafone' },
      { match: /british\s*telecom|bt\s*group/i, asn: [5400], badge: 'telia', label: 'BT' },
      { match: /swisscom/i, asn: [3303], badge: 'telia', label: 'Swisscom' },
      // ============ US Regional ============
      { match: /comcast/i, asn: [7922], badge: 'lumen', label: 'Comcast' },
      { match: /verizon/i, asn: [701, 703], badge: 'lumen', label: 'Verizon' },
      { match: /att|at&t/i, asn: [7018], badge: 'lumen', label: 'AT&T' },
      { match: /charter|spectrum/i, asn: [20115], badge: 'lumen', label: 'Charter' },
      // ============ Russia ============
      { match: /rostelecom/i, asn: [12389], badge: 'telia', label: 'Rostele' },
      // ============ Internet Exchanges ============
      { match: /bbix/i, asn: [23764, 23640], badge: 'bbix', label: 'IX:BBIX' },
      { match: /jpix/i, asn: [7527], badge: 'bbix', label: 'IX:JPIX' },
      { match: /equinix/i, asn: [24115], badge: 'equinix', label: 'IX:Equinix' },
      { match: /de-cix/i, asn: [6695], badge: 'equinix', label: 'IX:DE-CIX' },
      { match: /ams-ix/i, asn: [1200], badge: 'equinix', label: 'IX:AMS-IX' },
      { match: /linx/i, asn: [5459], badge: 'equinix', label: 'IX:LINX' },
      { match: /hkix/i, asn: [4635], badge: 'equinix', label: 'IX:HKIX' },
      { match: /jinx/i, asn: [37662], badge: 'jinx', label: 'IX:JINX' },
      // ============ CDN / Cloud ============
      { match: /cloudflare/i, asn: [13335], badge: 'cogent', label: 'CDN:CF' },
      { match: /akamai/i, asn: [20940, 16625], badge: 'cogent', label: 'CDN:Akamai' },
      { match: /fastly/i, asn: [54113], badge: 'cogent', label: 'CDN:Fastly' },
      { match: /google/i, asn: [15169, 396982], badge: 'ntt', label: 'Google' },
      { match: /amazon|aws/i, asn: [16509, 14618], badge: 'ntt', label: 'AWS' },
      { match: /microsoft|azure/i, asn: [8075], badge: 'ntt', label: 'Azure' },
      { match: /alibaba|aliyun/i, asn: [45102], badge: 'pccw', label: 'Aliyun' },
      { match: /tencent/i, asn: [132203], badge: 'pccw', label: 'Tencent' },
      // ============ VPS / Hosting ============
      { match: /vultr|choopa/i, asn: [20473], badge: 'he', label: 'Vultr' },
      { match: /digitalocean/i, asn: [14061], badge: 'he', label: 'DO' },
      { match: /linode/i, asn: [63949], badge: 'he', label: 'Linode' },
      { match: /ovh/i, asn: [16276], badge: 'he', label: 'OVH' },
      { match: /hetzner/i, asn: [24940], badge: 'he', label: 'Hetzner' },
      { match: /scaleway/i, asn: [12876], badge: 'he', label: 'Scaleway' },
      { match: /oracle.*cloud/i, asn: [31898], badge: 'ntt', label: 'Oracle' },
      { match: /sakura/i, asn: [7684], badge: 'iij', label: 'Sakura' },
      { match: /conoha|gmo/i, asn: [7506], badge: 'iij', label: 'ConoHa' },
    ];

    function detectIspBadge(isp, asn) {
      // First check dynamic cache from API
      if (asn && _asnTierCache[asn]) {
        const cached = _asnTierCache[asn];
        // Map tier to badge style
        const tierBadgeMap = {
          'T1': 'ntt', 'T2': 'pccw', 'T3': 'iij', 'IX': 'jinx', 'CDN': 'cogent', 'ISP': 'he'
        };
        const badge = tierBadgeMap[cached.tier] || 'he';
        return { badge, label: cached.tier };
      }
      
      // Fallback to hardcoded rules
      if (!isp) return null;
      for (const rule of ISP_RULES) {
        if (rule.match.test(isp) || (asn && rule.asn.includes(asn))) return { badge: rule.badge, label: rule.label };
      }
      return null;
    }

    function renderBadge(b) { return b ? `<span class="badge badge-${b.badge}">${b.label}</span>` : ''; }
    
    // Latency heatmap color helper
    function getLatencyClass(rtt) {
      if (!rtt || rtt <= 0) return '';
      if (rtt < 20) return 'latency-excellent';
      if (rtt < 50) return 'latency-good';
      if (rtt < 100) return 'latency-fair';
      if (rtt < 200) return 'latency-slow';
      return 'latency-bad';
    }
    
    function getLatencyColor(rtt) {
      if (!rtt || rtt <= 0) return '#64748b';  // gray for unknown
      if (rtt < 20) return '#22c55e';  // green
      if (rtt < 50) return '#84cc16';  // lime
      if (rtt < 100) return '#eab308'; // yellow
      if (rtt < 200) return '#f97316'; // orange
      return '#ef4444';                 // red
    }
    
    // Render latency capsule badge with color
    function renderLatencyCapsule(rtt) {
      if (!rtt || rtt <= 0) return '<span class="latency-capsule" style="color:#64748b">-</span>';
      const capsuleClass = rtt < 50 ? 'excellent' : rtt < 150 ? 'good' : 'bad';
      return `<span class="latency-capsule ${capsuleClass}">${rtt.toFixed(0)}ms</span>`;
    }
    
    // Chart instances
    let fwdChart = null, revChart = null;
    
    function renderLatencyChart(canvasId, hops, label) {
      const canvas = document.getElementById(canvasId);
      if (!canvas) return;
      
      const ctx = canvas.getContext('2d');
      const labels = hops.map((h, i) => `#${i + 1}`);
      const data = hops.map(h => h.rtt_avg || 0);
      const colors = hops.map(h => getLatencyColor(h.rtt_avg));
      
      // Destroy existing chart if any
      if (canvasId === 'fwd-latency-chart' && fwdChart) { fwdChart.destroy(); }
      if (canvasId === 'rev-latency-chart' && revChart) { revChart.destroy(); }
      
      const chart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [{
            label: label,
            data: data,
            backgroundColor: colors,
            borderColor: colors.map(c => c.replace(')', ', 0.8)').replace('rgb', 'rgba')),
            borderWidth: 1,
            borderRadius: 4,
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                title: (items) => hops[items[0].dataIndex]?.ip || 'Unknown',
                label: (item) => `延迟: ${item.raw.toFixed(1)}ms`
              }
            }
          },
          scales: {
            y: { 
              beginAtZero: true,
              title: { display: true, text: 'ms', color: '#94a3b8' },
              ticks: { color: '#94a3b8' },
              grid: { color: 'rgba(51, 65, 85, 0.3)' }
            },
            x: { 
              ticks: { color: '#94a3b8' },
              grid: { display: false }
            }
          }
        }
      });
      
      if (canvasId === 'fwd-latency-chart') fwdChart = chart;
      if (canvasId === 'rev-latency-chart') revChart = chart;
    }
    
    // Extract AS path from hops (group consecutive hops by ASN)
    function extractAsPath(hops) {
      const path = [];
      let currentAs = null;
      let foundFirstPublic = false;  // Flag to skip leading local/hidden hops
      
      // Helper to extract ASN from ISP string (format: "AS1234 Company Name" or just "AS1234")
      function parseAsnFromIsp(isp) {
        if (!isp) return null;
        const match = isp.match(/^AS(\d+)/i);
        return match ? parseInt(match[1]) : null;
      }
      
      // Check if IP is private/local network
      function isLocalNetwork(ip) {
        if (!ip || ip === '*') return false;
        return /^(10\.|172\.(1[6-9]|2[0-9]|3[0-1])\.|192\.168\.|127\.|169\.254\.)/.test(ip);
      }
      
      for (const hop of hops) {
        const isp = hop.geo?.isp || '';
        const ip = hop.ip || '';
        
        // Try to get ASN from geo.asn first, then parse from ISP string
        let asn = hop.geo?.asn;
        if (!asn) asn = parseAsnFromIsp(isp);
        
        // Skip leading local network and hidden hops entirely
        if (!foundFirstPublic) {
          if (isLocalNetwork(ip) || ip === '*' || !asn) {
            continue;  // Skip until we find first public AS
          }
          foundFirstPublic = true;
        }
        
        // After first public AS, handle local network in middle of path
        if (isLocalNetwork(ip)) {
          // Just skip local network IPs in the middle - they don't add value
          continue;
        }
        
        // Hidden hops (* - 100%) - count them toward current AS
        if (ip === '*' || !asn) {
          if (currentAs) {
            currentAs.hopCount++;
          }
          continue;
        }
        
        if (currentAs && currentAs.asn === asn) {
          // Same AS, increment hop count
          currentAs.hopCount++;
          currentAs.lastHop = hop;
          if (hop.rtt_avg) currentAs.totalLatency += hop.rtt_avg;
        } else {
          // New AS
          if (currentAs) path.push(currentAs);
          
          // Determine tier from cache or ISP name
          let tier = 'ISP';
          const cached = _asnTierCache[asn];
          if (cached?.tier) {
            tier = cached.tier;
          } else {
            // Fallback tier detection
            const ispLower = isp.toLowerCase();
            if (/ntt|telia|cogent|lumen|gtt|zayo|hurricane/.test(ispLower)) tier = 'T1';
            else if (/pccw|hkt|kddi|softbank|singtel|telstra/.test(ispLower)) tier = 'T2';
            else if (/equinix|bbix|ix|exchange/.test(ispLower)) tier = 'IX';
          }
          
          currentAs = {
            asn: asn,
            name: isp,  // Full ISP name, no truncation
            tier: tier,
            hopCount: 1,
            firstHop: hop,
            lastHop: hop,
            totalLatency: hop.rtt_avg || 0
          };
        }
      }
      
      if (currentAs) path.push(currentAs);
      return path;
    }
    
    // Render AS path as breadcrumb flow diagram
    function renderAsPath(containerId, hops) {
      const container = document.getElementById(containerId);
      if (!container) return;
      
      const asPath = extractAsPath(hops);
      console.log('[AS Path Debug] hops count:', hops.length, 'asPath count:', asPath.length);
      if (hops.length > 0) console.log('[AS Path Debug] First hop geo:', JSON.stringify(hops[0]?.geo));
      
      if (asPath.length === 0) {
        container.innerHTML = '<span class="text-slate-500 text-xs">无 AS 信息</span>';
        return;
      }
      
      // Tier border colors
      const tierColors = {
        't1': '#f97316', 't2': '#22c55e', 't3': '#3b82f6', 
        'ix': '#a855f7', 'isp': '#64748b'
      };
      
      const html = '<div class="as-breadcrumb">' + asPath.map((as, i) => {
        const tierClass = `as-tier-${as.tier.toLowerCase()}`;
        const avgLatency = as.hopCount > 0 ? (as.totalLatency / as.hopCount).toFixed(0) : 0;
        const borderColor = tierColors[as.tier.toLowerCase()] || '#64748b';
        
        const crumb = `
          <div class="as-crumb" style="--tier-color: ${borderColor};" title="${as.name}\\n${as.hopCount}跳 | 平均${avgLatency}ms">
            <div class="flex items-center gap-2 mb-1">
              <span class="as-tier ${tierClass}">${as.tier}</span>
              <span class="as-asn text-xs">AS${as.asn}</span>
            </div>
            <div class="as-name text-xs truncate max-w-[120px]">${as.name}</div>
            <div class="as-hops">${as.hopCount}跳</div>
          </div>
        `;
        
        const arrow = i < asPath.length - 1 ? '<span class="as-crumb-arrow">→</span>' : '';
        return crumb + arrow;
      }).join('') + '</div>';
      
      container.innerHTML = html;
    }

    function extractRouteBadges(hops) {
      const seen = new Set(), result = [];
      for (const hop of hops) {
        const geo = hop.geo || {}, b = detectIspBadge(geo.isp, geo.asn);
        if (b && !seen.has(b.label)) { seen.add(b.label); result.push(b); }
      }
      return result;
    }

    function switchTab(tab) {
      ['single', 'schedules', 'multisrc', 'history'].forEach(t => {
        const panel = document.getElementById(`panel-${t}`);
        if (panel) panel.classList.toggle('hidden', t !== tab);
      });
      
      // Update Sidebar Active State
      document.querySelectorAll('.sidebar .nav-item').forEach(el => el.classList.remove('active'));
      let pageId = 'trace';
      if (tab === 'schedules') pageId = 'trace-schedules';
      if (tab === 'multisrc') pageId = 'compare';
      if (tab === 'history') pageId = 'history';
      const navItem = document.querySelector(`.sidebar .nav-item[data-page="${pageId}"]`);
      if (navItem) navItem.classList.add('active');

      if (tab === 'schedules') loadSchedules();
      if (tab === 'multisrc') loadMultisrcNodes();
      if (tab === 'history') loadHistory();
    }

    function handleHashChange() {
      const hash = window.location.hash;
      if (hash === '#schedules') switchTab('schedules');
      else if (hash === '#compare') switchTab('multisrc');
      else if (hash === '#history') switchTab('history');
      else switchTab('single');
    }
    window.addEventListener('hashchange', handleHashChange);
    
    function loadMultisrcNodes() {
      const container = document.getElementById('multisrc-nodes');
      const targetSel = document.getElementById('multisrc-target-node');
      if (!nodes.length) {
        container.innerHTML = '<p class="text-slate-500 text-xs col-span-2">加载中...</p>';
        return;
      }
      container.innerHTML = nodes.map(n => `
        <label class="flex items-center gap-2 p-2 rounded-lg hover:bg-slate-800/50 cursor-pointer text-sm" data-node-id="${n.id}">
          <input type="checkbox" class="multisrc-node-cb rounded border-slate-600 bg-slate-700 text-cyan-500" value="${n.id}" data-name="${n.name}" data-ip="${n.ip}">
          <span>${n.name}</span>
        </label>
      `).join('');
      // Populate target node dropdown
      targetSel.innerHTML = '<option value="">选择目标节点...</option>' + nodes.map(n => 
        `<option value="${n.id}" data-ip="${n.ip}" data-name="${n.name}">${n.name} (${n.ip})</option>`
      ).join('');
    }
    
    async function loadHistory() {
      const list = document.getElementById('history-list');
      list.innerHTML = '<p class="text-slate-500 text-sm">加载中...</p>';
      
      try {
        const res = await apiFetch('/api/trace/results?limit=100');
        const data = await res.json();
        
        if (!res.ok) {
          list.innerHTML = '<p class="text-rose-400 text-sm">加载失败: ' + (data.detail || '未知错误') + '</p>';
          return;
        }
        
        if (!data.length) {
          list.innerHTML = '<p class="text-slate-500 text-sm">暂无记录</p>';
          return;
        }
        
        // Group by source-target pair
        const groups = {};
        data.forEach(r => {
          const srcNode = nodes.find(n => n.id === r.src_node_id);
          const srcName = srcNode ? srcNode.name : `节点#${r.src_node_id}`;
          const key = `${srcName} → ${r.target}`;
          if (!groups[key]) groups[key] = [];
          groups[key].push(r);
        });
        
        // Source type icons
        const sourceIcons = { 'scheduled': '📅', 'single': '🚀', 'multisrc': '🌐' };
        const sourceNames = { 'scheduled': '定时', 'single': '单次', 'multisrc': '多源' };
        
        // Extract ISP badges for route change summary
        function extractIspBadges(hops) {
          const badges = [];
          const seen = new Set();
          for (const hop of (hops || [])) {
            const geo = hop.geo || {};
            const b = detectIspBadge(geo.isp, geo.asn);
            if (b && !seen.has(b.label)) {
              seen.add(b.label);
              badges.push(b);
            }
          }
          return badges;
        }
        
        // Generate ISP transition text for route changes
        function getIspTransition(oldHops, newHops) {
          const oldBadges = extractIspBadges(oldHops);
          const newBadges = extractIspBadges(newHops);
          
          if (oldBadges.length === 0 && newBadges.length === 0) return '';
          
          const formatBadges = (badges) => badges.slice(0, 3).map(b => 
            `<span class="px-1.5 py-0.5 rounded text-xs font-bold ${b.colorClass || 'bg-slate-600'}">${b.label}</span>`
          ).join('');
          
          if (oldBadges.length === 0) return formatBadges(newBadges);
          if (newBadges.length === 0) return formatBadges(oldBadges);
          
          // Check if there's an actual difference
          const oldLabels = oldBadges.map(b => b.label).sort().join(',');
          const newLabels = newBadges.map(b => b.label).sort().join(',');
          if (oldLabels === newLabels) return formatBadges(newBadges);
          
          return formatBadges(oldBadges) + ' <span class="text-amber-400">→</span> ' + formatBadges(newBadges);
        }
        
        // Render grouped
        list.innerHTML = Object.entries(groups).map(([route, records]) => {
          const hasChanges = records.some(r => r.has_change);
          const routeIcon = hasChanges ? '⚠️' : '✅';
          const headerClass = hasChanges ? 'text-amber-400' : 'text-emerald-400';
          
          // Get first changed record to extract ISP transition
          const changedRecord = records.find(r => r.has_change && r.change_summary);
          let ispTransition = '';
          if (changedRecord) {
            // Look for the previous record to compare ISPs
            const idx = records.indexOf(changedRecord);
            const prevRecord = idx + 1 < records.length ? records[idx + 1] : null;
            if (prevRecord) {
              ispTransition = getIspTransition(prevRecord.hops, changedRecord.hops);
            }
          }
          
          const recordsHtml = records.slice(0, 10).map((r, idx) => {
            const time = new Date(r.executed_at).toLocaleString('zh-CN');
            const changeIcon = r.has_change ? '⚠️' : '✅';
            const changeBg = r.has_change ? 'bg-amber-500/10' : 'bg-slate-800/30';
            const srcIcon = sourceIcons[r.source_type] || '📅';
            const srcName = sourceNames[r.source_type] || '定时';
            const recordId = `history-record-${r.id}`;
            
            // Get changed positions from summary for highlighting
            const changedPositions = r.change_summary?.changed_positions || [];
            
            // Generate hop preview (first and last IP)
            const hops = r.hops || [];
            const hopPreview = hops.length > 0 
              ? `${hops[0]?.ip || '-'} → ${hops[hops.length-1]?.ip || '-'}`
              : '';
            
            // Generate hop detail with changed rows highlighted in red
            const hopDetailsHtml = hops.map((h, i) => {
              const isChanged = changedPositions.includes(i);
              const rowClass = isChanged ? 'text-rose-400 bg-rose-500/10 hop-node hop-timeout' : 'hop-node';
              const hopNumClass = isChanged ? 'text-rose-300' : 'text-cyan-400';
              const ipClass = isChanged ? 'text-rose-200 font-bold' : 'text-slate-300';
              const latencyHtml = renderLatencyCapsule(h.rtt_avg);
              
              return `<div class="flex items-center gap-3 p-2 ${rowClass}"><span class="${hopNumClass} text-xs font-mono">#${i+1}</span><span class="${ipClass} font-mono text-xs">${h.ip || '*'}</span>${latencyHtml}<span class="text-slate-500 text-xs truncate max-w-[150px]">${h.geo?.isp || ''}</span></div>`;
            }).join('');
            
            return `
              <div class="cursor-pointer hover:bg-slate-700/30 ${changeBg} rounded" onclick="toggleHistoryDetail('${recordId}')">
                <div class="flex items-center justify-between p-2 text-xs">
                  <div class="flex items-center gap-2">
                    <span title="${srcName}">${srcIcon}</span>
                    <span>${changeIcon}</span>
                    <span class="text-slate-300">${r.total_hops}跳</span>
                    <span class="text-slate-500">${r.tool_used}</span>
                    <span class="text-slate-500">${r.elapsed_ms}ms</span>
                  </div>
                  <span class="text-slate-400">${time}</span>
                </div>
                <div id="${recordId}" class="hidden px-4 pb-2 text-xs text-slate-400">
                  <div class="mb-1">${hopPreview}</div>
                  <div class="bg-slate-900/50 rounded p-2 font-mono">
                    ${hopDetailsHtml}
                  </div>
                </div>
              </div>
            `;
          }).join('');
          
          return `
            <div class="mb-4">
              <div class="flex items-center gap-2 mb-2 font-semibold ${headerClass}">
                <span>${routeIcon}</span>
                <span>${route}</span>
                <span class="text-xs text-slate-500 font-normal">(${records.length}条记录)</span>
                ${ispTransition ? `<span class="ml-2 flex items-center gap-1">${ispTransition}</span>` : ''}
              </div>
              <div class="space-y-1 pl-4 border-l-2 border-slate-700">
                ${recordsHtml}
                ${records.length > 10 ? `<div class="text-xs text-slate-500 p-2">...还有 ${records.length - 10} 条记录</div>` : ''}
              </div>
            </div>
          `;
        }).join('');
        
      } catch (e) {
        list.innerHTML = '<p class="text-rose-400 text-sm">加载失败: ' + e.message + '</p>';
      }
    }
    
    function toggleHistoryDetail(id) {
      const el = document.getElementById(id);
      if (el) el.classList.toggle('hidden');
    }
    
    function toggleMultisrcTarget() {
      const isNode = document.getElementById('multisrc-target-type').value === 'node';
      document.getElementById('multisrc-target-node').classList.toggle('hidden', !isNode);
      document.getElementById('multisrc-target').classList.toggle('hidden', isNode);
      if (isNode) updateMultisrcNodeDisabled();
      else {
        // Re-enable all checkboxes when custom target is selected
        document.querySelectorAll('.multisrc-node-cb').forEach(cb => {
          cb.disabled = false;
          cb.closest('label').classList.remove('opacity-50', 'cursor-not-allowed');
        });
      }
    }
    
    function updateMultisrcNodeDisabled() {
      const targetNodeId = document.getElementById('multisrc-target-node').value;
      document.querySelectorAll('.multisrc-node-cb').forEach(cb => {
        const isTarget = cb.value === targetNodeId;
        cb.disabled = isTarget;
        cb.checked = isTarget ? false : cb.checked;  // Uncheck if it becomes disabled
        cb.closest('label').classList.toggle('opacity-50', isTarget);
        cb.closest('label').classList.toggle('cursor-not-allowed', isTarget);
      });
    }
    
    async function runMultiSourceTrace() {
      const checkboxes = document.querySelectorAll('.multisrc-node-cb:checked');
      const targetType = document.getElementById('multisrc-target-type').value;
      const targetNodeSel = document.getElementById('multisrc-target-node');
      const targetInput = document.getElementById('multisrc-target');
      const status = document.getElementById('multisrc-status');
      const results = document.getElementById('multisrc-results');
      const btn = document.getElementById('multisrc-start-btn');
      
      // Get target based on type
      let target, targetName;
      if (targetType === 'node') {
        const opt = targetNodeSel.selectedOptions[0];
        if (!targetNodeSel.value) { alert('请选择目标节点'); return; }
        target = opt.dataset.ip;
        targetName = opt.dataset.name;
      } else {
        target = targetInput.value.trim();
        targetName = target;
      }
      
      if (checkboxes.length < 1) { alert('请至少选择 1 个源节点'); return; }
      if (!target) { alert('请输入目标地址'); return; }
      
      btn.disabled = true;
      btn.textContent = '⏳ 追踪中...';
      status.textContent = `正在从 ${checkboxes.length} 个节点追踪到 ${targetName}...`;
      results.classList.add('hidden');
      results.innerHTML = '';
      
      const selectedNodes = Array.from(checkboxes).map(cb => ({
        id: cb.value,
        name: cb.dataset.name,
        ip: cb.dataset.ip
      }));
      
      try {
        // Run traces in parallel
        const tracePromises = selectedNodes.map(async (node) => {
          try {
            const res = await apiFetch(`/api/trace/run?node_id=${node.id}&save_result=true&source_type=multisrc`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ target, max_hops: 30, include_geo: true })
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Failed');
            return { node, success: true, data };
          } catch (e) {
            return { node, success: false, error: e.message };
          }
        });
        
        const traceResults = await Promise.all(tracePromises);
        
        // Prefetch ASN data
        const allHops = traceResults.filter(r => r.success).flatMap(r => r.data.hops);
        await prefetchAsnTiers(allHops);
        
        // Render results
        results.innerHTML = traceResults.map(r => {
          if (!r.success) {
            return `<div class="glass-card rounded-xl p-4 border-l-4 border-rose-500">
              <div class="font-semibold text-rose-400">${r.node.name}</div>
              <div class="text-sm text-slate-500">❌ ${r.error}</div>
            </div>`;
          }
          
          const asPath = extractAsPath(r.data.hops);
          
          // Generate AS path cards with same style as single trace
          const tierColors = {
            't1': '#f97316', 't2': '#22c55e', 't3': '#3b82f6', 
            'ix': '#a855f7', 'isp': '#64748b'
          };
          
          const asCardsHtml = asPath.length > 0 ? '<div class="as-path-container mt-3">' + asPath.map((as, i) => {
            const tierClass = `as-tier-${as.tier.toLowerCase()}`;
            const cardWidth = 100 + (as.hopCount * 25);
            const borderColor = tierColors[as.tier.toLowerCase()] || '#64748b';
            
            const card = `
              <div class="as-node" style="min-width: ${cardWidth}px; --tier-color: ${borderColor};">
                <div class="as-header">
                  <span class="as-tier ${tierClass}">${as.tier}</span>
                  <span class="as-asn">AS${as.asn}</span>
                </div>
                <div class="as-name">${as.name}</div>
                <div class="as-hops">${as.hopCount}跳</div>
              </div>
            `;
            const arrow = i < asPath.length - 1 ? '<span class="as-arrow">→</span>' : '';
            return card + arrow;
          }).join('') + '</div>' : '<div class="text-slate-500 text-xs mt-2">无 AS 信息</div>';
          
          return `<div class="glass-card rounded-xl p-4 fade-in border-l-4 border-cyan-500">
            <div class="flex items-center justify-between mb-2">
              <div class="font-semibold text-cyan-400 text-lg">${r.node.name}</div>
              <div class="text-sm text-slate-400">${r.data.total_hops}跳 | ${r.data.elapsed_ms}ms</div>
            </div>
            <div class="text-xs text-slate-500 mb-1">首跳: ${r.data.hops[0]?.ip || '-'} → 末跳: ${r.data.hops[r.data.hops.length-1]?.ip || '-'}</div>
            ${asCardsHtml}
          </div>`;
        }).join('');
        
        results.classList.remove('hidden');
        status.textContent = `✅ ${traceResults.filter(r => r.success).length}/${traceResults.length} 个节点追踪完成`;
        
      } catch (e) {
        status.textContent = `❌ ${e.message}`;
      } finally {
        btn.disabled = false;
        btn.textContent = '🚀 开始多源追踪';
      }
    }

    async function shareAsImage() {
      const btn = document.getElementById('share-btn');
      const originalText = btn.innerHTML;
      btn.innerHTML = '<span>⏳</span><span>处理中...</span>';
      btn.disabled = true;
      
      try {
        const container = document.getElementById('trace-results');
        
        // Hide share button temporarily
        btn.style.display = 'none';
        
        // Store original text content and mask IPs temporarily
        const textNodes = [];
        const originalTexts = [];
        const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, null, false);
        while (walker.nextNode()) {
          textNodes.push(walker.currentNode);
          originalTexts.push(walker.currentNode.textContent);
        }
        
        // Mask all IPs (xxx.xxx.xxx.xxx -> xxx.xxx.**.** )
        textNodes.forEach(node => {
          node.textContent = node.textContent.replace(
            /(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})/g,
            '$1.$2.**.**'
          );
        });
        
        // Small delay for DOM to update
        await new Promise(r => setTimeout(r, 100));
        
        // Capture using dom-to-image (better CSS support than html2canvas)
        const blob = await domtoimage.toBlob(container, {
          bgcolor: '#1e293b',
          quality: 1,
          style: {
            transform: 'scale(1)',
            transformOrigin: 'top left'
          }
        });
        
        // Restore original text
        textNodes.forEach((node, i) => {
          node.textContent = originalTexts[i];
        });
        
        // Show share button again
        btn.style.display = '';
        
        // Copy to clipboard
        try {
          await navigator.clipboard.write([
            new ClipboardItem({ 'image/png': blob })
          ]);
          btn.innerHTML = '<span>✅</span><span>已复制!</span>';
          setTimeout(() => { btn.innerHTML = originalText; btn.disabled = false; }, 2000);
        } catch (e) {
          console.error('Clipboard write failed:', e);
          // Fallback: download as file
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = 'traceroute_' + new Date().toISOString().slice(0,10) + '.png';
          a.click();
          URL.revokeObjectURL(url);
          btn.innerHTML = '<span>📥</span><span>已下载</span>';
          setTimeout(() => { btn.innerHTML = originalText; btn.disabled = false; }, 2000);
        }
        
      } catch (e) {
        console.error('Share as image failed:', e);
        btn.style.display = '';
        btn.innerHTML = '<span>❌</span><span>失败</span>';
        setTimeout(() => { btn.innerHTML = originalText; btn.disabled = false; }, 2000);
      }
    }

    async function shareSingleAsImage() {
      const btn = document.getElementById('share-single-btn');
      const originalText = btn.innerHTML;
      btn.innerHTML = '<span>⏳</span><span>处理中...</span>';
      btn.disabled = true;
      
      try {
        const container = document.getElementById('single-result');
        
        // Hide share button temporarily
        btn.style.display = 'none';
        
        // Store original text content and mask IPs temporarily
        const textNodes = [];
        const originalTexts = [];
        const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, null, false);
        while (walker.nextNode()) {
          textNodes.push(walker.currentNode);
          originalTexts.push(walker.currentNode.textContent);
        }
        
        // Mask all IPs (xxx.xxx.xxx.xxx -> xxx.xxx.**.** )
        textNodes.forEach(node => {
          node.textContent = node.textContent.replace(
            /(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})/g,
            '$1.$2.**.**'
          );
        });
        
        // Small delay for DOM to update
        await new Promise(r => setTimeout(r, 100));
        
        // Capture using dom-to-image (better CSS support than html2canvas)
        const blob = await domtoimage.toBlob(container, {
          bgcolor: '#1e293b',
          quality: 1,
          style: {
            transform: 'scale(1)',
            transformOrigin: 'top left'
          }
        });
        
        // Restore original text
        textNodes.forEach((node, i) => {
          node.textContent = originalTexts[i];
        });
        
        // Show share button again
        btn.style.display = '';
        
        // Copy to clipboard
        try {
          await navigator.clipboard.write([
            new ClipboardItem({ 'image/png': blob })
          ]);
          btn.innerHTML = '<span>✅</span><span>已复制!</span>';
          setTimeout(() => { btn.innerHTML = originalText; btn.disabled = false; }, 2000);
        } catch (e) {
          console.error('Clipboard write failed:', e);
          // Fallback: download as file
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = 'traceroute_single_' + new Date().toISOString().slice(0,10) + '.png';
          a.click();
          URL.revokeObjectURL(url);
          btn.innerHTML = '<span>📥</span><span>已下载</span>';
          setTimeout(() => { btn.innerHTML = originalText; btn.disabled = false; }, 2000);
        }
        
      } catch (e) {
        console.error('Share single as image failed:', e);
        btn.style.display = '';
        btn.innerHTML = '<span>❌</span><span>失败</span>';
        setTimeout(() => { btn.innerHTML = originalText; btn.disabled = false; }, 2000);
      }
    }


    async function loadNodes() {
      try {
        const res = await apiFetch('/nodes');
        nodes = await res.json();
        ['trace-src-node', 'trace-target-node'].forEach(id => {
          const sel = document.getElementById(id);
          sel.innerHTML = '<option value="">选择...</option>';
          nodes.forEach(n => { const opt = new Option(`${n.name} (${n.ip})`, n.id); opt.dataset.ip = n.ip; opt.dataset.name = n.name; sel.appendChild(opt); });
        });
        // Schedule modal dropdowns
        const schedSrc = document.getElementById('sched-src');
        schedSrc.innerHTML = '';
        nodes.forEach(n => schedSrc.appendChild(new Option(n.name, n.id)));
        const schedTargetNode = document.getElementById('sched-target-node');
        schedTargetNode.innerHTML = '<option value="">选择目标节点...</option>';
        nodes.forEach(n => { const opt = new Option(`${n.name} (${n.ip})`, n.id); opt.dataset.ip = n.ip; schedTargetNode.appendChild(opt); });
      } catch (e) { console.error('Load nodes failed:', e); }
    }

    function toggleTargetInput() {
      const isNode = document.getElementById('trace-target-type').value === 'node';
      document.getElementById('trace-target-node').classList.toggle('hidden', !isNode);
      document.getElementById('trace-target-input').classList.toggle('hidden', isNode);
    }

    function toggleSchedTarget() {
      const isNode = document.getElementById('sched-target-type').value === 'node';
      document.getElementById('sched-target-node-row').classList.toggle('hidden', !isNode);
      document.getElementById('sched-target-input-row').classList.toggle('hidden', isNode);
    }

    function renderFlag(code) { return code ? `<img src="/flags/${code}" alt="${code}" class="inline-block w-4 h-3 rounded-sm">` : ''; }

    function renderHopCell(hop, hopNum) {
      if (!hop) return '<div class="hop-empty">-</div>';
      const geo = hop.geo || {}, badge = detectIspBadge(geo.isp, geo.asn), flag = renderFlag(geo.country_code);
      const latencyCapsule = renderLatencyCapsule(hop.rtt_avg);
      const loss = hop.loss_pct > 0 ? `<span class="text-rose-400 text-xs ml-1">${hop.loss_pct}%</span>` : '';
      const isp = geo.isp || '';
      const isTimeout = hop.ip === '*';
      const isPrivate = isPrivateIP(hop.ip);
      const nodeClass = isTimeout ? 'hop-node hop-timeout' : isPrivate ? 'hop-node hop-private' : 'hop-node';
      const dotClass = isTimeout ? 'hop-dot dot-timeout' : isPrivate ? 'hop-dot dot-private' : 'hop-dot';
      return `<div class="${nodeClass}">
        <div class="flex items-center gap-2 mb-1">
          <span class="${dotClass}"></span>
          ${renderBadge(badge)}
          <span class="font-mono text-xs ${isTimeout ? 'text-slate-500' : ''}">${hop.ip}</span>
          ${latencyCapsule}${loss}
        </div>
        <div class="text-xs text-slate-500 flex items-center gap-1 truncate ml-4">${flag} ${isp}</div>
      </div>`;
    }

    function renderComparisonTable(fwdHops, revHops, srcIp, srcName, dstIp, dstName) {
      // Reverse the return route for proper alignment (B→A becomes A←B direction)
      const revHopsReversed = [...revHops].reverse();
      
      // Forward route: srcIp → hops → dstIp
      // Check if srcIp/dstIp already exist in MTR data to avoid duplication
      const srcIpExistsInFwd = fwdHops.some(h => h.ip === srcIp);
      const dstIpExistsInFwd = fwdHops.some(h => h.ip === dstIp);
      
      const fwdComplete = [];
      
      // Only prepend srcIp if it doesn't exist in MTR data
      if (!srcIpExistsInFwd) {
        fwdComplete.push({ip: srcIp, geo: {isp: srcName}, isEndpoint: true, endType: 'start'});
      }
      
      // Add all hops, marking srcIp as start endpoint if found
      fwdHops.forEach(h => {
        const copy = {...h};
        if (h.ip === srcIp) {
          copy.isEndpoint = true;
          copy.endType = 'start';
        }
        fwdComplete.push(copy);
      });
      
      // Only append dstIp if it doesn't exist in MTR data
      if (!dstIpExistsInFwd) {
        fwdComplete.push({ip: dstIp, geo: {isp: dstName}, isEndpoint: true, endType: 'end'});
      } else {
        // Mark existing dstIp hop as end endpoint
        for (let i = fwdComplete.length - 1; i >= 0; i--) {
          if (fwdComplete[i].ip === dstIp) {
            fwdComplete[i].isEndpoint = true;
            fwdComplete[i].endType = 'end';
            break;
          }
        }
      }
      
      // Reverse route after reversal: should show srcIp → ... → dstIp
      // Original trace was dstIp → hops → srcIp (MTR doesn't include dstIp)
      // After reversal: [..., srcIp or near srcIp]
      const revComplete = [];
      
      // Check if srcIp exists ANYWHERE in reversed hops (not just first)
      // MTR may have reached srcIp as a hop, so we shouldn't duplicate it
      const srcIpExistsInRev = revHopsReversed.some(h => h.ip === srcIp);
      
      // Only prepend srcIp if it doesn't exist in the data
      if (!srcIpExistsInRev) {
        revComplete.push({ip: srcIp, geo: {isp: srcName}, isEndpoint: true, endType: 'start'});
      }
      
      // Add all reversed hops
      revHopsReversed.forEach((h, i) => {
        const copy = {...h};
        // Mark hop as start endpoint if it's srcIp
        if (h.ip === srcIp) {
          copy.isEndpoint = true;
          copy.endType = 'start';
        }
        revComplete.push(copy);
      });
      
      // Check if dstIp exists ANYWHERE in reversed hops
      const dstIpExistsInRev = revHopsReversed.some(h => h.ip === dstIp);
      
      // Only append dstIp if it doesn't exist in the data
      if (!dstIpExistsInRev) {
        revComplete.push({ip: dstIp, geo: {isp: dstName}, isEndpoint: true, endType: 'end'});
      } else {
        // Mark the existing dstIp hop as end endpoint
        for (let i = revComplete.length - 1; i >= 0; i--) {
          if (revComplete[i].ip === dstIp) {
            revComplete[i].isEndpoint = true;
            revComplete[i].endType = 'end';
            break;
          }
        }
      }
      
      // Get hop key for matching - Tier 1/2/3 and ISP classification
      function getHopKey(hop) {
        if (!hop || hop.ip === '*') return null;
        
        // CRITICAL: For endpoint hops (start/end), use IP to ensure alignment
        // This prevents misalignment when same IP has different ISP info in forward vs reverse
        if (hop.isEndpoint) return 'ENDPOINT:' + hop.ip;
        
        const isp = (hop.geo?.isp || '').toLowerCase();
        const asn = hop.geo?.asn || '';
        
        // Tier 1 - Global backbone carriers
        if (/ntt|as2914/i.test(isp + asn)) return 'T1:NTT';
        if (/lumen|level\s*3|centurylink|as3356|as3549/i.test(isp + asn)) return 'T1:Lumen';
        if (/cogent|as174/i.test(isp + asn)) return 'T1:Cogent';
        if (/telia|as1299/i.test(isp + asn)) return 'T1:Telia';
        if (/gtt|cyberverse|as3257/i.test(isp + asn)) return 'T1:GTT';
        if (/zayo|as6461/i.test(isp + asn)) return 'T1:Zayo';
        if (/hurricane|he\.net|as6939/i.test(isp + asn)) return 'T1:HE';
        if (/arelion/i.test(isp)) return 'T1:Arelion';
        
        // Tier 2 - Regional/Transit carriers
        if (/pccw|as3491/i.test(isp + asn)) return 'T2:PCCW';
        if (/kddi|as2516/i.test(isp + asn)) return 'T2:KDDI';
        if (/softbank|as17676/i.test(isp + asn)) return 'T2:SoftBank';
        if (/iij|as2497/i.test(isp + asn)) return 'T2:IIJ';
        if (/telstra|as1221/i.test(isp + asn)) return 'T2:Telstra';
        if (/singtel|as7473/i.test(isp + asn)) return 'T2:Singtel';
        if (/hkt|as4515/i.test(isp + asn)) return 'T2:HKT';
        
        // IX/Peering points
        if (/bbix|as23640/i.test(isp + asn)) return 'IX:BBIX';
        if (/jinx|as37662/i.test(isp + asn)) return 'IX:JINX';
        if (/equinix|as24115/i.test(isp + asn)) return 'IX:Equinix';
        if (/de-cix/i.test(isp)) return 'IX:DE-CIX';
        if (/ams-ix/i.test(isp)) return 'IX:AMS-IX';
        if (/linx/i.test(isp)) return 'IX:LINX';
        
        // China carriers - must match ISP_RULES order
        // China Telecom
        if (/cn2|ctgnet|next\s*carr|as4809/i.test(isp + asn)) return 'CN:CN2';
        if (/chinanet|china\s*telecom|163data|as4134/i.test(isp + asn)) return 'CN:163';
        // China Unicom  
        if (/9929|as9929|unicom.*premium|cuii/i.test(isp + asn)) return 'CN:9929';
        if (/10099|as10099|unicom.*global/i.test(isp + asn)) return 'CN:10099';
        if (/4837|as4837|chinaunicom|cncgroup/i.test(isp + asn)) return 'CN:4837';
        // China Mobile
        if (/cmin2|as58807/i.test(isp + asn)) return 'CN:CMIN2';
        if (/cmi|as58453|mobile.*international/i.test(isp + asn)) return 'CN:CMI';
        if (/chinamobile|cmnet|as9808/i.test(isp + asn)) return 'CN:CM';
        
        // Cloud/CDN providers
        if (/cloudflare|as13335/i.test(isp + asn)) return 'CDN:CF';
        if (/akamai|as20940/i.test(isp + asn)) return 'CDN:Akamai';
        if (/google|as15169/i.test(isp + asn)) return 'CDN:Google';
        if (/amazon|aws|as16509/i.test(isp + asn)) return 'CDN:AWS';
        if (/microsoft|azure|as8075/i.test(isp + asn)) return 'CDN:Azure';
        
        // Regional ISPs (match by name keywords)
        if (/sakura/i.test(isp)) return 'ISP:Sakura';
        if (/linode|akamai connected/i.test(isp)) return 'ISP:Linode';
        if (/vultr|choopa/i.test(isp)) return 'ISP:Vultr';
        if (/digitalocean/i.test(isp)) return 'ISP:DO';
        if (/ovh/i.test(isp)) return 'ISP:OVH';
        if (/hetzner/i.test(isp)) return 'ISP:Hetzner';
        if (/fdcservers/i.test(isp)) return 'ISP:FDC';
        if (/conus|vpg/i.test(isp)) return 'ISP:Conus';
        if (/prime\s*security/i.test(isp)) return 'ISP:PrimeSec';
        
        // Local network
        if (/local\s*network|private|internal/i.test(isp)) return 'LOCAL';
        
        return hop.ip; // Fall back to IP for matching
      }
      
      // ISP-based LCS alignment
      const fwdKeys = fwdComplete.map(h => getHopKey(h));
      const revKeys = revComplete.map(h => getHopKey(h));
      const aligned = alignByLCS(fwdKeys, revKeys);
      
      const rows = [];
      let rowNum = 0;
      
      for (const [fIdx, rIdx] of aligned) {
        const fwd = fIdx >= 0 ? fwdComplete[fIdx] : null;
        const rev = rIdx >= 0 ? revComplete[rIdx] : null;
        
        // Determine row style using endType field
        let rowClass = '';
        const isStartRow = (fwd?.endType === 'start' || rev?.endType === 'start');
        const isEndRow = (fwd?.endType === 'end' || rev?.endType === 'end');
        
        if (fwd && rev && fwd.ip === rev.ip && fwd.ip !== '*') {
          rowClass = 'same-row';
        } else if (fwd && rev && getHopKey(fwd) === getHopKey(rev) && getHopKey(fwd)) {
          rowClass = 'same-row';  // Same ISP = green highlight
        }
        
        const rowLabel = isStartRow ? '起' : (isEndRow ? '终' : rowNum);
        const rowStyle = (isStartRow || isEndRow) ? 'style="background: rgba(6, 182, 212, 0.1) !important; border-left: 3px solid #06b6d4;"' : '';
        
        rows.push(`<div class="comp-row ${rowClass}" ${rowStyle}><div class="text-cyan-400 font-mono font-bold text-center">${rowLabel}</div>${renderHopCell(fwd)}<div class="text-slate-600 text-center">⇄</div>${renderHopCell(rev)}</div>`);
        rowNum++;
      }
      
      // Add symmetry info
      const commonKeys = new Set(fwdKeys.filter(k => k && revKeys.includes(k)));
      const commonCount = commonKeys.size;
      const totalUnique = new Set([...fwdKeys, ...revKeys].filter(k => k)).size;
      const symmetryPct = totalUnique > 0 ? Math.round(commonCount / totalUnique * 100) : 100;
      
      if (symmetryPct < 30) {
        rows.push(`<div class="p-3 bg-amber-500/10 border-t border-amber-500/30 text-amber-400 text-sm">⚠️ 路由不对称：去回程仅 ${commonCount} 个公共节点 (${symmetryPct}%)</div>`);
      } else if (commonCount > 1) {
        rows.push(`<div class="p-3 bg-slate-800/50 border-t border-slate-700 text-slate-400 text-xs">✓ ${commonCount} 个公共节点 | 对称度 ${symmetryPct}%</div>`);
      }
      
      return rows.join('');
    }
    
    function alignByLCS(fwdKeys, revKeys) {
      const m = fwdKeys.length, n = revKeys.length;
      const dp = Array.from({length: m+1}, () => Array(n+1).fill(0));
      
      for (let i = 1; i <= m; i++) {
        for (let j = 1; j <= n; j++) {
          if (fwdKeys[i-1] && fwdKeys[i-1] === revKeys[j-1]) {
            dp[i][j] = dp[i-1][j-1] + 1;
          } else {
            dp[i][j] = Math.max(dp[i-1][j], dp[i][j-1]);
          }
        }
      }
      
      // Backtrack
      const result = [];
      let i = m, j = n;
      while (i > 0 || j > 0) {
        if (i > 0 && j > 0 && fwdKeys[i-1] && fwdKeys[i-1] === revKeys[j-1]) {
          result.unshift([i-1, j-1]);
          i--; j--;
        } else if (j > 0 && (i === 0 || dp[i][j-1] >= dp[i-1][j])) {
          result.unshift([-1, j-1]);
          j--;
        } else {
          result.unshift([i-1, -1]);
          i--;
        }
      }
      return result;
    }

    
    function isPrivateIP(ip) {
      if (!ip || ip === '*') return false;
      return ip.startsWith('10.') || ip.startsWith('192.168.') || 
             ip.startsWith('172.16.') || ip.startsWith('172.17.') || ip.startsWith('172.18.') ||
             ip.startsWith('172.19.') || ip.startsWith('172.2') || ip.startsWith('172.30.') || ip.startsWith('172.31.');
    }



    function renderSingleHops(hops) {
      return hops.map(hop => {
        const geo = hop.geo || {}, badge = detectIspBadge(geo.isp, geo.asn), flag = renderFlag(geo.country_code);
        const rtt = hop.rtt_avg ? `${hop.rtt_avg.toFixed(1)}ms` : '-';
        const rttClass = hop.rtt_avg > 100 ? 'text-amber-400' : hop.rtt_avg > 50 ? 'text-yellow-400' : 'text-emerald-400';
        const loss = hop.loss_pct > 0 ? `<span class="text-rose-400">${hop.loss_pct}%</span>` : '-';
        const isp = [geo.city, geo.isp].filter(Boolean).join(' · ') || '-';
        return `<div class="px-4 py-2 grid grid-cols-12 gap-3 items-center text-sm border-b border-slate-700/50"><div class="col-span-1 font-mono text-cyan-400 font-bold">${hop.hop}</div><div class="col-span-3 font-mono ${hop.ip === '*' ? 'text-slate-500' : ''}">${hop.ip}</div><div class="col-span-2 text-right ${rttClass} font-medium">${rtt}</div><div class="col-span-1 text-right text-xs">${loss}</div><div class="col-span-5 text-slate-400 truncate flex items-center gap-1">${renderBadge(badge)} ${flag} ${isp}</div></div>`;
      }).join('');
    }

    async function runSingleTrace(nodeId, target) {
      const res = await apiFetch(`/api/trace/run?node_id=${nodeId}&save_result=true&source_type=single`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target, max_hops: 30, include_geo: true }) });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Failed');
      return data;
    }

    async function runBidirectionalTrace() {
      const srcSel = document.getElementById('trace-src-node'), srcId = srcSel.value, srcOpt = srcSel.options[srcSel.selectedIndex];
      if (!srcId) { alert('请选择节点 A'); return; }
      const targetType = document.getElementById('trace-target-type').value;
      let targetId, targetIp, targetName, isBi = false;
      if (targetType === 'node') {
        const tgtSel = document.getElementById('trace-target-node');
        targetId = tgtSel.value;
        if (!targetId) { alert('请选择节点 B'); return; }
        const tgtOpt = tgtSel.options[tgtSel.selectedIndex];
        targetIp = tgtOpt.dataset.ip; targetName = tgtOpt.dataset.name; isBi = true;
      } else {
        targetIp = document.getElementById('trace-target-input').value.trim();
        // Strip protocol and path from URLs - traceroute only needs hostname/IP
        try {
          if (targetIp.includes('://')) {
            const url = new URL(targetIp);
            targetIp = url.hostname;
          } else if (targetIp.includes('/')) {
            targetIp = targetIp.split('/')[0];
          }
        } catch(e) { /* Not a valid URL, use as-is */ }
        if (!targetIp) { alert('请输入目标'); return; }
        targetName = targetIp;
      }
      const btn = document.getElementById('trace-start-btn'), status = document.getElementById('trace-status');
      btn.disabled = true; btn.textContent = '⏳ 追踪中...';
      document.getElementById('trace-results').classList.add('hidden');
      document.getElementById('single-result').classList.add('hidden');
      try {
        status.textContent = `正在追踪: ${srcOpt.dataset.name} → ${targetName}...`;
        const fwdData = await runSingleTrace(srcId, targetIp);
        const fwdBadges = extractRouteBadges(fwdData.hops);
        if (isBi) {
          status.textContent = `正在追踪: ${targetName} → ${srcOpt.dataset.name}...`;
          const revData = await runSingleTrace(targetId, srcOpt.dataset.ip);
          const revBadges = extractRouteBadges(revData.hops);
          document.getElementById('fwd-title').textContent = `${srcOpt.dataset.name} → ${targetName}`;
          document.getElementById('fwd-badges').innerHTML = fwdBadges.map(b => renderBadge(b)).join('');
          document.getElementById('fwd-stats').textContent = `${fwdData.total_hops}跳 | ${fwdData.elapsed_ms}ms`;
          document.getElementById('rev-title').textContent = `${targetName} → ${srcOpt.dataset.name}`;
          document.getElementById('rev-badges').innerHTML = revBadges.map(b => renderBadge(b)).join('');
          document.getElementById('rev-stats').textContent = `${revData.total_hops}跳 | ${revData.elapsed_ms}ms`;
          // Prefetch ASN tier data before rendering
          await prefetchAsnTiers([...fwdData.hops, ...revData.hops]);
          document.getElementById('comparison-body').innerHTML = renderComparisonTable(fwdData.hops, revData.hops, srcOpt.dataset.ip, srcOpt.dataset.name, targetIp, targetName);
          document.getElementById('trace-results').classList.remove('hidden');
          // Render latency charts
          renderLatencyChart('fwd-latency-chart', fwdData.hops, '去程延迟');
          renderLatencyChart('rev-latency-chart', revData.hops, '回程延迟');
          // Render AS path analysis
          renderAsPath('fwd-as-path', fwdData.hops);
          renderAsPath('rev-as-path', revData.hops);
        } else {
          document.getElementById('single-title').textContent = `${srcOpt.dataset.name} → ${targetName}`;
          document.getElementById('single-badges').innerHTML = fwdBadges.map(b => renderBadge(b)).join('');
          document.getElementById('single-stats').textContent = `${fwdData.total_hops}跳 | ${fwdData.elapsed_ms}ms | ${fwdData.tool_used}`;
          // Prefetch ASN tier data before rendering
          await prefetchAsnTiers(fwdData.hops);
          document.getElementById('single-hops').innerHTML = renderSingleHops(fwdData.hops);
          document.getElementById('single-result').classList.remove('hidden');
        }
        status.textContent = '✅ 追踪完成';
      } catch (e) { status.textContent = `❌ ${e.message}`; }
      finally { btn.disabled = false; btn.textContent = '🚀 开始追踪'; }
    }

    async function loadSchedules() {
      const list = document.getElementById('schedule-list');
      try {
        const res = await apiFetch('/api/trace/schedules');
        const data = await res.json();
        if (!data.length) { list.innerHTML = '<p class="text-slate-500 text-sm">暂无任务</p>'; return; }
        list.innerHTML = data.map(s => {
          const srcNode = nodes.find(n => n.id === s.src_node_id);
          const targetNode = s.target_type === 'node' && s.target_node_id ? nodes.find(n => n.id === s.target_node_id) : null;
          const targetDisplay = targetNode ? targetNode.name : s.target_address;
          const badge = s.enabled ? '<span class="px-2 py-0.5 bg-emerald-500/20 text-emerald-400 rounded text-xs">运行中</span>' : '<span class="px-2 py-0.5 bg-slate-600/40 text-slate-400 rounded text-xs">暂停</span>';
          return `<div class="flex items-center justify-between p-3 rounded-lg bg-slate-900/40 border border-slate-700"><div><div class="font-medium text-sm">${s.name}</div><div class="text-xs text-slate-400">${srcNode?.name || '?'} → ${targetDisplay} | ${Math.floor(s.interval_seconds/60)}分钟</div></div><div class="flex gap-2">${badge}<button onclick="toggleSchedule(${s.id}, ${!s.enabled})" class="px-2 py-1 bg-slate-700 hover:bg-slate-600 rounded text-xs">${s.enabled ? '暂停' : '启用'}</button><button onclick="deleteSchedule(${s.id})" class="px-2 py-1 bg-rose-600/30 hover:bg-rose-500/30 text-rose-300 rounded text-xs">删除</button></div></div>`;
        }).join('');
      } catch (e) { list.innerHTML = '<p class="text-rose-400 text-sm">加载失败</p>'; }
    }

    function showCreateScheduleModal() { document.getElementById('schedule-modal').classList.remove('hidden'); }
    function hideScheduleModal() { document.getElementById('schedule-modal').classList.add('hidden'); }

    async function createSchedule() {
      const name = document.getElementById('sched-name').value.trim();
      const srcId = document.getElementById('sched-src').value;
      const targetType = document.getElementById('sched-target-type').value;
      const interval = parseInt(document.getElementById('sched-interval').value) || 60;
      const alertVal = document.getElementById('sched-alert').value === 'true';
      
      let targetNodeId = null, targetAddress = null;
      if (targetType === 'node') {
        targetNodeId = parseInt(document.getElementById('sched-target-node').value);
        if (!targetNodeId) { alert('请选择目标节点'); return; }
        const targetOpt = document.getElementById('sched-target-node').selectedOptions[0];
        targetAddress = targetOpt.dataset.ip;  // Also store IP for display
      } else {
        targetAddress = document.getElementById('sched-target').value.trim();
        // Strip protocol and path from URLs - traceroute only needs hostname/IP
        try {
          if (targetAddress.includes('://')) {
            const url = new URL(targetAddress);
            targetAddress = url.hostname;
          } else if (targetAddress.includes('/')) {
            targetAddress = targetAddress.split('/')[0];
          }
        } catch(e) { /* Not a valid URL, use as-is */ }
        if (!targetAddress) { alert('请输入目标地址'); return; }
      }
      
      if (!name || !srcId) { alert('请填写完整'); return; }
      try {
        await apiFetch('/api/trace/schedules', { 
          method: 'POST', 
          headers: { 'Content-Type': 'application/json' }, 
          body: JSON.stringify({ 
            name, 
            src_node_id: parseInt(srcId), 
            target_type: targetType, 
            target_node_id: targetNodeId,
            target_address: targetAddress, 
            interval_seconds: interval * 60, 
            alert_on_change: alertVal 
          }) 
        });
        hideScheduleModal(); loadSchedules();
      } catch (e) { alert('创建失败'); }
    }

    async function toggleSchedule(id, enabled) {
      await apiFetch(`/api/trace/schedules/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }) });
      loadSchedules();
    }

    async function deleteSchedule(id) {
      if (!confirm('确定删除?')) return;
      await apiFetch(`/api/trace/schedules/${id}`, { method: 'DELETE' });
      loadSchedules();
    }


    document.addEventListener('DOMContentLoaded', function() {
      // Auth state is now handled by server-side rendering (is_guest passed to _trace_html)
      
      loadNodes();
      // Initialize tab state from URL hash
      handleHashChange();
      

    });
