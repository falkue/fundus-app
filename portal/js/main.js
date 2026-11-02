/**
 * Fundus-Portal: eigenständige Weboberfläche des Fundus-Servers (/portal/, gleiche Herkunft wie /api/…).
 * Gestaltung aus dem Systemlandschaft-Dashboard übernommen (tokens.css, app.css, dom.js, fmt.js), aber ohne
 * Abhängigkeit davon. Bereiche: Folien (weitere folgen). Router über #hash, Theme, Toast, Status in der Seitenleiste.
 *
 * Seitenleiste „Projekte“: jedes Projekt (eigener Name oder Kennung) führt zu seinen Folien; aufgeklappt zeigt es seine
 * Foliensätze. Das aktive Projekt ist immer aufgeklappt, weitere merkt sich der Browser (localStorage). Die Reihenfolge
 * (A–Z, Z–A, neueste/älteste zuerst) speichert Fundus für alle Geräte: POST /api/portal/settings {project_sort}.
 */
import { el, clear, icon } from './lib/dom.js';
import * as fmt from './lib/fmt.js';
import * as folien from './views/folien.js';
import * as pruefungen from './views/pruefungen.js';
import * as suche from './views/suche.js';
import * as statistik from './views/statistik.js';
import * as fotos from './views/fotos.js';
import * as datenqualitaet from './views/datenqualitaet.js';
import * as papierkorb from './views/papierkorb.js';
import * as archiv from './views/archiv.js';
import * as daten from './views/daten.js';
import * as verwaltung from './views/verwaltung.js';
import * as austausch from './views/austausch.js';

const VIEWS = [{ id: 'folien', label: 'Folien', view: folien }, { id: 'pruefungen', label: 'Prüfungen', view: pruefungen },
  { id: 'suche', label: 'Foliensuche', view: suche }, { id: 'statistik', label: 'Fundus-Statistik', view: statistik },
  { id: 'mediathek', label: 'Mediathek', view: fotos }, { id: 'fotos', label: 'Mediathek', view: fotos },   // #fotos: alte Links
  { id: 'datenqualitaet', label: 'Datenqualität', view: datenqualitaet }, { id: 'papierkorb', label: 'Papierkorb', view: papierkorb },
  { id: 'archiv', label: 'Archiv', view: archiv }, { id: 'daten', label: 'Datensätze', view: daten },
  { id: 'erhalten', label: 'Erhaltene Foliensätze', view: austausch }, { id: 'gesendet', label: 'Gesendet', view: austausch.gesendet },

  { id: 'verwaltung', label: 'Verwaltung', view: verwaltung }];
let cleanup = null;
let overview = null;   // GET /api/exhibits: Projekte mit Foliensätzen (für die Navigation)
let fbCounts = null;   // GET /api/feedback/counts: offene Punkte und Entscheidungen
let saved = null;      // GET /api/collections: gespeicherte Suchen (eigener Bereich in der Seitenleiste)
let trashN = 0;        // GET /api/trash: Einträge im Papierkorb (Zahl in der Seitenleiste)
let archiveN = 0;      // GET /api/archive: archivierte Folien (Zahl in der Seitenleiste)
let receivedNew = 0;   // GET /api/received: neue, noch nicht angenommene Pakete (Zahl in der Seitenleiste)

// ---------------------------------------------------------------- Theme
const THEME_KEY = 'fundus-theme';
function applyTheme(t) {
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  document.querySelectorAll('#theme-toggle button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.theme === t)));
  try { localStorage.setItem(THEME_KEY, t); } catch { /* privat */ }
}
document.getElementById('theme-toggle').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-theme]');
  if (b) applyTheme(b.dataset.theme);
});
try { applyTheme(localStorage.getItem(THEME_KEY) || 'auto'); } catch { applyTheme('auto'); }

// ---------------------------------------------------------------- Seitenleiste: Breite ziehen, ein- und ausblenden
// Breite (180–480 px) und Zustand merkt sich der Browser; schmal (≤ 760 px) liegt die Leiste oben und bleibt fest.
const SIDEBAR_KEY = 'fundus-sidebar';
const SB_MIN = 180, SB_MAX = 480;
const shell = document.getElementById('shell');
const resizer = document.getElementById('sidebar-resizer');
const sbState = (() => { try { return JSON.parse(localStorage.getItem(SIDEBAR_KEY) || '{}'); } catch { return {}; } })();
const saveSb = () => { try { localStorage.setItem(SIDEBAR_KEY, JSON.stringify(sbState)); } catch { /* privat */ } };
function setSidebarWidth(w, save = true) {
  if (w == null) { shell.style.removeProperty('--sidebar-width'); delete sbState.width; }
  else { sbState.width = Math.round(Math.min(SB_MAX, Math.max(SB_MIN, w))); shell.style.setProperty('--sidebar-width', `${sbState.width}px`); }
  resizer.setAttribute('aria-valuenow', String(sbState.width ?? 232));
  if (save) saveSb();
}
function setSidebarHidden(hidden, save = true) {
  sbState.hidden = !!hidden;
  shell.classList.toggle('shell--nosidebar', sbState.hidden);
  document.getElementById('sidebar-show').hidden = !sbState.hidden;
  if (save) saveSb();
}
resizer.setAttribute('aria-valuemin', String(SB_MIN)); resizer.setAttribute('aria-valuemax', String(SB_MAX));
if (sbState.width) setSidebarWidth(sbState.width, false); else setSidebarWidth(null, false);
setSidebarHidden(sbState.hidden, false);
resizer.addEventListener('pointerdown', (e) => {
  if (e.button !== 0) return;
  e.preventDefault();
  resizer.setPointerCapture(e.pointerId);
  const left = shell.getBoundingClientRect().left;
  shell.classList.add('shell--resizing');
  const move = (ev) => setSidebarWidth(ev.clientX - left, false);
  const up = () => { shell.classList.remove('shell--resizing'); saveSb(); resizer.removeEventListener('pointermove', move); resizer.removeEventListener('pointerup', up); resizer.removeEventListener('pointercancel', up); };
  resizer.addEventListener('pointermove', move);
  resizer.addEventListener('pointerup', up);
  resizer.addEventListener('pointercancel', up);
});
resizer.addEventListener('dblclick', () => setSidebarWidth(null));
resizer.addEventListener('keydown', (e) => {
  const cur = sbState.width ?? 232;
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); setSidebarWidth(cur + (e.key === 'ArrowRight' ? 16 : -16)); }
  else if (e.key === 'Home') { e.preventDefault(); setSidebarWidth(SB_MIN); }
  else if (e.key === 'End') { e.preventDefault(); setSidebarWidth(SB_MAX); }
});
document.getElementById('sidebar-hide').addEventListener('click', () => { setSidebarHidden(true); document.getElementById('sidebar-show').focus(); });
document.getElementById('sidebar-show').addEventListener('click', () => { setSidebarHidden(false); document.getElementById('sidebar-hide').focus(); });
document.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === '\\') { e.preventDefault(); setSidebarHidden(!sbState.hidden); }
});

// ---------------------------------------------------------------- Toast
let toastTimer;
export function toast(msg, ms = 3200) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.dataset.show = 'true';
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.dataset.show = 'false'; }, ms);
}

/** Statuszeile unten in der Seitenleiste: state ok|warn|crit|unknown + Text. */
export function setStatus(state, text) {
  const box = clear(document.getElementById('sidebar-status'));
  box.append(el('span.status-dot', { class: `status-dot status-dot--${state}`, 'aria-hidden': 'true' }), el('span', text));
}

// ---------------------------------------------------------------- Navigation: Projekte und Foliensätze
const OPEN_KEY = 'fundus-nav-open';
const openSet = (() => { try { return new Set(JSON.parse(localStorage.getItem(OPEN_KEY) || '[]')); } catch { return new Set(); } })();
const saveOpen = () => { try { localStorage.setItem(OPEN_KEY, JSON.stringify([...openSet])); } catch { /* privat */ } };
const arr = (v) => (Array.isArray(v) ? v : []);

function hashSel() {
  const q = new URLSearchParams(location.hash.split('?')[1] ?? '');
  return { p: q.get('p'), set: q.get('set') };
}
const projectHref = (p) => `#folien?p=${encodeURIComponent(p)}`;
const setHref = (p, id) => `#folien?p=${encodeURIComponent(p)}&set=${encodeURIComponent(id)}`;

const SORTS = [
  { value: 'name_asc', label: 'A–Z' }, { value: 'name_desc', label: 'Z–A' },
  { value: 'created_desc', label: 'Neueste zuerst' }, { value: 'created_asc', label: 'Älteste zuerst' },
];
const byName = (a, b) => (a.title || a.project).localeCompare(b.title || b.project, 'de', { sensitivity: 'base', numeric: true });
function sorted(ps, sort) {
  const out = [...ps];
  if (sort === 'name_desc') out.sort((a, b) => byName(b, a));
  else if (sort === 'created_desc') out.sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? '') || byName(a, b));
  else if (sort === 'created_asc') out.sort((a, b) => (a.created_at ?? '\uffff').localeCompare(b.created_at ?? '\uffff') || byName(a, b));
  else out.sort(byName);
  return out;
}
function sortControl() {
  const cur = overview?.project_sort ?? 'name_asc';
  const sel = el('select.pnav__sort', { 'aria-label': 'Projekte sortieren', title: 'Reihenfolge der Projekte (gilt auf allen Geräten)' },
    SORTS.map((o) => el('option', { value: o.value }, o.label)));
  sel.value = cur;
  sel.addEventListener('change', async () => {
    const prev = overview.project_sort;
    overview.project_sort = sel.value; buildNav();
    document.querySelector('.pnav__sort')?.focus();
    try {
      const r = await fetch('/api/portal/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_sort: sel.value }) });
      if (!r.ok) throw new Error((await r.json().catch(() => null))?.error ?? `HTTP ${r.status}`);
    } catch (e) {
      overview.project_sort = prev; buildNav();
      toast(`Sortierung nicht gespeichert: ${e.message}`, 5000);
    }
  });
  return sel;
}

function buildNav() {
  const nav = clear(document.getElementById('nav'));
  const dec = fbCounts?.decisions ?? 0;
  // Aufbau der Leiste: Foliensuche (wie ein Suchfeld) mit den gespeicherten Suchen darunter · Projekte ·
  // Bibliothek (Mediathek, Datensätze, Archiv, Papierkorb) · Überblick (Prüfungen, Fundus-Statistik)
  nav.append(el('a.nav__link.nav__search.nav__link--search', { href: '#suche', data: { view: 'suche' }, title: 'Semantische Suche über alle Folien' },
    icon('ic-search'), el('span.nav__search-text', 'Folien suchen …')));
  const ss = arr(saved);
  if (ss.length) {
    nav.append(el('ul.snav', { role: 'list', 'aria-label': 'Gespeicherte Suchen' }, ss.map((c) => el('li',
      el('a.nav__link.snav__link', { href: `#suche?c=${c.id}`, data: { cid: String(c.id) }, title: `Gespeicherte Suche: ${c.name}${c.question ? ` – „${c.question}“` : ''} (${c.ref})` },
        icon('ic-search'), el('span', c.name), el('span.nav__count', fmt.int(c.slides ?? 0)))))));
  }
  nav.append(el('div.pnav__head', el('span.sidebar__label', { id: 'pnav-label' }, 'Projekte'), overview?.projects?.length > 1 ? sortControl() : null));
  const ps = sorted(arr(overview?.projects), overview?.project_sort);
  const tree = el('ul.pnav', { role: 'list', 'aria-labelledby': 'pnav-label' });
  if (!ps.length) nav.append(el('p.small.muted.pnav__empty', overview ? 'Noch keine Projekte.' : 'Lade Projekte …'));
  for (const p of ps) {
    const sets = arr(p.sets);
    const subId = `pnav-sets-${p.project}`;
    const toggle = el('button.pnav__toggle', { type: 'button', 'aria-expanded': 'false', 'aria-controls': subId,
      'aria-label': `Foliensätze von ${p.title || p.project}` }, el('span.pnav__chev', { 'aria-hidden': 'true' }));
    const link = el('a.nav__link.pnav__link', { href: projectHref(p.project), data: { project: p.project }, title: p.title ? `${p.title} (${p.project})` : p.project },
      icon('ic-folder'), el('span', p.title || p.project), el('span.nav__count', fmt.int(p.exhibits ?? 0)));
    // Einstellungen (Name, Sichtbarkeit, Archiv) nur über die Schaltfläche „Einstellungen“ der Folienseite, kein Zahnrad in der Seitenleiste
    const sub = el('ul.pnav__sets', { id: subId, role: 'list' }, sets.map((s) => el('li.pnav__row',
      el('a.nav__link.pnav__set', { href: setHref(p.project, s.id), data: { project: p.project, set: String(s.id) }, title: `${s.title} (${s.slug})` },
        icon('ic-slides'), el('span', s.title || s.slug), el('span.nav__count', fmt.int(s.exhibits ?? 0))))));
    const li = el('li.pnav__item', { data: { project: p.project } },
      el('div.pnav__row', toggle, link), sub);
    toggle.addEventListener('click', () => {
      const open = toggle.getAttribute('aria-expanded') !== 'true';
      if (open) openSet.add(p.project); else openSet.delete(p.project);
      saveOpen(); setOpen(li, open);
    });
    tree.append(li);
  }
  if (ps.length) nav.append(tree);
  const group = (label, ...links) => { nav.append(el('span.sidebar__label.nav__group', label)); links.forEach((l) => nav.append(l)); };
  group('Bibliothek',
    el('a.nav__link.nav__link--photos', { href: '#mediathek', title: 'Mediathek: Fotos und Videos der Feldforschung' },
      icon('ic-photo'), el('span', 'Mediathek')),
    el('a.nav__link.nav__link--data', { href: '#daten', title: 'Datensätze für Analysen – gespeichert, beschrieben, abfragbar' },
      icon('ic-table'), el('span', 'Datensätze')),
    el('a.nav__link.nav__link--archive', { href: '#archiv', title: 'Archivierte Folien – auffindbar, aber nicht mehr im Projekt gelistet' },
      icon('ic-archive'), el('span', 'Archiv'), archiveN ? el('span.nav__count', fmt.int(archiveN)) : null),
    el('a.nav__link.nav__link--trash', { href: '#papierkorb', title: 'Gelöschte Folien, Foliensätze und Projekte – nach 30 Tagen endgültig weg' },
      icon('ic-trash'), el('span', 'Papierkorb'), trashN ? el('span.nav__count', fmt.int(trashN)) : null));
  group('Austausch',
    el('a.nav__link.nav__link--received', { href: '#erhalten', title: 'Foliensätze, die dir andere Fundus-Installationen geschickt haben' },
      icon('ic-inbox'), el('span', 'Erhaltene Foliensätze'), receivedNew ? el('span.nav__count.badge.badge--accent', { title: `${receivedNew} neu` }, fmt.int(receivedNew)) : null),
    el('a.nav__link.nav__link--sent', { href: '#gesendet', title: 'Gesendete oder als Paket exportierte Foliensätze' },
      icon('ic-send'), el('span', 'Gesendet')));
  group('Überblick',
    el('a.nav__link.nav__link--reviews', { href: '#pruefungen', data: { view: 'pruefungen' }, title: 'Freigegebene Prüfungen und Feedback' },
      icon('ic-check'), el('span', 'Prüfungen'),
      dec ? el('span.nav__count.badge.badge--danger', { title: `${dec} Punkte warten auf deine Entscheidung` }, String(dec))
        : fbCounts?.open ? el('span.nav__count', { title: 'offene Feedback-Punkte' }, fmt.int(fbCounts.open)) : null),
    el('a.nav__link.nav__link--stats', { href: '#statistik', title: 'Kennzahlen des Evidenzarchivs (wie im Dashboard)' },
      icon('ic-stats'), el('span', 'Fundus-Statistik')));
  group('Verwaltung',
    el('a.nav__link.nav__link--admin', { href: '#verwaltung', title: 'Einstellungen dieses Fundus: Fundus Cloud (verschlüsselt teilen), Kontakte' },
      icon('ic-gear'), el('span', 'Verwaltung')));
  syncNav();
}
function setOpen(li, open) {
  li.dataset.open = String(open);
  li.querySelector('.pnav__toggle').setAttribute('aria-expanded', String(open));
}
/** Markiert Projekt und Foliensatz der aktuellen Auswahl (auch nach replaceState aus der Ansicht). */
export function syncNav() {
  const { p, set } = hashSel();
  const inReviews = location.hash.startsWith('#pruefungen');
  const inSearch = location.hash.startsWith('#suche');
  const cid = inSearch ? new URLSearchParams(location.hash.split('?')[1] ?? '').get('c') : null;
  document.querySelector('.nav__link--reviews')?.classList.toggle('active', inReviews);
  document.querySelector('.nav__link--search')?.classList.toggle('active', inSearch && !cid);
  document.querySelectorAll('.snav__link').forEach((a) => a.classList.toggle('active', a.dataset.cid === cid));
  const inStats = location.hash.startsWith('#statistik') || location.hash.startsWith('#datenqualitaet');
  document.querySelector('.nav__link--stats')?.classList.toggle('active', inStats);
  const inPhotos = location.hash.startsWith('#mediathek') || location.hash.startsWith('#fotos');
  const inTrash = location.hash.startsWith('#papierkorb');
  const inArchive = location.hash.startsWith('#archiv');
  const inData = location.hash.startsWith('#daten');
  const inAdmin = location.hash.startsWith('#verwaltung');
  const inReceived = location.hash.startsWith('#erhalten');
  const inSent = location.hash.startsWith('#gesendet');
  document.querySelector('.nav__link--received')?.classList.toggle('active', inReceived);
  document.querySelector('.nav__link--sent')?.classList.toggle('active', inSent);
  document.querySelector('.nav__link--data')?.classList.toggle('active', inData);
  document.querySelector('.nav__link--admin')?.classList.toggle('active', inAdmin);
  document.querySelector('.nav__link--archive')?.classList.toggle('active', inArchive);
  document.querySelector('.nav__link--trash')?.classList.toggle('active', inTrash);
  document.querySelector('.nav__link--photos')?.classList.toggle('active', inPhotos);
  if (inReviews || inSearch || inStats || inPhotos || inTrash || inArchive || inData || inAdmin || inReceived || inSent) {
    document.querySelectorAll('.pnav__link, .pnav__set').forEach((a) => { a.classList.remove('active', 'pnav__link--within'); a.removeAttribute('aria-current'); });
    document.querySelectorAll('.pnav__item').forEach((li) => setOpen(li, openSet.has(li.dataset.project)));
    return;
  }
  const active = p ?? document.querySelector('.pnav__item')?.dataset.project ?? null;
  document.querySelectorAll('.pnav__item').forEach((li) => setOpen(li, li.dataset.project === active || openSet.has(li.dataset.project)));
  document.querySelectorAll('.pnav__link, .pnav__set').forEach((a) => {
    const on = a.dataset.project === active && (a.classList.contains('pnav__set') ? a.dataset.set === set : !set);
    a.classList.toggle('active', on);
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    if (a.classList.contains('pnav__link')) a.classList.toggle('pnav__link--within', a.dataset.project === active && !!set);
  });
  // schmal: Projekte als waagrechte Leiste – das aktive in Sicht rollen
  if (matchMedia('(max-width: 760px)').matches) {
    const a = document.querySelector(`.pnav__link[data-project="${CSS.escape(active ?? '')}"]`);
    const bar = a?.closest('.pnav');
    if (a && bar) bar.scrollLeft = Math.max(0, a.offsetLeft - bar.offsetLeft - 16);
  }
}
/** Projekte (Namen, Foliensätze, Zahlen) neu laden, z. B. nach einer Umbenennung. */
export async function refreshNav() {
  try {
    const [r, c, s, t, a, rc] = await Promise.all([fetch('/api/exhibits', { cache: 'no-store' }), fetch('/api/feedback/counts', { cache: 'no-store' }).catch(() => null),
      fetch('/api/collections', { cache: 'no-store' }).catch(() => null), fetch('/api/trash', { cache: 'no-store' }).catch(() => null),
      fetch('/api/archive?limit=1', { cache: 'no-store' }).catch(() => null), fetch('/api/received', { cache: 'no-store' }).catch(() => null)]);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    overview = await r.json();
    fbCounts = c?.ok ? await c.json() : fbCounts;
    saved = s?.ok ? (await s.json()).saved_searches : saved;
    trashN = t?.ok ? ((await t.json()).entries ?? 0) : trashN;
    archiveN = a?.ok ? ((await a.json()).total ?? 0) : archiveN;
    receivedNew = rc?.ok ? ((await rc.json()).new ?? 0) : receivedNew;
  } catch { if (!overview) setStatus('crit', 'Fundus-Server antwortet nicht'); }
  buildNav();
}

// ---------------------------------------------------------------- Router
function currentView() {
  const h = (location.hash || '#folien').slice(1).split('?')[0];
  return VIEWS.find((v) => v.id === h) ?? VIEWS[0];
}
function render() {
  const v = currentView();
  if (typeof cleanup === 'function') cleanup();
  const root = clear(document.getElementById('view'));
  cleanup = v.view.render(root, { toast, setStatus, syncNav, refreshNav });
  document.title = `${v.label} · Fundus-Portal`;
  syncNav();
}
window.addEventListener('hashchange', render);

// ---------------------------------------------------------------- Selbst aktualisieren
// Jede Minute (und beim Zurückkehren in den Tab): neue Portal-Version (index.html nennt neue ?v=) → neu laden; neue Daten
// (Projekte, Folienzahlen, Prüfungen, Papierkorb, Archiv – was die Seitenleiste lädt) → Ansicht neu zeichnen. Beides nur,
// wenn gerade niemand arbeitet (kein Dialog offen, kein Eingabefeld aktiv, 30 s keine Eingabe); sonst ein Hinweis unten.
const verOf = (u) => (u ?? '').match(/[?&]v=(\d+)/)?.[1] ?? null;
const LOADED = { js: verOf(document.querySelector('script[src*="main.js"]')?.getAttribute('src')),
  css: verOf(document.querySelector('link[href*="portal.css"]')?.getAttribute('href')) };
let lastInput = Date.now();
for (const ev of ['pointerdown', 'keydown', 'wheel', 'touchstart', 'input']) addEventListener(ev, () => { lastInput = Date.now(); }, { passive: true, capture: true });
const idle = () => Date.now() - lastInput > 30000 && !document.hidden && !document.querySelector('dialog[open]')
  && !document.activeElement?.closest?.('input, textarea, select, [contenteditable]');
const dataSig = () => JSON.stringify([arr(overview?.projects).map((p) => [p.project, p.exhibits, p.versions, p.updated_at, p.archived,
  arr(p.sets).map((x) => [x.id, x.exhibits, x.updated_at])]), fbCounts?.open, fbCounts?.decisions, arr(saved).map((c) => [c.id, c.slides]), trashN, archiveN, receivedNew]);
let pending = null;   // 'code' | 'data'
const banner = el('div.upd-banner', { role: 'status', hidden: true });
document.body.append(banner);
function redraw() {
  const y = window.scrollY;
  render();
  for (const t of [300, 1200]) setTimeout(() => { if (Math.abs(window.scrollY - y) > 2) window.scrollTo(0, y); }, t);
}
function apply() {
  const what = pending; pending = null; banner.hidden = true;
  if (what === 'code') location.reload(); else if (what === 'data') redraw();
}
function offer(what) {
  if (pending === 'code') return;
  pending = what;
  if (idle()) { apply(); return; }
  const go = el('button.btn.btn--primary.btn--small', { type: 'button' }, what === 'code' ? 'Neu laden' : 'Aktualisieren');
  const later = el('button.btn.btn--ghost.btn--small', { type: 'button', 'aria-label': 'Später' }, '✕');
  go.addEventListener('click', apply);
  later.addEventListener('click', () => { banner.hidden = true; });
  banner.replaceChildren(el('span', what === 'code' ? 'Es gibt eine neue Version des Portals.' : 'In Fundus hat sich etwas geändert.'), go, later);
  // ein offener Dialog (Grossansicht) liegt in der obersten Ebene: der Hinweis muss dort hinein, sonst ist er nicht klickbar
  const open = document.querySelectorAll('dialog[open]');
  (open[open.length - 1] ?? document.body).append(banner);
  banner.hidden = false;
}
async function checkUpdates() {
  if (document.hidden) return;
  if (pending && idle()) { apply(); return; }
  if (pending && !banner.isConnected) offer(pending);    // der Dialog mit dem Hinweis wurde geschlossen
  try {
    const html = await fetch('index.html', { cache: 'no-store' }).then((r) => (r.ok ? r.text() : ''));
    const js = html.match(/main\.js\?v=(\d+)/)?.[1], css = html.match(/portal\.css\?v=(\d+)/)?.[1];
    if ((js && LOADED.js && js !== LOADED.js) || (css && LOADED.css && css !== LOADED.css)) { offer('code'); return; }
  } catch { /* Server kurz weg: nächstes Mal */ }
  const before = dataSig();
  await refreshNav();
  if (dataSig() !== before) offer('data');
}
setInterval(checkUpdates, 60000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) checkUpdates(); });

buildNav();
refreshNav();
render();
