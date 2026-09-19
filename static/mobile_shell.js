const MOBILE_QUERY = '(max-width: 900px)';

function ensureMobileShell() {
  const sidebar = document.querySelector('.sidebar');
  const topbar = document.querySelector('.topbar');
  if (!sidebar || !topbar || document.querySelector('#mobile-nav-toggle')) return;

  if (!sidebar.id) sidebar.id = 'primary-sidebar';

  const toggle = document.createElement('button');
  toggle.id = 'mobile-nav-toggle';
  toggle.className = 'icon-button mobile-nav-toggle';
  toggle.type = 'button';
  toggle.textContent = '☰';
  toggle.title = 'Open navigation';
  toggle.setAttribute('aria-label', 'Open navigation');
  toggle.setAttribute('aria-controls', sidebar.id);
  toggle.setAttribute('aria-expanded', 'false');

  const backdrop = document.createElement('button');
  backdrop.id = 'mobile-nav-backdrop';
  backdrop.className = 'mobile-nav-backdrop';
  backdrop.type = 'button';
  backdrop.tabIndex = -1;
  backdrop.setAttribute('aria-label', 'Close navigation');

  topbar.prepend(toggle);
  document.body.appendChild(backdrop);

  const media = window.matchMedia(MOBILE_QUERY);

  const setOpen = (open, { restoreFocus = true } = {}) => {
    const mobile = media.matches;
    const next = mobile && Boolean(open);
    document.body.classList.toggle('mobile-nav-open', next);
    toggle.setAttribute('aria-expanded', String(next));
    toggle.textContent = next ? '×' : '☰';
    toggle.title = next ? 'Close navigation' : 'Open navigation';
    toggle.setAttribute('aria-label', toggle.title);

    if (mobile) {
      if (next) {
        sidebar.inert = false;
        sidebar.removeAttribute('inert');
      } else {
        sidebar.inert = true;
        sidebar.setAttribute('inert', '');
      }
      sidebar.setAttribute('aria-hidden', String(!next));
    } else {
      sidebar.inert = false;
      sidebar.removeAttribute('inert');
      sidebar.removeAttribute('aria-hidden');
    }

    if (next) {
      const first = sidebar.querySelector(
        'button:not(.icon-button):not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, button:not([disabled])'
      );
      window.setTimeout(() => {
        if (first && document.body.contains(first)) {
          first.focus({ preventScroll: true });
        }
      }, 0);
    } else if (restoreFocus && mobile && document.activeElement && sidebar.contains(document.activeElement)) {
      toggle.focus({ preventScroll: true });
    }
  };

  const syncMode = () => {
    setOpen(false, { restoreFocus: false });
    if (!media.matches) {
      sidebar.removeAttribute('inert');
      sidebar.removeAttribute('aria-hidden');
    }
  };

  toggle.addEventListener('click', () => {
    setOpen(!document.body.classList.contains('mobile-nav-open'));
  });
  backdrop.addEventListener('click', () => setOpen(false));

  sidebar.addEventListener('click', (event) => {
    if (!media.matches) return;
    const target = event.target instanceof Element ? event.target.closest('button, a[href]') : null;
    if (!target) return;
    if (target.id === 'refresh-token-usage' || target.id === 'compact-context') return;
    setOpen(false);
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.body.classList.contains('mobile-nav-open')) {
      event.preventDefault();
      setOpen(false);
    }
  });

  media.addEventListener?.('change', syncMode);
  syncMode();

  window.addEventListener('codex-mobile-nav-close', () => setOpen(false));
}

function syncVisualViewport() {
  const height = window.visualViewport?.height || window.innerHeight;
  if (!height) return;
  document.documentElement.style.setProperty('--mobile-viewport-height', `${Math.round(height)}px`);
}

function installMobileViewportTracking() {
  syncVisualViewport();
  window.addEventListener('resize', syncVisualViewport, { passive: true });
  window.visualViewport?.addEventListener('resize', syncVisualViewport, { passive: true });
  window.visualViewport?.addEventListener('scroll', syncVisualViewport, { passive: true });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    ensureMobileShell();
    installMobileViewportTracking();
  }, { once: true });
} else {
  ensureMobileShell();
  installMobileViewportTracking();
}
