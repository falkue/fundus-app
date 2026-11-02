/**
 * Ansicht „Fundus-Statistik“ (#statistik): Kennzahlen zu Bestand, Folien, Nutzung und Betrieb.
 * Darstellung: portal/stats/js (ursprünglich aus dem System-Dashboard übernommen, jetzt Teil von Fundus).
 * Daten: GET /api/stats – Fundus rechnet sie selbst (fundus/stats_export.py, fundus/metrics.py); Rückfall data/stats.json.
 */
import { el } from '../lib/dom.js';

let libs = null;     // D3 (klassisches Skript) und die Dashboard-Module erst beim ersten Aufruf laden
function loadLibs() {
  libs ??= new Promise((ok, fail) => {
    if (globalThis.d3) return ok();
    const s = document.createElement('script');
    s.src = 'stats/vendor/d3.v7.9.0.min.js'; s.onload = ok; s.onerror = () => fail(new Error('D3 nicht ladbar'));
    document.head.append(s);
  }).then(() => Promise.all([import('../../stats/js/views/fundus.js'), import('../../stats/js/views/insight.js')]));
  return libs;
}

let sprite = null;   // Lucide-Icons des Dashboards (einmal laden)
function loadSprite() {
  sprite ??= fetch('stats/vendor/lucide/icons.svg', { cache: 'force-cache' }).then((r) => r.text()).then((txt) => {
    const svg = new DOMParser().parseFromString(txt, 'image/svg+xml').documentElement;
    if (svg?.tagName.toLowerCase() === 'svg') { svg.setAttribute('style', 'position:absolute;width:0;height:0'); document.body.prepend(document.adoptNode(svg)); }
  }).catch(() => { /* Icons fehlen dann nur */ });
  return sprite;
}

export function render(root, ctx) {
  let alive = true;
  const note = el('p.small.muted', 'Lade Statistik …');
  root.append(note);
  draw();
  return () => { alive = false; };

  function draw() {
    const getJson = (u) => fetch(u, { cache: 'no-store' }).then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); });
    Promise.all([getJson('/api/stats').catch(() => getJson('data/stats.json')), loadSprite(), loadLibs()])
      .then(([d, , [fundus, { insightCard }]]) => {
        if (!alive) return;
        root.replaceChildren();
        ctx.setStatus?.('ok', 'verbunden');
        const data = { fundus: d.fundus ?? null, apps: d.apps ?? [], collectors: d.collectors ?? [], generated: d.generated };
        const vctx = { insight: (id, opts) => insightCard(id, d.interp, data, opts), toast: ctx.toast, isSample: () => false,
          reload: () => { const y = window.scrollY; draw(); requestAnimationFrame(() => window.scrollTo(0, y)); } };
        try { fundus.render(root, data, vctx); }
        catch (e) { console.error(e); root.append(el('div.alert.alert--error', el('strong', 'Statistik nicht darstellbar. '), String(e.message))); }
        if (d.generated) root.append(el('p.small.muted', `Stand ${new Date(d.generated).toLocaleString('de-CH')} (live aus Fundus, höchstens eine Minute alt).`));
      })
      .catch((e) => { ctx.setStatus?.('crit', 'Statistik nicht ladbar'); if (alive) root.replaceChildren(el('div.alert.alert--error', el('strong', 'Statistik nicht ladbar. '), e.message)); });
  }
}
