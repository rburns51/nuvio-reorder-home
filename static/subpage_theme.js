(() => {
  const THEME_KEY = 'nuvio-sync-theme';
  let initialized = false;

  function safeGet(key) {
    try { return window.localStorage.getItem(key); } catch (_err) { return null; }
  }

  function safeSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (_err) {}
  }

  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  }

  function preferredTheme() {
    const saved = safeGet(THEME_KEY);
    if (saved === 'light' || saved === 'dark') return saved;
    const prefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
    return prefersLight ? 'light' : 'dark';
  }

  function renderToggle(theme) {
    const iconClass = `bi ${theme === 'light' ? 'bi-moon' : 'bi-sun'}`;
    document.querySelectorAll('[data-theme-toggle] i').forEach((icon) => {
      icon.className = iconClass;
    });
    document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
      button.setAttribute('aria-pressed', theme === 'dark' ? 'true' : 'false');
      button.setAttribute('aria-label', theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
      button.setAttribute('title', theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
    });
  }

  function applyTheme(theme, event = null) {
    const nextTheme = theme === 'light' ? 'light' : 'dark';
    const doApply = () => {
      document.documentElement.setAttribute('data-theme', nextTheme);
      safeSet(THEME_KEY, nextTheme);
      renderToggle(nextTheme);
      window.dispatchEvent(new CustomEvent('nuvio-theme-changed', { detail: { theme: nextTheme } }));
    };

    const reducedMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!reducedMotion && event && document.startViewTransition) {
      const x = event.clientX || Math.floor(window.innerWidth / 2);
      const y = event.clientY || 0;
      const maxRadius = Math.hypot(Math.max(x, window.innerWidth - x), Math.max(y, window.innerHeight - y));
      const transition = document.startViewTransition(doApply);
      transition.ready.then(() => {
        document.documentElement.animate(
          { clipPath: [`circle(0% at ${x}px ${y}px)`, `circle(${maxRadius}px at ${x}px ${y}px)`] },
          { duration: 420, easing: 'ease-in-out', pseudoElement: '::view-transition-new(root)' },
        );
      }).catch(() => {});
      return;
    }

    doApply();
  }

  function bindToggle(button) {
    if (!button || button.dataset.themeBound === 'true') return;
    button.dataset.themeBound = 'true';
    button.addEventListener('click', (event) => {
      const next = currentTheme() === 'light' ? 'dark' : 'light';
      applyTheme(next, event);
    });
  }

  function bindAllToggles() {
    document.querySelectorAll('[data-theme-toggle]').forEach(bindToggle);
    renderToggle(currentTheme());
  }

  function init() {
    if (!initialized) {
      initialized = true;
      applyTheme(preferredTheme());
    } else {
      renderToggle(currentTheme());
    }
    bindAllToggles();
  }

  window.NuvioSubpageTheme = {
    init,
    applyTheme,
    bindToggle,
    bindAllToggles,
    preferredTheme,
    currentTheme,
    key: THEME_KEY,
  };

  document.documentElement.setAttribute('data-theme', preferredTheme());

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
