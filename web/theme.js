/* Light/dark theme. Follows the system setting (prefers-color-scheme, which
 * needs no permission) until the visitor flips the switch.
 * Load it as a plain (non-deferred) script inside <head>, before
 * pixel-transition.js, so the page paints in the right theme from the start.
 * Override with ?theme=light|dark for testing. */
(() => {
  'use strict';

  const KEY = 'theme';
  const THEMES = ['light', 'dark'];
  const root = document.documentElement;
  const systemQuery = window.matchMedia?.('(prefers-color-scheme: light)');
  const forced = new URLSearchParams(location.search).get('theme');

  let themeSwitch = null;

  function readSaved() {
    try {
      return localStorage.getItem(KEY);
    } catch {
      return null;
    }
  }

  function save(theme) {
    try {
      if (theme) localStorage.setItem(KEY, theme);
      else localStorage.removeItem(KEY);
    } catch {}
  }

  let chosen = THEMES.includes(forced) ? forced : readSaved();

  const systemTheme = () => (systemQuery?.matches ? 'light' : 'dark');
  const currentTheme = () => (THEMES.includes(chosen) ? chosen : systemTheme());

  function apply() {
    const theme = currentTheme();
    if (root.dataset.theme === theme) return;
    root.dataset.theme = theme;
    syncSwitch();
    window.dispatchEvent(new CustomEvent('themechange', { detail: { theme } }));
  }

  function syncSwitch() {
    if (!themeSwitch) return;
    const light = root.dataset.theme === 'light';
    themeSwitch.setAttribute('aria-checked', String(light));
    themeSwitch.title = light ? 'Switch to dark theme' : 'Switch to light theme';
  }

  function toggle() {
    const next = currentTheme() === 'light' ? 'dark' : 'light';
    // Picking what the system already wants means "follow the system" again.
    chosen = next === systemTheme() ? null : next;
    save(chosen);
    apply();
  }

  const SUN = '<path d="M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10zM11 1h2v3h-2zm0 19h2v3h-2zM1 11h3v2H1zm19 0h3v2h-3zM4.22 5.64l1.42-1.42 2.12 2.12-1.42 1.42zm12.02 12.02 1.42-1.42 2.12 2.12-1.42 1.42zM4.22 18.36l2.12-2.12 1.42 1.42-2.12 2.12zM16.24 6.34l2.12-2.12 1.42 1.42-2.12 2.12z"/>';
  const MOON = '<path d="M12.3 2a9 9 0 1 0 9.7 11.6A7.5 7.5 0 0 1 12.3 2z"/>';
  const icon = (paths, cls = '') => `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${paths}</svg>`;

  function mountSwitch() {
    themeSwitch = document.createElement('button');
    themeSwitch.type = 'button';
    themeSwitch.className = 'theme-switch';
    themeSwitch.setAttribute('role', 'switch');
    themeSwitch.setAttribute('aria-label', 'Light theme');
    themeSwitch.innerHTML =
      `<span class="theme-switch-track" aria-hidden="true">${icon(MOON)}${icon(SUN)}` +
      `<span class="theme-switch-knob">${icon(MOON, 'knob-moon')}${icon(SUN, 'knob-sun')}</span></span>`;
    themeSwitch.addEventListener('click', toggle);

    // Sit next to the button that owns the top-right corner, in the same
    // container, so the switch stays reachable when that container goes fullscreen.
    const audioToggle = document.querySelector('.audio-toggle-top');
    const fullscreenBtn = document.querySelector('.fullscreen-btn');
    if (audioToggle) {
      themeSwitch.classList.add('is-beside-audio');
      audioToggle.before(themeSwitch);
    } else if (fullscreenBtn) {
      themeSwitch.classList.add('is-beside-fullscreen');
      fullscreenBtn.before(themeSwitch);
    } else {
      document.body.prepend(themeSwitch);
    }
    syncSwitch();
  }

  apply();
  systemQuery?.addEventListener?.('change', apply);

  // Keep other open tabs of the site in step.
  window.addEventListener('storage', (e) => {
    if (e.key !== KEY || THEMES.includes(forced)) return;
    chosen = e.newValue;
    apply();
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mountSwitch, { once: true });
  else mountSwitch();
})();
