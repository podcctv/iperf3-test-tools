export const apiFetch = async (url, options = {}) => {
    const response = await fetch(url, { credentials: 'include', ...options });
    if (!response.ok && response.status === 401) {
        // Only redirect if not already on /web (dashboard) to prevent infinite loop
        const currentPath = window.location.pathname;
        if (!currentPath.startsWith('/web') || currentPath.startsWith('/web/')) {
            // Only redirect for non-dashboard pages
            if (!currentPath.startsWith('/web/') && currentPath !== '/web' && currentPath !== '/web/') {
                window.location.href = '/web';
            }
        }
        return null;
    }
    return response;
};
