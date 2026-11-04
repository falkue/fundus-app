/**
 * Forschung – the main page: one list of hypotheses and findings (#forschung?q=…&art=h|f&series=…&result=…&ip=missing
 * &status=…&theory=…&dataset=…&flag=…). A one-line summary, a search field, three dropdowns (Art, Serie, Ergebnis) and
 * "Weitere Filter" for the rest. Rows: code · title with a muted second line · type · result · interpretation · date;
 * the whole row is a link. Filtering runs in the browser; from 3 characters the server search adds matches in plans,
 * results and interpretations, and the dataset filter asks the server.
 * Data: GET /api/research/hypotheses?limit=2000, /api/research/findings?limit=2000, /api/research/series.
 */
import { el } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { arr, kids, hp, api, put, listItems, VERDICTS, VERDICT, STATUS, KIND, interpWord, resultPill, itemHref, isFinding, seriesLabel, day, hay } from './forschung-lib.js';
import { rememberList } from './forschung-finding.js';

const PAGE = 100;
const NOTES = [['data_newer_than_result', 'Neuere Daten verfügbar'], ['prereg_incomplete', 'Plan unvollständig'], ['prereg_missing', 'Ohne Plan abgelegt'],
  ['sample_only', 'Bisher nur ein Probelauf']];
const codeKey = (c) => String(c ?? '').replace(/\d+/g, (d) => d.padStart(5, '0'));
const KEYS = ['q', 'art', 'series', 'result', 'ip', 'status', 'theory', 'dataset', 'flag'];

export function render(root, ctx, alive) {
  const p = hp();
  const st = Object.fromEntries(KEYS.map((k) => [k, p.get(k) ?? '']));
  st.limit = PAGE;
  const names = new Map();
  const sName = (slug) => names.get(slug) ?? seriesLabel(slug);
  let items = null, seriesList = [], server = null, serverQ = '', dsIds = null, dsRef = '', theories = null;

  const summary = el('p.fo-summary', { 'aria-live': 'polite' });
  const q = el('input.input.fo-q', { type: 'search', value: st.q, placeholder: 'Suchen nach Titel, Code oder Text', 'aria-label': 'Hypothesen und Findings durchsuchen' });
  const select = (key, label, opts) => {
    const s = el('select.input.fo-select', { 'aria-label': label }, opts.map(([v, l]) => el('option', { value: v }, l)));
    s.value = st[key];
    s.addEventListener('change', () => { st[key] = s.value; st.limit = PAGE; if (key === 'dataset') loadDataset(); draw(); });
    return s;
  };
  const art = select('art', 'Art', [['', 'Hypothesen und Findings'], ['h', 'Nur Hypothesen'], ['f', 'Nur Findings']]);
  const seriesSel = select('series', 'Serie', [['', 'Alle Serien']]);
  const result = select('result', 'Ergebnis', [['', 'Jedes Ergebnis'], ...VERDICTS.map((v) => [v, VERDICT[v]]), ['none', 'noch kein Ergebnis']]);
  const status = select('status', 'Status', [['', 'Jeder Status'], ...Object.entries(STATUS).map(([v, l]) => [v, `Status: ${l}`])]);
  const flag = select('flag', 'Hinweise', [['', 'Alle Hinweise'], ...NOTES]);
  const ipSel = select('ip', 'Interpretation', [['', 'Interpretation: alle'], ['missing', 'ohne Interpretation'], ['complete', 'mit Interpretation']]);
  const theory = select('theory', 'Theorie', [['', 'Jede Theorie']]);
  const dataset = el('input.input.fo-select', { type: 'text', value: st.dataset, placeholder: 'Datensatz, z. B. dataset:47', 'aria-label': 'Datensatz' });
  dataset.addEventListener('change', () => { st.dataset = dataset.value.trim(); st.limit = PAGE; loadDataset(); });
  const more = el('details.fo-morefilters', { open: !!(st.status || st.theory || st.dataset || st.flag || st.ip) },
    el('summary', 'Weitere Filter'), el('div.fo-filters', status, theory, flag, ipSel, dataset));
  more.addEventListener('toggle', () => { if (more.open) loadTheories(); });
  const results = el('div.fo-results');
  const foot = el('p.fo-foot', el('a', { href: '#forschung?view=uebersicht' }, 'Übersicht nach Theorie und Serie'));
  root.append(el('h1.fo-title', 'Forschung'), summary, el('div.fo-toolbar', q, art, seriesSel, result), more, results, foot);

  let tmr, stmr;
  q.addEventListener('input', () => { clearTimeout(tmr); tmr = setTimeout(() => { st.q = q.value.trim(); st.limit = PAGE; draw(); serverSearch(); }, 160); });

  async function load() {
    put(results, el('p.fo-muted', 'Lade …'));
    try {
      const [h, f, s] = await Promise.all([api('/hypotheses?limit=2000'), api('/findings?limit=2000'), api('/series').catch(() => ({}))]);
      if (!alive()) return;
      seriesList = arr(s.series);
      // newest series first: numbered series by number (serie-13 before serie-2), the others after them
      const order = new Map(seriesList.map((x) => [x.slug, Number((x.slug ?? '').match(/^serie-(\d+)/)?.[1] ?? 0)]));
      for (const x of seriesList) if (!/^serie-\d/.test(x.slug ?? '')) names.set(x.slug, shorten(x.title ?? x.slug, 32));
      items = [...listItems(h, 'hypotheses').map((x) => ({ ...x, type: 'hypothesis' })), ...listItems(f, 'findings').map((x) => ({ ...x, type: 'finding' }))];
      for (const x of items) {
        x._s = x.series?.slug ?? x.series ?? '';
        x._hay = hay(x.code, x.title, x.statement, x._s, sName(x._s), x.project, KIND[x.kind], VERDICT[x.verdict]);
        x._ord = [order.has(x._s) ? order.get(x._s) : -1, isFinding(x) ? 1 : 0, codeKey(x.code)];
        x._ip = isFinding(x) || x.verdict ? interpWord(x.interpretation_state) : null;
      }
      items.sort((a, b) => b._ord[0] - a._ord[0] || a._ord[1] - b._ord[1] || a._ord[2].localeCompare(b._ord[2]));
      const used = new Set(items.map((x) => x._s));
      put(seriesSel, el('option', { value: '' }, 'Alle Serien'),
        seriesList.filter((x) => used.has(x.slug)).map((x) => el('option', { value: x.slug }, /^serie-\d/.test(x.slug) ? `${seriesLabel(x.slug)} – ${shorten(x.title, 60)}` : shorten(x.title ?? x.slug, 70))),
        used.has('') ? el('option', { value: 'none' }, 'ohne Serie') : null);
      seriesSel.value = st.series;
      if (more.open) loadTheories();
      if (st.dataset) await loadDataset();
      draw();
      if (st.q) serverSearch();
    } catch (e) { put(results, el('p', `Liste nicht ladbar: ${e.message}`)); }
  }

  async function loadTheories() {
    if (theories) return;
    theories = [];
    try {
      const b = await api('');
      theories = arr(b.evidence_map).map((r) => ({ key: `${r.series ?? ''}|${r.theory}`, label: `${r.series ? `${seriesLabel(r.series)}: ` : ''}${shorten(r.name ?? r.theory, 70)}` }));
      if (!alive()) return;
      put(theory, el('option', { value: '' }, 'Jede Theorie'), theories.map((t) => el('option', { value: t.key }, t.label)));
      theory.value = st.theory;
    } catch { /* no theory filter */ }
  }

  async function loadDataset() {
    const ref = st.dataset;
    if (!ref) { dsIds = null; dsRef = ''; draw(); return; }
    try {
      const r = (x) => api(`/${x}?dataset=${encodeURIComponent(/^\d+$/.test(ref) ? `dataset:${ref}` : ref)}&limit=2000`);
      const [h, f] = await Promise.all([r('hypotheses'), r('findings')]);
      dsIds = new Set([...listItems(h, 'hypotheses').map((x) => `h${x.id}`), ...listItems(f, 'findings').map((x) => `f${x.id}`)]);
      dsRef = ref;
      draw();
    } catch (e) { ctx.toast(`Datensatz-Filter: ${e.message}`, 5000); }
  }

  function serverSearch() {
    clearTimeout(stmr);
    const text = st.q;
    if (text.length < 3) { server = null; serverQ = ''; return; }
    stmr = setTimeout(async () => {
      try {
        const [h, f] = await Promise.all([api(`/hypotheses?q=${encodeURIComponent(text)}&limit=500`), api(`/findings?q=${encodeURIComponent(text)}&limit=500`)]);
        if (!alive() || text !== st.q) return;
        server = new Set([...listItems(h, 'hypotheses').map((x) => `h${x.id}`), ...listItems(f, 'findings').map((x) => `f${x.id}`)]);
        serverQ = text;
        draw();
      } catch { /* browser search only */ }
    }, 380);
  }

  const key = (x) => `${isFinding(x) ? 'f' : 'h'}${x.id}`;
  function passes(x) {
    const tokens = st.q.toLowerCase().split(/\s+/).filter(Boolean);
    if (tokens.length && !(server && serverQ === st.q && server.has(key(x))) && !tokens.every((t) => x._hay.includes(t))) return false;
    if (st.art === 'h' && isFinding(x)) return false;
    if (st.art === 'f' && !isFinding(x)) return false;
    if (st.series && (st.series === 'none' ? x._s : x._s !== st.series)) return false;
    if (st.result && (isFinding(x) || (st.result === 'none' ? !!x.verdict : x.verdict !== st.result))) return false;
    if (st.ip === 'missing' && (x._ip === null || x._ip === 'vorhanden')) return false;
    if (st.ip === 'complete' && x._ip !== 'vorhanden') return false;
    if (st.status && (isFinding(x) || x.status !== st.status)) return false;
    if (st.flag && !arr(x.flags).includes(st.flag)) return false;
    if (st.theory) {
      const [ts, tid] = st.theory.split('|');
      if (!arr(x.theory_refs).includes(tid) || (ts && x._s !== ts)) return false;
    }
    if (st.dataset && dsIds && dsRef === st.dataset && !dsIds.has(key(x))) return false;
    return true;
  }

  function writeHash() {
    const h = new URLSearchParams();
    for (const k of KEYS) if (st[k]) h.set(k, st[k]);
    const s = `#forschung${h.toString() ? `?${h}` : ''}`;
    if (location.hash !== s && !(location.hash === '' && s === '#forschung')) history.replaceState(null, '', s);
    rememberList(s);
  }

  function drawSummary() {
    const hs = items.filter((x) => !isFinding(x));
    const fs = items.length - hs.length;
    const missing = items.filter((x) => x._ip && x._ip !== 'vorhanden').length;
    const link = st.ip === 'missing'
      ? el('a', { href: '#', on: { click: (e) => { e.preventDefault(); st.ip = ''; ipSel.value = ''; draw(); } } }, 'alle zeigen')
      : el('a', { href: '#', on: { click: (e) => { e.preventDefault(); st.ip = 'missing'; ipSel.value = 'missing'; draw(); } } }, `${fmt.int(missing)} ohne Interpretation`);
    put(summary, `${fmt.int(hs.length)} Hypothesen · ${fmt.int(hs.filter((x) => x.verdict).length)} mit Ergebnis · ${fmt.int(fs)} Findings`, el('span.fo-summary__sep', ' · '), link);
  }

  function draw() {
    if (!items) return;
    writeHash();
    drawSummary();
    const shown = items.filter(passes);
    const list = shown.slice(0, st.limit);
    const active = KEYS.some((k) => st[k]);
    const reset = active ? el('button.btn.btn--ghost.btn--small', { type: 'button', on: { click: resetAll } }, 'Filter zurücksetzen') : null;
    if (!shown.length) { put(results, el('div.fo-empty', el('p', 'Nichts gefunden.'), reset)); return; }
    put(results,
      active ? el('p.fo-count', `${fmt.int(shown.length)} von ${fmt.int(items.length)}`, ' ', reset) : null,
      el('div.fo-table', { role: 'table', 'aria-label': 'Hypothesen und Findings' },
        el('div.fo-row.fo-row--head', { role: 'row' }, ['Code', 'Titel', 'Art', 'Ergebnis', 'Interpretation', 'Datum'].map((t) => el('span', { role: 'columnheader' }, t))),
        list.map(row)),
      shown.length > list.length ? el('div.fo-more', el('button.btn', { type: 'button', on: { click: () => { st.limit += PAGE; draw(); } } },
        `Weitere ${fmt.int(Math.min(PAGE, shown.length - list.length))} anzeigen`), el('span.fo-muted', `(noch ${fmt.int(shown.length - list.length)})`)) : null);
  }

  function resetAll() {
    for (const k of KEYS) st[k] = '';
    q.value = ''; dataset.value = ''; dsIds = null;
    for (const s of [art, seriesSel, result, status, flag, ipSel, theory]) s.value = '';
    draw();
  }

  function row(x) {
    const f = isFinding(x);
    const date = f ? x.updated_at : (x.preregistered_at ?? x.updated_at);
    return el('a.fo-row', { href: itemHref(x), role: 'row' },
      el('span.fo-row__code', { role: 'cell' }, x.code ?? '–'),
      el('span.fo-row__title', { role: 'cell' }, el('span.fo-row__t', x.title || x.statement || x.code), el('span.fo-row__sub', kids(x._s ? sName(x._s) : 'ohne Serie', KIND[x.kind]).join(' · '))),
      el('span.fo-row__type', { role: 'cell' }, f ? 'Finding' : 'Hypothese'),
      el('span.fo-row__res', { role: 'cell' }, f ? el('span.fo-muted.fo-dash', '–') : resultPill(x.verdict)),
      el('span.fo-row__ip', { role: 'cell', class: `fo-row__ip${x._ip === 'fehlt' ? ' is-missing' : ''}` }, x._ip ? [el('span.fo-mlabel', 'Interpretation '), x._ip] : el('span.fo-muted', '–')),
      el('span.fo-row__date', { role: 'cell' }, date ? day(date) : '–'));
  }

  load();
}

const shorten = (s, n) => (String(s ?? '').length > n ? `${String(s).slice(0, n - 1)}…` : String(s ?? ''));
