/**
 * Gemeinsame Bausteine der Ansicht „Datensätze“ (daten.js, daten-katalog.js, daten-abdeckung.js, daten-archiv.js,
 * daten-detail.js): API-Aufruf, Katalog laden (mit kurzem Zwischenspeicher für den Wechsel zwischen den Reitern),
 * Bezeichnungen, Merkmale je Datensatz (Facetten), Abzeichen, kleine Status-Symbole, Hochladen-Dialog, Tooltip.
 *
 * Katalog: GET /api/datasets/catalog?removed=0|1 → {stats, facets, datasets}. Antwortet der Server noch nicht damit
 * (älterer Fundus-Server), baut normalize() denselben Aufbau aus GET /api/datasets?limit=500 – dann fehlen Nutzung,
 * Lebenszyklus und Archivangaben (data.legacy = true).
 */
import { el, svgEl, segmented, badge } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';

export const arr = (v) => (Array.isArray(v) ? v : []);
export const kids = (...a) => a.filter((x) => x !== null && x !== undefined && x !== false);
export const hp = () => new URLSearchParams(location.hash.split('?')[1] ?? '');
export const sqlDate = (v) => (v ? String(v).replace(' ', 'T') : null);
/** replaceChildren ohne null/false (die DOM-Methode schriebe sonst „null“ als Text). */
export function put(node, ...c) { node.replaceChildren(...c.flat(Infinity).filter((x) => x !== null && x !== undefined && x !== false)); return node; }
export const cell = (v) => (v === null || v === undefined ? el('span.muted', '–') : String(v));

export async function api(path, body) {
  const r = await fetch(`/api/datasets${path}`, body === undefined ? { cache: 'no-store' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error) { const e = new Error(d.error ?? `HTTP ${r.status}`); e.status = r.status; throw e; }
  return d;
}

// ---------------------------------------------------------------- Bezeichnungen
export const VIS = [{ value: 'public', label: 'öffentlich' }, { value: 'internal', label: 'intern (Kunde/eigene)' }];
export const FREQ = [['', '– kein Rhythmus –'], ['annual', 'jährlich'], ['semiannual', 'halbjährlich'], ['quarterly', 'vierteljährlich'], ['monthly', 'monatlich'], ['irregular', 'unregelmässig'], ['once', 'einmalig']];
export const DUE = { overdue: 'Update fällig', never_checked: 'Update: noch nie geprüft', stale_check: 'Update: lange nicht geprüft' };
export const LABELS = {
  frequency: { ...Object.fromEntries(FREQ.slice(1)), none: 'ohne Rhythmus' },
  visibility: { public: 'öffentlich', internal: 'intern' },
  lifecycle: { active: 'aktiv', superseded: 'ersetzt', withdrawn: 'zurückgezogen' },
  holding: { stored: 'in Fundus gespeichert', reference: 'Referenz (bleibt am Ort)' },
  kind: { raw: 'Ausgangsdaten', derived: 'abgeleitet (aus anderen Datensätzen)', crosswalk: 'Umschlüsselung (Code-Zuordnung)', text: 'Text mit Fassungen' },
  act_type: { law: 'Gesetz', regulation: 'Verordnung (EU-Verordnung)', directive: 'Richtlinie (EU-Richtlinie)', ordinance: 'Verordnung (Ausführungsrecht)',
    guideline: 'Leitlinie', standard: 'Norm / Standard', terms: 'Nutzungsbedingungen', policy: 'Policy eines Anbieters', other: 'anderer Text' },
  size: { lt10mb: 'unter 10 MB', lt100mb: '10 bis 100 MB', lt1gb: '100 MB bis 1 GB', ge1gb: '1 GB und mehr' },
  status: { new: 'wird beschrieben', described: 'beschrieben', failed: 'Beschreibung fehlgeschlagen' },
  state: { update_due: 'Update fällig', dormant: 'ruhend (90 Tage ungenutzt)', stale: 'Eingangsdaten neuer', used: 'in den letzten 30 Tagen genutzt', slides: 'auf Folien verwendet' },
};
export const LIFE_TONE = { superseded: 'warn', withdrawn: 'danger' };

/** Facetten in der Reihenfolge des Filterfelds: key, Überschrift, Werte je Datensatz, offen? */
export const FACETS = [
  { key: 'project', title: 'Projekt', open: true, of: (d) => (d.project ? [d.project] : []) },
  { key: 'topic', title: 'Thema', open: true, of: (d) => arr(d.tags?.topic) },
  { key: 'region', title: 'Region', open: true, of: (d) => arr(d.tags?.region) },
  { key: 'industry', title: 'Branche', of: (d) => arr(d.tags?.industry) },
  { key: 'publisher_group', title: 'Herausgeber', of: (d) => (d.publisher_group ? [d.publisher_group] : []) },
  { key: 'state', title: 'Zustand', open: true, of: (d) => kids(d.update_due ? 'update_due' : null, d.dormant ? 'dormant' : null,
    arr(d.stale_inputs).length ? 'stale' : null, d.uses_30d > 0 ? 'used' : null, d.slides_n > 0 ? 'slides' : null) },
  { key: 'lifecycle', title: 'Lebenszyklus', of: (d) => [d.lifecycle || 'active'] },
  { key: 'frequency', title: 'Neue Ausgaben', of: (d) => [d.frequency || 'none'] },
  { key: 'kind', title: 'Art', of: (d) => [kindOf(d)] },
  { key: 'jurisdiction', title: 'Rechtsraum', of: (d) => (d.kind === 'text' && d.jurisdiction ? [d.jurisdiction] : []) },
  { key: 'act_type', title: 'Textart', of: (d) => (d.kind === 'text' && d.act_type ? [d.act_type] : []) },
  { key: 'holding', title: 'Ablage', of: (d) => [d.holding || 'stored'] },
  { key: 'visibility', title: 'Sichtbarkeit', of: (d) => [d.visibility || 'public'] },
  { key: 'size', title: 'Grösse', of: (d) => [d.size_class || sizeClass(d.bytes)] },
  { key: 'status', title: 'Beschreibung', of: (d) => [d.status || 'described'] },
];
export const kindOf = (d) => (d.kind === 'text' ? 'text' : d.kind === 'crosswalk' ? 'crosswalk' : (d.inputs_n ?? 0) > 0 ? 'derived' : 'raw');
export const isText = (d) => d?.kind === 'text';
/** „gültig seit 27.07.2026“ / „gültig 12.07.2024 bis 26.07.2026“ (valid_to ist der letzte Tag). */
export function validity(v) {
  if (!v?.valid_from) return v?.valid_to ? `gültig bis ${fmt.date(v.valid_to)}` : 'Gültigkeit unbekannt';
  return v.valid_to ? `gültig ${fmt.date(v.valid_from)} bis ${fmt.date(v.valid_to)}` : `gültig seit ${fmt.date(v.valid_from)}`;
}
export const sizeClass = (b) => { const x = b ?? 0; return x < 1e7 ? 'lt10mb' : x < 1e8 ? 'lt100mb' : x < 1e9 ? 'lt1gb' : 'ge1gb'; };
const SIZE_ORDER = ['lt10mb', 'lt100mb', 'lt1gb', 'ge1gb'];
export const facetOrder = (key) => (key === 'size' ? (a, b) => SIZE_ORDER.indexOf(a.value) - SIZE_ORDER.indexOf(b.value) : null);

/** Bezeichnung eines Facettenwerts: feste Liste, sonst Bezeichnung vom Server (Projekt-Titel, Vokabular), sonst der Wert. */
export function facetLabel(cat, key, value) {
  const fixed = LABELS[key]?.[value];
  if (fixed) return fixed;
  const f = arr(cat?.facets?.[key]).find((x) => x.value === value);
  return f?.label || prettify(value);
}
const prettify = (v) => (/^[a-z]{2}$/.test(v) ? v.toUpperCase() : String(v).replace(/-/g, ' '));

// ---------------------------------------------------------------- Katalog laden
let cache = null;   // {key, at, promise}
export function loadCatalog({ removed = false, force = false } = {}) {
  const key = removed ? 'removed' : 'active';
  if (!force && cache && cache.key === key && Date.now() - cache.at < 45000) return cache.promise;
  const promise = (async () => {
    try { return enrich(await api(`/catalog?removed=${removed ? 1 : 0}`)); }
    catch (e) {
      if (e.status !== 404 && e.status !== 405 && !/not found|HTTP 4/i.test(e.message)) throw e;
      const d = await api(`?limit=500${removed ? '&removed=1' : ''}`);
      return enrich(normalize(d));
    }
  })();
  cache = { key, at: Date.now(), promise };
  promise.catch(() => { if (cache?.promise === promise) cache = null; });
  return promise;
}
export const dropCatalog = () => { cache = null; };

const YEAR = /(?<!\d)(1[6-9]\d\d|20\d\d)(?!\d)/g;
export function periodYears(p) {
  const ys = [...String(p ?? '').matchAll(YEAR)].map((m) => Number(m[1]));
  return ys.length ? [Math.min(...ys), Math.max(...ys)] : [null, null];
}
function pubGroup(p) {
  if (!p) return null;
  if (p.startsWith('Jane Doe')) return 'Eigene Daten (Jane Doe)';
  return p.split(/ \(|, | – | \/ /)[0].trim();
}

/** Alte Liste (GET /api/datasets) in den Katalog-Aufbau bringen. */
function normalize(d) {
  const ds = arr(d.datasets).map((x) => {
    const [pf, pt] = periodYears(x.period);
    return { ...x, period_from: pf, period_to: pt, size_class: sizeClass(x.bytes), publisher_group: pubGroup(x.publisher),
      lifecycle: 'active', holding: 'stored', dormant: false, uses_30d: null, seen_30d: null, last_used: null,
      inputs_n: null, used_by_n: null, slides_n: null, bytes_disk: null };
  });
  const s = d.stats ?? {};
  return { legacy: true, datasets: ds, facets: {},
    stats: { datasets: ds.length, tables: ds.reduce((a, x) => a + (x.tables ?? 0), 0), rows: s.rows ?? ds.reduce((a, x) => a + (x.rows ?? 0), 0),
      bytes_original: s.bytes ?? null, bytes_parquet: null, bytes_reference: null, internal: ds.filter((x) => x.visibility === 'internal').length,
      update_due: ds.filter((x) => x.update_due).length, dormant: null, superseded: null, withdrawn: null, references: null, fixity: null, uses_30d: null } };
}

/** Suchtext je Datensatz einmal vorberechnen (Titel, Zusammenfassung, Schlagworte, Herausgeber, Kennung, Projekt). */
function enrich(cat) {
  const byId = new Map();
  for (const d of arr(cat.datasets)) {
    if (d.period_from === undefined) [d.period_from, d.period_to] = periodYears(d.period);
    d.size_class ??= sizeClass(d.bytes);
    d._hay = [d.title, d.summary, d.summary_en, arr(d.keywords).join(' '), d.publisher, d.publisher_group, d.ref, d.slug, d.project, d.period, d.geo,
      d.identifier, d.jurisdiction, d.issuer, d.in_force?.version_label,
      Object.values(d.tags ?? {}).flat().join(' '), arr(d.code_systems).join(' ')].filter(Boolean).join(' \n ').toLowerCase();
    d._title = String(d.title ?? '').toLowerCase();
    byId.set(d.id, d);
  }
  cat.byId = byId;
  return cat;
}

// ---------------------------------------------------------------- Abzeichen und Symbole
/** Abzeichen eines Datensatzes (Karte, Steckbrief). */
export function badges(d, cat) {
  const succ = d.superseded_by ? cat?.byId?.get(d.superseded_by) : null;
  return kids(
    d.visibility === 'internal' ? badge('intern', 'warn') : null,
    d.update_due ? badge(DUE[d.update_due] ?? 'Update fällig', d.update_due === 'overdue' ? 'danger' : 'warn') : null,
    d.lifecycle === 'superseded' ? badge(succ ? `ersetzt durch dataset:${succ.id}` : 'ersetzt', 'warn') : null,
    d.lifecycle === 'withdrawn' ? badge('zurückgezogen', 'danger') : null,
    d.dormant ? badge('ruhend', 'outline') : null,
    d.holding === 'reference' ? badge('Referenz', 'accent') : null,
    isText(d) ? badge('Text mit Fassungen', 'accent') : null,
    d.kind === 'crosswalk' ? badge('Umschlüsselung', 'outline') : null,
    (d.inputs_n ?? arr(d.inputs).length) > 0 ? badge('abgeleitet', 'outline') : null,
    arr(d.stale_inputs).length ? badge('Eingangsdaten neuer', 'warn') : null,
    d.status === 'new' ? badge('wird beschrieben', 'outline') : d.status === 'failed' ? badge('Beschreibung fehlgeschlagen', 'danger') : null,
    d.removed_at ? badge(`gelöscht ${fmt.date(sqlDate(d.removed_at))}`, 'danger') : null);
}

const ICON_PATHS = {
  lock: 'M4.5 7V5.2a3.5 3.5 0 0 1 7 0V7M3.5 7h9v6.5h-9z',
  clock: 'M8 2.2a5.8 5.8 0 1 0 0 11.6A5.8 5.8 0 0 0 8 2.2zM8 4.8V8l2.2 1.4',
  moon: 'M12.8 9.6A5.4 5.4 0 0 1 6.4 3.2a5.4 5.4 0 1 0 6.4 6.4z',
  arrow: 'M2.5 8h10M9 4.5 12.5 8 9 11.5',
  ban: 'M8 2.2a5.8 5.8 0 1 0 0 11.6A5.8 5.8 0 0 0 8 2.2zM3.9 3.9l8.2 8.2',
  link: 'M6.6 9.4l2.8-2.8M7 4.6l1.2-1.2a2.6 2.6 0 0 1 3.7 3.7L10.7 8.3M9 11.4l-1.2 1.2a2.6 2.6 0 0 1-3.7-3.7l1.2-1.2',
  swap: 'M2.5 5.5h10L10 3M13.5 10.5h-10L6 13',
  branch: 'M4.5 2.5v11M4.5 9c0-3 7-2 7-5.5M11.5 3.5v0',
  spin: 'M8 2.5a5.5 5.5 0 1 0 5.5 5.5',
  alert: 'M8 2.2 14 13H2zM8 6.5v3M8 11.2v.1',
  check: 'M3 8.5l3.2 3L13 4.5',
  text: 'M4 2.5h5.5L12 5v8.5H4zM9.5 2.5V5H12M6 8h4M6 10.5h4',
};
export function glyph(name, title, tone) {
  const s = svgEl('svg', { class: `dk-ic${tone ? ` dk-ic--${tone}` : ''}`, viewBox: '0 0 16 16', role: 'img', 'aria-label': title });
  s.append(svgEl('title', {}, title), svgEl('path', { d: ICON_PATHS[name], fill: 'none', stroke: 'currentColor', 'stroke-width': '1.5', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
  return s;
}
/** Status-Symbole für die Tabellenansicht (jedes mit Titel). */
export function glyphs(d) {
  return kids(
    d.visibility === 'internal' ? glyph('lock', 'intern', 'warn') : null,
    d.update_due ? glyph('clock', DUE[d.update_due] ?? 'Update fällig', d.update_due === 'overdue' ? 'crit' : 'warn') : null,
    d.lifecycle === 'superseded' ? glyph('arrow', `ersetzt${d.superseded_by ? ` durch dataset:${d.superseded_by}` : ''}`, 'warn') : null,
    d.lifecycle === 'withdrawn' ? glyph('ban', 'zurückgezogen', 'crit') : null,
    d.dormant ? glyph('moon', 'ruhend – 90 Tage nicht genutzt') : null,
    d.holding === 'reference' ? glyph('link', 'Referenz – Rohdaten bleiben an ihrem Ort', 'accent') : null,
    isText(d) ? glyph('text', `Text mit Fassungen${d.version > 1 ? ` (${d.version} Fassungen)` : ''}`, 'accent') : null,
    d.kind === 'crosswalk' ? glyph('swap', 'Umschlüsselung (Code-Zuordnung)') : null,
    (d.inputs_n ?? 0) > 0 ? glyph('branch', `abgeleitet aus ${d.inputs_n} ${d.inputs_n === 1 ? 'Datensatz' : 'Datensätzen'}`) : null,
    d.status === 'new' ? glyph('spin', 'wird beschrieben') : d.status === 'failed' ? glyph('alert', 'Beschreibung fehlgeschlagen', 'crit') : null);
}

/** Kurzform grosser Zahlen: 172.6 Mio., 1.2 Mrd. */
export function compact(n) {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '–';
  if (Math.abs(n) >= 1e9) return `${fmt.num1(n / 1e9)} Mrd.`;
  if (Math.abs(n) >= 1e6) return `${fmt.num1(n / 1e6)} Mio.`;
  return fmt.int(n);
}

/** „18’863 Zeilen · 3 Tabellen · 17.6 MB“ */
export function figures(d) {
  if (isText(d)) {
    return kids(d.version ? `${fmt.int(d.version)} ${d.version === 1 ? 'Fassung' : 'Fassungen'}` : null,
      d.in_force ? `in Kraft: Fassung ${d.in_force.n}, ${validity(d.in_force)}` : 'keine Fassung heute gültig',
      d.bytes != null ? fmt.bytes(d.bytes) : null).join(' · ');
  }
  return kids(d.rows != null ? `${fmt.int(d.rows)} Zeilen` : null, d.tables > 1 ? `${fmt.int(d.tables)} Tabellen` : d.tables === 1 ? '1 Tabelle' : null,
    d.bytes != null ? fmt.bytes(d.bytes) : null).join(' · ');
}
export function periodText(d) {
  if (d.period_from && d.period_to) return d.period_from === d.period_to ? String(d.period_from) : `${d.period_from}–${d.period_to}`;
  return d.period || null;
}

// ---------------------------------------------------------------- Tooltip (#tooltip aus index.html)
export function tip(evt, lines) {
  const t = document.getElementById('tooltip');
  if (!t) return;
  if (!lines) { t.dataset.show = 'false'; return; }
  put(t, ...lines.map((x, i) => el(i === 0 ? 'div.tooltip__title' : 'div.tooltip__note', x)));
  const pad = 14;
  const w = t.offsetWidth || 240;
  const h = t.offsetHeight || 40;
  const x = Math.min(window.innerWidth - w - 8, evt.clientX + pad);
  const y = evt.clientY + h + pad > window.innerHeight ? evt.clientY - h - pad : evt.clientY + pad;
  t.style.left = `${Math.max(8, x)}px`; t.style.top = `${Math.max(8, y)}px`;
  t.dataset.show = 'true';
}
export const hideTip = () => tip(null, null);

/** Text in die Zwischenablage (mit Rückfall für http ohne Clipboard-API). */
export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* Rückfall */ }
  const ta = el('textarea', { style: { position: 'fixed', opacity: '0' } });
  ta.value = text; document.body.append(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  return ok;
}

// ---------------------------------------------------------------- Hochladen
export function uploadDialog(ctx, version, onDone) {
  const dlg = el('dialog.dialog.ds-upload');
  const files = el('input', { type: 'file', multiple: true, accept: '.csv,.tsv,.txt,.xlsx,.xlsm,.json,.jsonl,.ndjson,.parquet,.pdf,.md,.docx,.doc,.html,.xhtml,.xml,.pptx' });
  const title = el('input.input', { type: 'text', placeholder: 'Titel (sonst aus dem Dateinamen)' });
  const descr = el('textarea.input', { rows: 3, placeholder: 'Was ist das, woher, wie erhoben? (hilft dem Modell und der Suche)' });
  const pub = el('input.input', { type: 'text', placeholder: 'Herausgeber, z. B. BFS' });
  const urlI = el('input.input', { type: 'url', placeholder: 'URL der Quelle' });
  const proj = el('input.input', { type: 'text', placeholder: 'Projekt (optional)' });
  let visibility = 'public';
  const vis = segmented(VIS, visibility, (v) => { visibility = v; }, 'Sichtbarkeit');
  const note = el('input.input', { type: 'text', placeholder: 'Notiz zu dieser Version' });
  const bar = el('progress.ph-progress', { max: 100, value: 0, hidden: true });
  const msg = el('p.small.muted', 'Tabellen: CSV, TSV, Excel (jedes Blatt), JSON, Parquet – hier bis 2 GB je Datei; grössere über eine Claude-Session (fundus_dataset_add) oder fundus datasets add im Terminal. Begleitdokumente (Codebuch, Methodik): PDF, Markdown, Word. Mehrere Dateien = ein Datensatz.');
  const ok = el('button.btn.btn--primary', { type: 'button' }, 'Hochladen');
  const cancel = el('button.btn', { type: 'button' }, 'Schliessen');
  const row = (label, c) => el('label.ph-uprow', el('span.label', label), c);
  // neue Fassung eines Texts mit Fassungen: Gültigkeit und Bezeichnung (gültig ab ist Pflicht)
  const text = isText(version);
  const vFrom = el('input.input', { type: 'date', 'aria-label': 'gültig ab' });
  const vTo = el('input.input', { type: 'date', 'aria-label': 'gültig bis (letzter Tag, leer = gilt weiter)' });
  const vLabel = el('input.input', { type: 'text', placeholder: 'z. B. konsolidierte Fassung vom 01.01.2027' });
  const vSrc = el('input.input', { type: 'url', placeholder: 'URL dieser Fassung (EUR-Lex, Fedlex …)' });
  if (text) msg.textContent = 'Die Dateien (PDF, HTML, XHTML, XML) werden so gespeichert, wie sie sind – nicht zerlegt, nicht verglichen. Die bisher gültige Fassung endet am Tag vor „gültig ab“, wenn sie kein eigenes Ende hat.';
  dlg.append(el('div.dialog__body', el('h3', version ? `${text ? 'Neue Fassung' : 'Neue Version'} von „${version.title}“` : 'Datensatz hochladen'), row('Dateien', files),
    ...(text ? [el('div.ph-upgrid', row('Gültig ab', vFrom), row('Gültig bis', vTo)), row('Bezeichnung', vLabel), row('Quelle', vSrc), row('Notiz', note)]
      : version ? [row('Notiz', note)] : [row('Titel', title), row('Beschreibung', descr), el('div.ph-upgrid', row('Herausgeber', pub), row('URL', urlI), row('Projekt', proj)),
        row('Sichtbarkeit', vis.el)]), msg, bar, el('div.row.dialog__actions', cancel, ok)));
  cancel.addEventListener('click', () => { dlg.close(); dlg.remove(); });
  ok.addEventListener('click', () => {
    if (!files.files.length) { msg.textContent = 'Bitte Dateien wählen.'; return; }
    if (text && !vFrom.value) { msg.textContent = 'Bitte „gültig ab“ angeben – ab wann gilt diese Fassung?'; vFrom.focus(); return; }
    const huge = [...files.files].filter((f) => f.size > 2 * 1024 ** 3);
    if (huge.length) { msg.textContent = `${huge.map((f) => `${f.name} (${fmt.bytes(f.size)})`).join(', ')}: Dateien über 2 GB bitte über eine Claude-Session übergeben (fundus_dataset_add mit dem Pfad auf dem Spark) oder im Terminal: fundus datasets add <Pfad> – ohne Umweg über den Browser.`; return; }
    const fd = new FormData();
    for (const f of files.files) fd.append('files', f, f.name);
    const fields = version ? [['dataset', String(version.id)], ['note', note.value],
      ...(text ? [['valid_from', vFrom.value], ['valid_to', vTo.value], ['version_label', vLabel.value], ['source_url', vSrc.value]] : [])]
      : [['title', title.value], ['description', descr.value], ['publisher', pub.value], ['url', urlI.value], ['project', proj.value], ['visibility', visibility]];
    for (const [k, v] of fields) if (v?.trim()) fd.append(k, v.trim());
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/datasets/upload');
    bar.hidden = false; ok.disabled = true;
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) { bar.value = Math.round((e.loaded / e.total) * 100); msg.textContent = `Lade hoch … ${bar.value} %`; } };
    xhr.upload.onload = () => { msg.textContent = text ? 'Hochgeladen – die Fassung wird gespeichert …' : 'Hochgeladen – Tabellen werden eingelesen und profiliert …'; };
    xhr.onload = () => {
      ok.disabled = false;
      let d = {};
      try { d = JSON.parse(xhr.responseText); } catch { /* leer */ }
      if (xhr.status !== 200) { msg.textContent = `Fehler: ${d.error ?? xhr.status}`; return; }
      ctx.toast(`Gespeichert: ${arr(d.tables).length} Tabellen${arr(d.documents).length ? `, ${arr(d.documents).length} Dokumente` : ''}. Das Modell beschreibt den Datensatz jetzt.`, 6000);
      dropCatalog();
      dlg.close(); dlg.remove(); onDone?.(d);
    };
    xhr.onerror = () => { ok.disabled = false; msg.textContent = 'Hochladen fehlgeschlagen (Verbindung).'; };
    xhr.send(fd);
  });
  document.body.append(dlg);
  dlg.showModal();
}
