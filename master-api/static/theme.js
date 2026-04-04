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
            this.currentTheme = this.getStoredTheme() || this.getPreferredTheme();
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
                const value = localStorage.getItem(THEME_KEY);
                return Object.values(THEMES).includes(value) ? value : null;
            } catch (e) {
                console.warn('localStorage not available:', e);
                return null;
            }
        }

        getPreferredTheme() {
            return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
                ? THEMES.DARK
                : THEMES.LIGHT;
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

            html.style.colorScheme = theme;

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
            const existingToggles = Array.from(document.querySelectorAll('.theme-toggle'));

            if (existingToggles.length) {
                existingToggles.forEach(toggle => this.bindToggleButton(toggle));
            } else {
                const toggle = this.createToggleButton();
                document.body.appendChild(toggle);
                this.bindToggleButton(toggle.querySelector('.theme-toggle'));
            }

            this.updateToggleUI();
        }

        bindToggleButton(button) {
            if (!button || button.dataset.themeBound === '1') {
                return;
            }

            button.dataset.themeBound = '1';
            button.removeAttribute('onclick');
            button.addEventListener('click', () => this.toggleTheme());
        }

        createToggleButton() {
            const container = document.createElement('div');
            container.className = 'theme-toggle-container theme-toggle-floating';
            container.innerHTML = `
        <button class="theme-toggle"
                aria-label="切换主题"
                title="切换深色/浅色模式">
          <div class="theme-toggle-slider">
            <span class="theme-icon"></span>
          </div>
        </button>
      `;

            return container;
        }

        updateToggleUI() {
            const isDark = this.currentTheme === THEMES.DARK;
            const icon = isDark ? '🌙' : '☀️';
            const title = isDark ? '切换到浅色模式' : '切换到深色模式';

            document.querySelectorAll('.theme-icon, .theme-toggle-icon').forEach(node => {
                node.textContent = icon;
            });

            document.querySelectorAll('.theme-toggle').forEach(button => {
                button.setAttribute('aria-label', title);
                button.setAttribute('title', title);
            });
        }
    }

    // Initialize theme manager
    window.themeManager = new ThemeManager();

    // Expose toggle function globally for easy access
    window.toggleTheme = () => window.themeManager.toggleTheme();

})();
