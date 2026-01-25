from app.dependencies import Role

def get_sidebar_css() -> str:
    """Generate shared sidebar CSS styles (legacy compatibility).
    Most styles are in glass-design.css.
    """
    return '''
    /* Global Styles */
    body {
      background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
      min-height: 100vh;
      font-family: 'Inter', system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      color: #f8fafc;
      margin: 0;
    }
    .glass-card, .card {
      background: rgba(15, 23, 42, 0.7) !important;
      backdrop-filter: blur(10px) !important;
      border: 1px solid rgba(148, 163, 184, 0.1) !important;
      border-radius: 0.75rem;
    }
    '''

def get_sidebar_js() -> str:
    """Generate shared sidebar JavaScript functions."""
    return '''
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
    function toggleTheme() {
      const body = document.body;
      const currentTheme = body.getAttribute('data-theme');
      const newTheme = currentTheme === 'light' ? 'dark' : 'light';
      body.setAttribute('data-theme', newTheme);
      localStorage.setItem('theme', newTheme);
      // Update toggle button text
      const themeText = document.getElementById('theme-text');
      if (themeText) {
        themeText.textContent = newTheme === 'light' ? '浅色模式' : '暗黑模式';
      }
    }
    // Initialize theme from localStorage
    (function() {
      const savedTheme = localStorage.getItem('theme') || 'dark';
      document.body.setAttribute('data-theme', savedTheme);
      const themeText = document.getElementById('theme-text');
      if (themeText) {
        themeText.textContent = savedTheme === 'light' ? '浅色模式' : '暗黑模式';
      }
    })();
    async function logout() {
      try {
        await fetch('/auth/logout', { method: 'POST', credentials: 'include' });
        // Clear cookies
        document.cookie.split(";").forEach(function(c) { 
            document.cookie = c.replace(/^ +/, "").replace(/=.*/, "=;expires=" + new Date().toUTCString() + ";path=/"); 
        });
        window.location.href = '/web';
      } catch (e) {
        window.location.href = '/web';
      }
    }

    // SPA Navigation Logic
    document.addEventListener('DOMContentLoaded', () => {
        document.body.addEventListener('click', async (e) => {
            const link = e.target.closest('a.nav-item');
            if (link && link.href && link.href.startsWith(window.location.origin)) {
                // Ignore hash links on same page
                const url = new URL(link.href);
                if (url.pathname === window.location.pathname && url.hash) return;
                
                e.preventDefault();
                const targetUrl = link.href;
                
                // Update active state
                document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
                link.classList.add('active');
                
                // Close mobile sidebar if open
                closeSidebar();
                
                try {
                    // Show loading state if needed (optional)
                    const mainContent = document.getElementById('main-content');
                    mainContent.style.opacity = '0.5';
                    
                    const response = await fetch(targetUrl, {
                        headers: { 'X-Content-Only': '1' }
                    });
                    
                    if (response.ok) {
                        const html = await response.text();
                        mainContent.innerHTML = html;
                        window.history.pushState({}, '', targetUrl);
                        mainContent.style.opacity = '1';
                        
                        // Execute scripts in the new content
                        const scripts = mainContent.querySelectorAll('script');
                        scripts.forEach(oldScript => {
                            const newScript = document.createElement('script');
                            Array.from(oldScript.attributes).forEach(attr => newScript.setAttribute(attr.name, attr.value));
                            newScript.appendChild(document.createTextNode(oldScript.innerHTML));
                            oldScript.parentNode.replaceChild(newScript, oldScript);
                        });
                    } else {
                        window.location.href = targetUrl; // Fallback
                    }
                } catch (err) {
                    console.error('Navigation failed:', err);
                    window.location.href = targetUrl; // Fallback
                }
            }
        });
        
        // Handle browser back/forward
        window.addEventListener('popstate', () => {
             window.location.reload(); # Simplest way to handle back button for now
        });
    });
    '''

def get_sidebar_html(current_page: str, role: Role) -> str:
    """Generate sidebar HTML widget."""
    def nav_active(page: str) -> str:
        return "active" if page == current_page else ""
    
    is_guest = (role == "guest")
    role_class = "role-guest" if is_guest else "role-admin"
    avatar_class = "guest" if is_guest else "admin"
    avatar_letter = "G" if is_guest else "A"
    user_name = "访客" if is_guest else "管理员"
    user_role = "只读模式" if is_guest else "已登录"
    
    return f'''
      <!-- Role Class Script -->
      <script>document.body.classList.add('{role_class}');</script>
      
      <!-- Guest Banner -->
      <div class="guest-banner">
        👁️ 访客模式 · 仅可查看，无法操作
      </div>
      
      <!-- Sidebar -->
      <aside class="sidebar" id="sidebar">
        <div class="sidebar-brand">
          <div class="sidebar-logo">📊</div>
          <div>
            <div class="sidebar-title">iPerf3</div>
            <div class="sidebar-subtitle">网络测试</div>
          </div>
        </div>
        
        <nav class="sidebar-nav">
          <div class="nav-section">
            <div class="nav-section-title">监控面板</div>
            <a href="/web" class="nav-item {nav_active('dashboard')}" data-page="dashboard">
              <span class="nav-item-icon">🏠</span>
              <span>节点概览</span>
            </a>
            <a href="/web/tests" class="nav-item {nav_active('tests')}" data-page="tests">
              <span class="nav-item-icon">🚀</span>
              <span>速度测试</span>
            </a>
            <a href="/web/schedules" class="nav-item {nav_active('schedules')}" data-page="schedules">
              <span class="nav-item-icon">📅</span>
              <span>定时任务</span>
            </a>
          </div>
          
          <div class="nav-section">
            <div class="nav-section-title">路由分析</div>
            <a href="/web/trace" class="nav-item {nav_active('trace')}" data-page="trace">
              <span class="nav-item-icon">🔍</span>
              <span>单次追踪</span>
            </a>
            <a href="/web/trace#schedules" class="nav-item" data-page="trace-schedules">
              <span class="nav-item-icon">📅</span>
              <span>定时追踪</span>
            </a>
            <a href="/web/trace#compare" class="nav-item" data-page="compare">
              <span class="nav-item-icon">📊</span>
              <span>多元对比</span>
            </a>
            <a href="/web/trace#history" class="nav-item" data-page="history">
              <span class="nav-item-icon">📜</span>
              <span>历史记录</span>
            </a>
          </div>
          
          <div class="nav-section admin-only">
            <div class="nav-section-title">系统设置</div>
            <a href="/web/redis" class="nav-item {nav_active('redis')}" data-page="redis">
              <span class="nav-item-icon">📊</span>
              <span>Redis 监控</span>
            </a>
            <a href="/web/whitelist" class="nav-item {nav_active('whitelist')}" data-page="whitelist">
              <span class="nav-item-icon">🛡️</span>
              <span>白名单管理</span>
            </a>
            <a href="/web/admin" class="nav-item {nav_active('admin')}" data-page="admin">
              <span class="nav-item-icon">🔐</span>
              <span>系统管理</span>
            </a>
          </div>
        </nav>
        
        <div class="sidebar-footer">
          <div class="sidebar-user">
            <div class="sidebar-avatar {avatar_class}" id="sidebar-avatar">{avatar_letter}</div>
            <div class="sidebar-user-info">
              <div class="sidebar-user-name" id="sidebar-username">{user_name}</div>
              <div class="sidebar-user-role" id="sidebar-role">{user_role}</div>
            </div>
          </div>
          <button onclick="toggleTheme()" class="theme-toggle">
            <span class="theme-toggle-icon">🌙</span>
            <span id="theme-text">暗黑模式</span>
          </button>
          <button onclick="logout()" class="btn-logout">
            <span>🚪</span>
            <span>退出登录</span>
          </button>
        </div>
      </aside>
      
      <div class="sidebar-overlay" id="sidebar-overlay" onclick="closeSidebar()"></div>
      <button class="mobile-menu-btn" id="mobile-menu-btn" onclick="toggleSidebar()">☰</button>
    '''

def render_page(
    role: Role,
    current_page: str,
    title: str,
    content: str,
    is_content_only: bool = False,
    extra_head: str = ""
) -> str:
    """Render the full page layout or just the content for SPA."""
    if is_content_only:
        return content

    sidebar_css = get_sidebar_css()
    sidebar_html = get_sidebar_html(current_page, role)
    sidebar_js = get_sidebar_js()

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} - iPerf3 测试工具</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="/static/glass-design.css" />
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    {sidebar_css}
  </style>
  {extra_head}
</head>
<body class="{'role-guest' if role == 'guest' else 'role-admin'}">
  <div class="app-layout">
    {sidebar_html}
    <main class="main-content" id="main-content">
      {content}
    </main>
  </div>
  <script>
    {sidebar_js}
  </script>
</body>
</html>'''
