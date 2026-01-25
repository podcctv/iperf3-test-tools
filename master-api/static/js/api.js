export const apiFetch = (url, options = {}) => fetch(url, { credentials: 'include', ...options });
