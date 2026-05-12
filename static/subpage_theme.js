(() => {
  const THEME_KEY = 'nuvio-sync-theme';

  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  }

  function preferredTheme() {
    const saved = localStorage.getItem(THEME_KEY);
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
    });
  }

  function applyTheme(theme, event = null) {
    const doApply = () => {
      document.documentElement.setAttribute('data-theme', theme);
      localStorage.setItem(THEME_KEY, theme);
      renderToggle(theme);
    };

    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!reducedMotion && event && document.startViewTransition) {
      const x = event.clientX;
      const y = event.clientY;
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
  }

  function init() {
    applyTheme(preferredTheme());
    bindAllToggles();
  }

  window.NuvioSubpageTheme = {
    init,
    applyTheme,
    bindToggle,
    bindAllToggles,
    preferredTheme,
    key: THEME_KEY,
  };

  document.documentElement.setAttribute('data-theme', preferredTheme());
})();
