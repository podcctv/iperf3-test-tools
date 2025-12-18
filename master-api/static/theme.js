/**
 * Theme Management System
 * Handles dark/light mode switching with localStorage persistence
 */

(function () {
    'use strict';

    const THEME_KEY = 'iperf-theme';
    const THEMES = {
        DARK: 'dark',
        LIGHT: 'light'
    };

    class ThemeManager {
        constructor() {
            this.currentTheme = this.getStoredTheme() || THEMES.DARK;
            this.init();
        }

        init() {
            // Apply theme immediately to prevent flash
            this.applyTheme(this.currentTheme, true);

            // Wait for DOM to be ready
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', () => this.setupToggle());
            } else {
                this.setupToggle();
            }
        }

        getStoredTheme() {
            try {
                return localStorage.getItem(THEME_KEY);
            } catch (e) {
                console.warn('localStorage not available:', e);
                return null;
            }
        }

        setStoredTheme(theme) {
            try {
                localStorage.setItem(THEME_KEY, theme);
            } catch (e) {
                console.warn('Failed to store theme:', e);
            }
        }

        applyTheme(theme, immediate = false) {
            const html = document.documentElement;

            // Add no-transitions class for immediate application
            if (immediate) {
                html.classList.add('no-transitions');
            }

            // Set theme attribute
            if (theme === THEMES.LIGHT) {
                html.setAttribute('data-theme', 'light');
            } else {
                html.removeAttribute('data-theme');
            }

            this.currentTheme = theme;
            this.setStoredTheme(theme);

            // Remove no-transitions class after a tick
            if (immediate) {
                setTimeout(() => html.classList.remove('no-transitions'), 10);
            }

            // Update toggle button if it exists
            this.updateToggleUI();

            // Dispatch custom event for other components
            window.dispatchEvent(new CustomEvent('themechange', { detail: { theme } }));
        }

        toggleTheme() {
            const newTheme = this.currentTheme === THEMES.DARK ? THEMES.LIGHT : THEMES.DARK;
            this.applyTheme(newTheme);
        }

        setupToggle() {
            // Create toggle button
            const toggle = this.createToggleButton();

            // Find navigation bar and insert toggle
            const nav = document.querySelector('.flex.flex-wrap.items-center.gap-3');
            if (nav) {
                // Insert before logout button
                const logoutBtn = document.getElementById('logout-btn');
                if (logoutBtn) {
                    nav.insertBefore(toggle, logoutBtn);
                } else {
                    nav.appendChild(toggle);
                }
            }

            // Update initial UI state
            this.updateToggleUI();
        }

        createToggleButton() {
            const container = document.createElement('div');
            container.className = 'theme-toggle-container';
            container.innerHTML = `
        <button id="theme-toggle-btn" 
                class="theme-toggle" 
                aria-label="Toggle theme"
                title="切换深色/浅色模式">
          <div class="theme-toggle-slider">
            <span class="theme-icon"></span>
          </div>
        </button>
      `;

            const button = container.querySelector('#theme-toggle-btn');
            button.addEventListener('click', () => this.toggleTheme());

            return container;
        }

        updateToggleUI() {
            const icon = document.querySelector('.theme-icon');
            if (icon) {
                icon.textContent = this.currentTheme === THEMES.DARK ? '🌙' : '☀️';
            }
        }
    }

    // Initialize theme manager
    window.themeManager = new ThemeManager();

    // Expose toggle function globally for easy access
    window.toggleTheme = () => window.themeManager.toggleTheme();

})();
