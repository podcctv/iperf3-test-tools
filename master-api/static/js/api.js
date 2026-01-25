export const apiFetch = async (url, options = {}) => {
    const response = await fetch(url, { credentials: 'include', ...options });
    if (!response.ok && response.status === 401) {
        window.location.href = '/web';
        return null;
    }
    return response;
};
