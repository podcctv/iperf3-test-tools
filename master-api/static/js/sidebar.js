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
(function () {
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
        document.cookie = 'session=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT';
        document.cookie = 'guest_session=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT';
        window.location.href = '/web';
    } catch (e) {
        window.location.href = '/web';
    }
}

window.toggleSidebar = toggleSidebar;
window.closeSidebar = closeSidebar;
window.toggleTheme = toggleTheme;
window.logout = logout;
