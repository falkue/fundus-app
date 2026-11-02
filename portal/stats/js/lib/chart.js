/**
 * Gemeinsame Diagramm-Basis: Diagramme werden in ECHTEN Pixeln gezeichnet
 * (SVG-Breite = Containerbreite, keine skalierende viewBox), damit Schrift überall
 * dieselbe Grösse hat wie der UI-Text. Bei Breitenänderung wird neu gezeichnet.
 *
 *   responsive(container, (width) => { ... zeichnen ... }, {minWidth, scroll})
 *     - ruft draw sofort und bei Breitenänderung (ResizeObserver) sowie nach dem Laden der Schriften
 *     - minWidth: darunter wird der Container horizontal scrollbar und draw(minWidth) gezeichnet
 *     - gibt eine Aufräumfunktion zurück und registriert sie (runCleanups beim Ansichtswechsel)
 *   svgPx(container, width, height, label) → d3-Selektion eines pixelgenauen SVG
 */
const d3 = globalThis.d3;
const cleanups = new Set();

export function trackCleanup(fn) { if (typeof fn === 'function') cleanups.add(fn); return fn; }
export function runCleanups() { for (const fn of cleanups) { try { fn(); } catch { /* ignore */ } } cleanups.clear(); }

export function responsive(container, draw, { minWidth = 280, scroll = false } = {}) {
  let last = -1;
  let raf = 0;
  const run = () => {
    raf = 0;
    const avail = Math.floor(container.clientWidth || container.getBoundingClientRect().width || 0);
    if (avail <= 0) return;
    let w = avail;
    if (avail < minWidth) {
      if (scroll) { w = minWidth; container.style.overflowX = 'auto'; }
      else w = Math.max(avail, 120);
    } else container.style.overflowX = '';
    if (Math.abs(w - last) < 2) return;
    last = w;
    container.replaceChildren();
    try { draw(w, { avail, scrolled: w > avail }); } catch (e) { console.error(e); }
  };
  // setTimeout statt requestAnimationFrame: rAF pausiert in Hintergrund-Tabs
  const schedule = () => { if (!raf) raf = setTimeout(run, 0); };
  if (container.isConnected) run(); else schedule();
  const ro = new ResizeObserver(schedule);
  ro.observe(container);
  if (document.fonts?.ready) document.fonts.ready.then(() => { last = -1; schedule(); });
  const cleanup = () => { ro.disconnect(); if (raf) clearTimeout(raf); };
  return trackCleanup(cleanup);
}

export function svgPx(container, width, height, label) {
  return d3.select(container).append('svg')
    .attr('width', width).attr('height', height)
    .attr('viewBox', `0 0 ${width} ${height}`)
    .style('display', 'block').style('overflow', 'visible')
    .attr('role', 'img').attr('aria-label', label ?? '');
}

/** Textbreite eines (nicht eingehängten) Textes abschätzen – grobe Reserve für Layout vor dem Messen. */
export const estimate = (s, size = 12) => String(s ?? '').length * size * 0.56;
