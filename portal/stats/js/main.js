/**
 * Einstieg: Daten laden, Router (#hash), Navigation, Theme, Aktualisieren, Drawer.
 * Keine externen Abhängigkeiten ausser vendor/ (D3 als globales d3).
 */
import { el, clear, icon, badge, append } from './lib/dom.js';
import { safe, verdict, serviceStats, exposureStats, vulnStats, backupStats, aiStats, findingCounts } from './lib/derive.js';
import * as fmt from './lib/fmt.js';
import { openDrawer, closeDrawer } from './views/drawer.js';
import { runCleanups, trackCleanup } from './lib/chart.js';
import { insightCard } from './views/insight.js';
import { confirmDialog } from './lib/dom.js';
import * as lage from './views/lage.js';
import * as landschaft from './views/landschaft.js';
import * as dienste from './views/dienste.js';
import * as exposition from './views/exposition.js';
import * as patches from './views/patches.js';
import * as sicherung from './views/sicherung.js';
import * as verlauf from './views/verlauf.js';
import * as ki from './views/ki.js';
import * as schutz from './views/schutz.js';
import * as apps from './views/apps.js';
import * as firebase from './views/firebase.js';
import * as fundus from './views/fundus.js';
import * as energie from './views/energie.js';

// Zähler in der Navigation: überall dieselbe Bedeutung – offene, nicht quittierte Befunde
// ab Schwere „mittel" aus den Kategorien der Ansicht; Farbe nach schwerstem Befund.
const SEV_RANK = { crit: 3, high: 2, med: 1, low: 0 };
const openFindings = (d, cats, min = 1) => (d.findings || []).filter((f) => !f.acked
  && (SEV_RANK[f.severity] ?? 0) >= min && (!cats || cats.includes(f.category)));
const byCats = (cats, min) => (d) => openFindings(d, cats, min);

const VIEWS = [
  { section: 'Überblick', items: [
    { id: 'lage', label: 'Lage', icon: 'ic-lage', view: lage, findings: byCats(null, 2) },
  ] },
  { section: 'Inventar', items: [
    { id: 'landschaft', label: 'Landschaft', icon: 'ic-map', view: landschaft, findings: byCats(['hardware']) },
    { id: 'dienste', label: 'Dienste', icon: 'ic-services', view: dienste, findings: byCats(['service']) },
  ] },
  { section: 'Sicherheit', items: [
    { id: 'exposition', label: 'Exposition', icon: 'ic-exposure', view: exposition, findings: byCats(['exposure']) },
    { id: 'patches', label: 'Updates & Support', icon: 'ic-patch', view: patches, findings: byCats(['patch', 'eol']) },
    { id: 'schutz', label: 'Schutz', icon: 'ic-shield', view: schutz,
      findings: (d) => (d.checks || []).filter((c) => c.state === 'crit').map(() => ({ severity: 'crit' })) },
  ] },
  { section: 'Anwendungen', items: [
    { id: 'apps', label: 'Applikationsüberblick', icon: 'ic-apps', view: apps, count: (d) => d.apps.length },
    { id: 'firebase', label: 'Firebase-Monitoring', icon: 'ic-fire', view: firebase, badge: (d) => d.apps.filter((a) => a.platform === 'firebase' && (a.quotas ?? []).some((q) => q.state === 'crit')).length },
    { id: 'fundus', label: 'Fundus', icon: 'lu-database', view: fundus },
  ] },
  { section: 'Betrieb', items: [
    { id: 'sicherung', label: 'Sicherung', icon: 'ic-backup', view: sicherung, findings: byCats(['backup']) },
    { id: 'verlauf', label: 'Kapazität & Verlauf', icon: 'ic-trend', view: verlauf, findings: byCats(['capacity']) },
    { id: 'ki', label: 'KI-Knoten', icon: 'ic-ai', view: ki, findings: byCats(['ai']) },
    { id: 'energie', label: 'Nachhaltigkeit', icon: 'ic-leaf', view: energie },
  ] },
];
const ALL = VIEWS.flatMap((s) => s.items);

const state = { data: null, source: null, sample: false, error: null, current: null, interp: null, interpSample: false };

// ---------------------------------------------------------------- Theme
const THEME_KEY = 'v2-theme';
function applyTheme(t) {
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  document.querySelectorAll('#theme-toggle button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.theme === t)));
  try { localStorage.setItem(THEME_KEY, t); } catch { /* privat */ }
  // Diagramme hängen an CSS-Variablen; nichts neu zu zeichnen.
}
document.getElementById('theme-toggle').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-theme]');
  if (b) applyTheme(b.dataset.theme);
});
try { applyTheme(localStorage.getItem(THEME_KEY) || 'auto'); } catch { applyTheme('auto'); }

// ---------------------------------------------------------------- Toast
let toastTimer;
export function toast(msg, ms = 3200) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.dataset.show = 'true';
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.dataset.show = 'false'; }, ms);
}

// ---------------------------------------------------------------- Daten
async function fetchJson(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}
async function loadData() {
  const param = new URLSearchParams(location.search).get('data');
  const candidates = param ? [param] : ['landscape.json', 'landscape.sample.json'];
  let lastErr = null;
  for (const url of candidates) {
    try {
      const raw = await fetchJson(url);
      state.source = url;
      state.sample = url !== 'landscape.json' && !param ? true : url.includes('sample');
      state.data = safe(raw);
      // Fundus-Kennzahlen (optional, null wenn der Kollektor scheiterte) – safe() kennt das Feld nicht
      state.data.fundus = raw && typeof raw.fundus === 'object' ? raw.fundus : null;
      state.error = null;
      return;
    } catch (e) { lastErr = e; }
  }
  state.data = safe(null);
  state.error = lastErr;
}
async function loadInterpretations() {
  for (const url of ['interpretations.json', 'interpretations.sample.json']) {
    try { state.interp = await fetchJson(url); state.interpSample = url.includes('sample'); return; } catch { /* weiter */ }
  }
  state.interp = null;
}

// ---------------------------------------------------------------- Navigation
function buildNav() {
  const nav = clear(document.getElementById('nav'));
  for (const sec of VIEWS) {
    nav.append(el('div.sidebar__label', { 'aria-hidden': 'true' }, sec.section));
    const list = el('div.nav');
    for (const it of sec.items) {
      const a = el('a.nav__link', { href: `#${it.id}`, id: `nav-${it.id}` }, icon(it.icon), el('span', it.label));
      const d = state.data;
      if (d) {
        const fs = it.findings ? it.findings(d) : [];
        if (fs.length) {
          const worst = Math.max(...fs.map((f) => SEV_RANK[f.severity] ?? 0));
          const what = it.id === 'schutz' ? 'Prüfungen kritisch' : (it.id === 'lage' ? 'kritische/hohe Befunde' : 'offene Befunde');
          a.append(el(`span.nav__count.badge.${worst >= 3 ? 'badge--danger' : 'badge--warning'}`,
            { title: `${fs.length} ${what}`, 'aria-label': `${fs.length} ${what}` }, String(fs.length)));
        }
      }
      list.append(a);
    }
    nav.append(list);
  }
  updateActive();
}
function updateActive() {
  document.querySelectorAll('.nav__link').forEach((a) => {
    const active = a.id === `nav-${state.current}`;
    a.classList.toggle('active', active);
    if (active) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    if (active && window.matchMedia('(max-width: 760px)').matches) a.scrollIntoView?.({ block: 'nearest', inline: 'center' });
  });
}

function updateSidebarStatus() {
  const box = clear(document.getElementById('sidebar-status'));
  const d = state.data;
  if (state.error && !d.generated) {
    box.append(el('span.status-dot.status-dot--crit', { 'aria-hidden': 'true' }), el('span', 'Keine Daten geladen'));
    return;
  }
  const v = verdict(d);
  box.append(el('span.status-dot', { class: `status-dot status-dot--${v.state === 'ok' ? 'ok' : v.state}`, 'aria-hidden': 'true' }),
    el('span', `Stand ${fmt.dateTime(d.generated)} · ${fmt.ago(d.generated)}`));
  const sub = document.getElementById('brand-sub');
  sub.textContent = state.sample ? 'Beispieldaten' : `${d.nodes.length} Systeme · ${(d.apps || []).length} Anwendungen`;
  sub.title = d.nodes.map((n) => n.label).filter(Boolean).join(' · ');
}

// ---------------------------------------------------------------- Aktionen
async function refresh() {
  const btn = document.getElementById('btn-refresh');
  btn.disabled = true;
  toast('Kollektor wird angestossen…', 2000);
  try {
    const r = await fetch('../api/refresh-v2', { method: 'POST' });
    if (r.status === 409) toast('Ein Lauf ist bereits aktiv. Bitte kurz warten.');
    else if (r.status === 404) toast('Der Server kennt /api/refresh-v2 noch nicht – Daten werden nur neu geladen.');
    else if (!r.ok) toast(`Aktualisieren fehlgeschlagen (HTTP ${r.status}).`);
    else toast('Lauf abgeschlossen, Daten werden neu geladen.');
  } catch {
    toast('Kein Server erreichbar – Daten werden nur neu geladen.');
  }
  await Promise.all([loadData(), loadInterpretations()]);
  buildNav(); updateSidebarStatus(); render();
  btn.disabled = false;
}
/** Sicherung manuell auslösen: POST ../api/backup {job}, danach Kollektor anstossen. */
async function backup(job) {
  const ok = await confirmDialog({
    title: `${job.label ?? job.id} jetzt sichern?`,
    text: `${job.id === 'restic-bilder' || job.id === 'restic-system' ? 'Bilder und System werden zusammen gesichert (ein Hero-Dienst). ' : ''}Läuft im Hintergrund – Dauer je nach Änderungen Minuten bis Stunden.`,
    confirmLabel: 'Jetzt sichern',
  });
  if (!ok) return false;
  try {
    const r = await fetch('../api/backup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job: job.id }) });
    let body = null; try { body = await r.json(); } catch { /* kein JSON */ }
    const msg = body?.message ?? body?.error ?? null;
    if (r.status === 202) toast(msg ?? `${job.label ?? job.id}: Sicherung gestartet.`, 5000);
    else if (r.status === 409) toast(msg ?? 'Diese Sicherung läuft bereits.');
    else if (r.status === 429) toast(msg ?? 'Gerade erst ausgelöst – bitte eine Minute warten.');
    else if (r.status === 400) toast(msg ?? 'Unbekannter Sicherungsauftrag.');
    else if (r.status === 502 || r.status === 504) toast(msg ?? 'xerox ist nicht erreichbar.');
    else if (r.status === 404) toast('Der Server kennt /api/backup noch nicht.');
    else toast(msg ?? `Auslösen fehlgeschlagen (HTTP ${r.status}).`);
    if (r.status === 202) setTimeout(() => refresh(), 5000);
    return r.status === 202;
  } catch {
    toast('Kein Server erreichbar – Sicherung konnte nicht ausgelöst werden.');
    return false;
  }
}
async function ack(id) {
  try {
    const r = await fetch('../api/ack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: id }) });
    if (!r.ok) throw new Error(String(r.status));
    toast('Quittiert.');
  } catch {
    toast('Quittieren nicht möglich (kein Server). Lokal ausgeblendet.');
  }
  const f = state.data.findings.find((x) => x.id === id);
  if (f) f.acked = true;
  buildNav(); render();
}

const ctx = {
  navigate(hash) { location.hash = hash; },
  openNode(nodeId) { openDrawer(state.data, nodeId, ctx); },
  closeDrawer,
  ack,
  refresh,
  toast,
  isSample: () => state.sample,
  backup,
  /** KI-Einschätzung für eine Ansicht (Karte oder null). */
  insight: (viewId, opts) => insightCard(viewId, state.interp, state.data, opts),
  interpIsSample: () => state.interpSample,
  source: () => state.source,
};

// ---------------------------------------------------------------- Router
function currentId() {
  const h = (location.hash || '#lage').slice(1).split('?')[0];
  return ALL.some((v) => v.id === h) ? h : 'lage';
}
function render() {
  const id = currentId();
  state.current = id;
  updateActive();
  const root = document.getElementById('view');
  runCleanups();
  clear(root);
  const d = state.data;
  if (state.sample || state.interpSample) {
    root.append(el('div.row', badge('Beispieldaten', 'demo'), el('span.small.muted', state.sample ? `Quelle: ${state.source} – echter Kollektor liefert landscape.json.` : 'KI-Einschätzung aus interpretations.sample.json – interpret.py liefert interpretations.json.')));
  }
  if (state.error && !d.generated) {
    root.append(el('div.alert.alert--error', el('div', el('strong', 'Keine Daten geladen. '), `Weder landscape.json noch landscape.sample.json konnten gelesen werden (${state.error.message}).`)));
  }
  const view = ALL.find((v) => v.id === id).view;
  try {
    const c = view.render(root, d, ctx);
    if (typeof c === 'function') trackCleanup(c);
  } catch (e) {
    console.error(e);
    root.append(el('div.alert.alert--error', el('div', el('strong', 'Diese Ansicht konnte nicht gezeichnet werden. '), String(e.message))));
  }
  document.title = `${ALL.find((v) => v.id === id).label} · Systemlandschaft`;
  window.scrollTo({ top: 0 });
}

window.addEventListener('hashchange', () => { closeDrawer(); if (state.data) render(); }); // vor dem ersten Laden: start() zeichnet
document.getElementById('btn-refresh').addEventListener('click', refresh);
document.getElementById('drawer-close').addEventListener('click', closeDrawer);
document.getElementById('drawer-backdrop').addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

// Alle 10 Minuten neu laden, wenn der Tab sichtbar ist (Stand-Anzeige bleibt frisch)
setInterval(async () => {
  if (document.hidden) return;
  const before = state.data?.generated;
  await loadData();
  if (state.data.generated !== before) { buildNav(); updateSidebarStatus(); render(); toast('Neue Daten geladen.'); }
  else updateSidebarStatus();
}, 10 * 60 * 1000);

async function loadSprite() {
  try {
    const txt = await (await fetch('vendor/lucide/icons.svg', { cache: 'force-cache' })).text();
    const doc = new DOMParser().parseFromString(txt, 'image/svg+xml');
    const svg = doc.documentElement;
    if (svg && svg.tagName.toLowerCase() === 'svg') document.body.prepend(document.adoptNode(svg));
  } catch { /* Icons fehlen dann nur */ }
}
(async function start() {
  await Promise.all([loadData(), loadInterpretations(), loadSprite()]);
  buildNav();
  updateSidebarStatus();
  render();
})();
