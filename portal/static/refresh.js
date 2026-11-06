/*
  refresh.js: pull-to-refresh for the home-screen app.

  Safari has pull-to-refresh built in, but a web app opened from the Home
  Screen (standalone) doesn't get it, so this adds it back there and does
  nothing in a normal browser tab. Pull down from the top of any page: a
  round arrow appears under the status bar and turns as you pull; once it
  fills in, letting go reloads the page.

  It never reloads over unsaved typing (a search not yet run, the My games
  or settings forms): once any field on the page has been changed, the
  arrow shows a "-" and the pull does nothing. Pulls that start in a text
  field or on a button are left alone.

  Passive listeners only: the page's own scrolling and iOS's rubber-band
  bounce are untouched; this just watches the distance.
*/
(() => {
  const PULL = 80;          // points of finger travel that count as "refresh"
  const IGNORE = 'input, textarea, select, button';

  // Checked at each pull, not at load, so it follows the app however it was opened.
  const standalone = () => navigator.standalone === true ||
    window.matchMedia('(display-mode: standalone)').matches;

  let dirty = false;        // a field on this page has been edited
  document.addEventListener('input', () => { dirty = true; }, true);
  document.addEventListener('change', () => { dirty = true; }, true);
  // Submitting saves the edits, so the next page starts clean anyway.
  document.addEventListener('submit', () => { dirty = false; }, true);

  const ring = document.createElement('div');
  ring.className = 'ptr';
  ring.setAttribute('aria-hidden', 'true');
  ring.textContent = '↻';
  document.body.appendChild(ring);

  let startY = null;        // where the finger came down, while a pull is possible
  let ready = false;        // pulled far enough to refresh

  function reset() {
    startY = null;
    ready = false;
    ring.classList.remove('ready', 'blocked');
    ring.style.opacity = '';
    ring.style.transform = '';
  }

  document.addEventListener('touchstart', (e) => {
    reset();
    if (!standalone() || e.touches.length !== 1 || window.scrollY > 0) return;
    if (e.target.closest && e.target.closest(IGNORE)) return;
    startY = e.touches[0].clientY;
  }, { passive: true });

  document.addEventListener('touchmove', (e) => {
    if (startY === null) return;
    const dy = e.touches[0].clientY - startY;
    if (dy < 0 || window.scrollY > 0) { reset(); return; }   // scrolling the page, not pulling
    if (dy === 0) return;
    const p = Math.min(dy / PULL, 1);
    ready = p >= 1 && !dirty;
    ring.classList.toggle('ready', ready);
    ring.classList.toggle('blocked', p >= 1 && dirty);
    ring.textContent = p >= 1 && dirty ? '–' : '↻';
    ring.style.opacity = String(Math.min(p * 1.4, 1));
    ring.style.transform = `translate(-50%, ${Math.min(dy, PULL * 1.3) * 0.5}px) rotate(${p * 300}deg)`;
  }, { passive: true });

  document.addEventListener('touchend', () => {
    if (ready) {
      ring.classList.add('spinning');
      window.location.reload();
      return;
    }
    reset();
  }, { passive: true });
  document.addEventListener('touchcancel', reset, { passive: true });

  // Coming back to a page from the back/forward cache: clear a leftover spinner.
  window.addEventListener('pageshow', () => { ring.classList.remove('spinning'); reset(); });
})();
