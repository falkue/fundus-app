/**
 * Datensätze › Katalog (#daten, #daten?q=…&topic=ai|ml&sort=…&view=table&r=1): Kennzahlen, „Frage an den Bestand“
 * (lokales Modell, einklappbar), Facetten links (Anzahl je Wert reagiert auf die übrigen Filter; schmal als Schublade),
 * Suche (im Browser über Titel, Zusammenfassung, Schlagworte, Herausgeber; ab 3 Zeichen zusätzlich die Server-Suche, die
 * auch Spaltennamen und -bedeutungen kennt), Sortierung, Karten oder dichte Tabelle mit Zeitraum-Leiste auf gemeinsamer
 * Achse. Alles Filtern und Sortieren läuft im Browser über den ganzen Katalog (GET /api/datasets/catalog).
 */
import { el, svgEl, emptyNote, segmented, badge } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { arr, kids, hp, api, loadCatalog, FACETS, facetLabel, facetOrder, badges, glyphs, figures, periodText, compact, put, isText } from './daten-lib.js';

const SORTS = [
  { value: 'relevant', label: 'Relevanz' }, { value: 'new', label: 'Neueste zuerst' }, { value: 'big', label: 'Grösste zuerst' },
  { value: 'used', label: 'Zuletzt genutzt' }, { value: 'due', label: 'Update fällig zuerst' }, { value: 'title', label: 'Titel A–Z' },
  { value: 'publisher', label: 'Herausgeber A–Z' }, { value: 'period', label: 'Zeitraum, früheste zuerst' }, { value: 'rows', label: 'Meiste Zeilen' },
  { value: 'uses', label: 'Meiste Nutzungen (30 Tage)' }];
const PAGE = 60;
const ASC = new Set(['title', 'publisher', 'period']);   // natürliche Richtung aufsteigend
const DUE_RANK = { overdue: 3, never_checked: 2, stale_check: 1 };
const STATE_ORDER = ['update_due', 'stale', 'dormant', 'used', 'slides'];
let askOpen = false;   // „Frage an den Bestand“ aufgeklappt (bleibt beim Wechsel der Reiter)

export function render(root, ctx, alive) {
  const p = hp();
  const st = {
    q: p.get('q') ?? '', sort: p.get('sort') ?? 'relevant', rev: p.get('rev') === '1', view: p.get('view') === 'table' ? 'table' : 'cards',
    removed: p.get('r') === '1', sel: {}, open: {}, more: new Set(), limit: PAGE, server: null, serverQ: '', cat: null,
  };
  for (const f of FACETS) {
    const v = p.get(f.key);
    if (v) st.sel[f.key] = new Set(v.split('|').filter(Boolean));
  }

  const kpis = el('div.dk-kpis');
  const q = el('input.input.dk-q', { type: 'search', placeholder: 'Suchen – Thema, Spalte, Herausgeber, dataset:123 …', value: st.q, 'aria-label': 'Datensätze suchen' });
  const sort = el('select.input.dk-sort', { 'aria-label': 'Sortierung' }, SORTS.map((s) => el('option', { value: s.value }, s.label)));
  sort.value = st.sort;
  const view = segmented([{ value: 'cards', label: 'Karten' }, { value: 'table', label: 'Tabelle' }], st.view, (v) => { st.view = v; st.limit = PAGE; draw(); }, 'Darstellung');
  const fbtn = el('button.btn.dk-fbtn', { type: 'button', 'aria-expanded': 'false', 'aria-controls': 'dk-facets' }, 'Filter');
  const facets = el('aside.dk-facets#dk-facets', { 'aria-label': 'Filter' });
  const backdrop = el('div.dk-backdrop', { hidden: true });
  const info = el('div.dk-info', { 'aria-live': 'polite' });
  const results = el('div.dk-results');
  const legacy = el('p.small.muted.dk-legacy', { hidden: true });

  root.append(kpis, askBox(alive), legacy,
    el('div.dk-layout', facets, el('div.dk-main', el('div.dk-toolbar', q, sort, view.el, fbtn), info, results)), backdrop);

  // ---- Ereignisse
  let tmr, stmr;
  q.addEventListener('input', () => {
    clearTimeout(tmr); tmr = setTimeout(() => { st.q = q.value.trim(); st.limit = PAGE; if (st.q && st.sort === 'relevant') sort.value = 'relevant'; draw(); serverSearch(); }, 160);
  });
  sort.addEventListener('change', () => { st.sort = sort.value; st.rev = false; draw(); });
  const drawer = (on) => {
    facets.classList.toggle('is-open', on); backdrop.hidden = !on; fbtn.setAttribute('aria-expanded', String(on));
    if (on) facets.querySelector('button, input, summary')?.focus();
  };
  fbtn.addEventListener('click', () => drawer(!facets.classList.contains('is-open')));
  backdrop.addEventListener('click', () => drawer(false));
  facets.addEventListener('keydown', (e) => { if (e.key === 'Escape' && facets.classList.contains('is-open')) { drawer(false); fbtn.focus(); } });

  // ---- Server-Suche (Spaltennamen und -bedeutungen) ab 3 Zeichen
  function serverSearch() {
    clearTimeout(stmr);
    const text = st.q;
    if (text.length < 3 || /^dataset:\d+$/i.test(text)) { st.server = null; st.serverQ = ''; return; }
    stmr = setTimeout(async () => {
      try {
        const r = await api(`?q=${encodeURIComponent(text)}&limit=120${st.removed ? '&removed=1' : ''}`);
        if (!alive() || text !== st.q) return;
        st.server = new Map(arr(r.datasets).map((x, i) => [x.id, i]));
        st.serverQ = text;
        draw();
      } catch { /* nur die Suche im Browser */ }
    }, 380);
  }

  // ---- Laden
  async function load(force) {
    put(results, el('div.dk-skel', Array.from({ length: 6 }, () => el('div.dk-skel__card'))));
    try {
      const cat = await loadCatalog({ removed: st.removed, force });
      if (!alive()) return;
      st.cat = cat;
      for (const d of arr(cat.datasets)) d._fv ??= Object.fromEntries(FACETS.map((f) => [f.key, f.of(d)]));
      legacy.hidden = !cat.legacy;
      legacy.textContent = cat.legacy ? 'Der Fundus-Server liefert den Katalog noch im alten Format: Nutzung, Lebenszyklus, Referenzen und Prüfsummen erscheinen nach dem nächsten Neustart des Servers.' : '';
      drawKpis();
      draw();
      if (st.q) serverSearch();
    } catch (e) { put(results, el('div.alert.alert--error', `Katalog nicht ladbar: ${e.message}`)); }
  }

  // ---- Kennzahlen
  function drawKpis() {
    const s = st.cat.stats ?? {};
    const stored = (s.bytes_original ?? 0) + (s.bytes_parquet ?? 0);
    const tile = (label, value, foot, opts = {}) => {
      const t = el(opts.onClick ? 'button.kpi.dk-kpi' : 'div.kpi.kpi--static.dk-kpi', { type: opts.onClick ? 'button' : null, title: opts.title ?? null },
        el('span.kpi__label', label), el('span.kpi__value', value), foot ? el('span.kpi__foot', foot) : null);
      if (opts.tone) t.classList.add(`dk-kpi--${opts.tone}`);
      if (opts.onClick) t.addEventListener('click', opts.onClick);
      return t;
    };
    const filterState = (v) => () => { st.sel = { state: new Set([v]) }; st.q = ''; q.value = ''; st.limit = PAGE; draw(); results.scrollIntoView({ block: 'start', behavior: 'smooth' }); };
    put(kpis, 
      tile('Datensätze', fmt.int(s.datasets), s.internal != null ? `davon ${fmt.int(s.internal)} intern` : null),
      tile('Tabellen', fmt.int(s.tables), s.references ? `${fmt.int(s.references)} ${s.references === 1 ? 'Referenz' : 'Referenzen'} ohne Tabellen` : null),
      tile('Zeilen', compact(s.rows), null, { title: `${fmt.int(s.rows)} Zeilen` }),
      tile('Speicher', s.bytes_original != null ? fmt.bytes(stored) : '–',
        kids(s.bytes_parquet != null ? `Original ${fmt.bytes(s.bytes_original)} + Parquet ${fmt.bytes(s.bytes_parquet)}` : null,
          s.bytes_reference ? `Referenzen ${fmt.bytes(s.bytes_reference)}` : null).join(' · ') || null),
      s.texts != null ? tile('Texte mit Fassungen', fmt.int(s.texts), 'Gesetze, Leitlinien, Policies',
        s.texts ? { onClick: () => { st.sel = { kind: new Set(['text']) }; st.q = ''; q.value = ''; st.limit = PAGE; draw(); results.scrollIntoView({ block: 'start', behavior: 'smooth' }); }, title: 'Im Katalog zeigen' } : {}) : null,
      tile('Update fällig', fmt.int(s.update_due), 'neue Ausgabe erschienen oder erwartet', { onClick: filterState('update_due'), tone: s.update_due ? 'warn' : null, title: 'Im Katalog zeigen' }),
      tile('Ruhend', s.dormant != null ? fmt.int(s.dormant) : '–', '90 Tage nicht genutzt', s.dormant != null ? { onClick: filterState('dormant'), title: 'Im Katalog zeigen' } : {}),
      tile('Nutzungen', s.uses_30d != null ? fmt.int(s.uses_30d) : '–', 'in den letzten 30 Tagen', s.uses_30d ? { onClick: () => { st.sort = 'uses'; sort.value = 'uses'; draw(); }, title: 'Nach Nutzung sortieren' } : {}));
  }

  // ---- Filtern
  function textScore(d, tokens) {
    if (!tokens.length) return 1;
    let score = 0;
    for (const t of tokens) {
      if (t.startsWith('dataset:')) { if (d.ref !== t) return 0; score += 50; continue; }
      if (!d._hay.includes(t)) return 0;
      score += d._title.includes(t) ? (d._title.startsWith(t) ? 6 : 4) : 1;
    }
    return score;
  }
  function passes(d, except) {
    for (const [key, set] of Object.entries(st.sel)) {
      if (key === except || !set?.size) continue;
      if (!d._fv[key].some((v) => set.has(v))) return false;
    }
    return true;
  }
  function textBase() {
    const tokens = st.q.toLowerCase().split(/\s+/).filter(Boolean);
    const srv = st.server && st.serverQ === st.q ? st.server : null;
    const out = [];
    for (const d of arr(st.cat.datasets)) {
      let s = textScore(d, tokens);
      const r = srv?.get(d.id);
      if (r !== undefined) s += Math.max(0.5, 3 - r / 20);
      if (s > 0) { d._score = s; d._srvOnly = !textScore(d, tokens) && r !== undefined; out.push(d); }
    }
    return out;
  }

  function sorter() {
    const by = {
      relevant: st.q ? (a, b) => b._score - a._score : (a, b) => String(b.updated_at ?? '').localeCompare(String(a.updated_at ?? '')),
      new: (a, b) => String(b.created_at ?? b.updated_at ?? '').localeCompare(String(a.created_at ?? a.updated_at ?? '')) || b.id - a.id,
      big: (a, b) => (b.bytes ?? 0) - (a.bytes ?? 0),
      used: (a, b) => String(b.last_used ?? '').localeCompare(String(a.last_used ?? '')),
      due: (a, b) => (DUE_RANK[b.update_due] ?? 0) - (DUE_RANK[a.update_due] ?? 0) || String(a.next_expected ?? '9').localeCompare(String(b.next_expected ?? '9')),
      title: (a, b) => String(a.title).localeCompare(String(b.title), 'de', { sensitivity: 'base', numeric: true }),
      publisher: (a, b) => String(a.publisher_group ?? '￿').localeCompare(String(b.publisher_group ?? '￿'), 'de', { sensitivity: 'base' }),
      period: (a, b) => (a.period_from ?? 9999) - (b.period_from ?? 9999) || (a.period_to ?? 9999) - (b.period_to ?? 9999),
      rows: (a, b) => (b.rows ?? 0) - (a.rows ?? 0),
      uses: (a, b) => (b.uses_30d ?? 0) - (a.uses_30d ?? 0) || String(b.last_used ?? '').localeCompare(String(a.last_used ?? '')),
    }[st.sort] ?? ((a, b) => b.id - a.id);
    return st.rev ? (a, b) => by(b, a) : by;
  }

  function writeHash() {
    const h = new URLSearchParams();
    if (st.q) h.set('q', st.q);
    for (const f of FACETS) if (st.sel[f.key]?.size) h.set(f.key, [...st.sel[f.key]].join('|'));
    if (st.sort !== 'relevant') h.set('sort', st.sort);
    if (st.rev) h.set('rev', '1');
    if (st.view === 'table') h.set('view', 'table');
    if (st.removed) h.set('r', '1');
    const s = `#daten${h.toString() ? `?${h}` : ''}`;
    if (location.hash !== s) history.replaceState(null, '', s);
    try { sessionStorage.setItem('fundus-daten-katalog', s); } catch { /* privat */ }
  }

  function draw() {
    if (!st.cat) return;
    writeHash();
    const base = textBase();
    const shown = base.filter((d) => passes(d, null)).sort(sorter());
    drawFacets(base);
    drawInfo(shown.length);
    const list = shown.slice(0, st.limit);
    if (!shown.length) {
      put(results, emptyNote(st.removed ? 'Nichts gelöscht.' : arr(st.cat.datasets).length ? 'Kein Datensatz passt zu Suche und Filtern.' : 'Noch keine Datensätze. „Hochladen …“ oder in einer Claude-Session mit fundus_dataset_add.'));
      return;
    }
    const more = shown.length > list.length ? el('div.row.dk-more',
      el('button.btn', { type: 'button', on: { click: () => { st.limit += PAGE; draw(); } } }, `Weitere ${Math.min(PAGE, shown.length - list.length)} anzeigen`),
      el('button.btn.btn--ghost', { type: 'button', on: { click: () => { st.limit = Infinity; draw(); } } }, `Alle ${fmt.int(shown.length)} anzeigen`)) : null;
    put(results, st.view === 'table' ? tableView(list) : el('div.dk-cards', list.map(cardView)), more ?? '');
  }

  // ---- Facetten
  function drawFacets(base) {
    const sel = Object.values(st.sel).reduce((a, s) => a + (s?.size ?? 0), 0);
    fbtn.textContent = sel ? `Filter (${sel})` : 'Filter';
    const groups = [];
    const scope = segmented([{ value: '', label: 'Bestand' }, { value: '1', label: 'Gelöscht' }], st.removed ? '1' : '',
      (v) => { st.removed = v === '1'; st.limit = PAGE; load(); }, 'Anzeige');
    for (const f of FACETS) {
      const all = new Map();
      for (const d of arr(st.cat.datasets)) for (const v of d._fv[f.key]) all.set(v, 0);
      const chosen = st.sel[f.key] ?? new Set();
      if (all.size < 2 && !chosen.size) continue;
      for (const d of base) if (passes(d, f.key)) for (const v of d._fv[f.key]) all.set(v, (all.get(v) ?? 0) + 1);
      let vals = [...all].map(([value, count]) => ({ value, count, label: facetLabel(st.cat, f.key, value) }))
        .filter((x) => x.count > 0 || chosen.has(x.value));
      const ord = facetOrder(f.key);
      if (f.key === 'state') vals.sort((a, b) => STATE_ORDER.indexOf(a.value) - STATE_ORDER.indexOf(b.value));
      else if (ord) vals.sort(ord);
      else vals.sort((a, b) => b.count - a.count || a.label.localeCompare(b.label, 'de'));
      if (!vals.length) continue;
      const expanded = st.more.has(f.key);
      const top = expanded ? vals : vals.filter((x, i) => i < 8 || chosen.has(x.value));
      const det = el('details.dk-facet', { open: st.open[f.key] ?? (f.open || chosen.size > 0) },
        el('summary', el('span', f.title), chosen.size ? el('span.dk-facet__n', String(chosen.size)) : null),
        el('ul.dk-facet__list', top.map((x) => {
          const id = `dkf-${f.key}-${String(x.value).replace(/[^a-z0-9]/gi, '_')}`;
          const cb = el('input', { type: 'checkbox', id, checked: chosen.has(x.value) });
          cb.addEventListener('change', () => {
            const s = st.sel[f.key] ?? new Set();
            if (cb.checked) s.add(x.value); else s.delete(x.value);
            st.sel[f.key] = s; st.limit = PAGE; draw();
            document.getElementById(id)?.focus();
          });
          return el('li', { class: x.count ? null : 'is-zero' }, cb, el('label', { for: id, title: x.label }, el('span.dk-facet__l', x.label)), el('span.dk-facet__c', fmt.int(x.count)));
        })),
        vals.length > top.length || expanded ? el('button.btn.btn--ghost.btn--small.dk-facet__more', { type: 'button', on: { click: () => { if (expanded) st.more.delete(f.key); else st.more.add(f.key); draw(); } } },
          expanded ? 'weniger' : `mehr … (${vals.length - top.length})`) : null);
      det.addEventListener('toggle', () => { st.open[f.key] = det.open; });
      groups.push(det);
    }
    const reset = el('button.btn.btn--small', { type: 'button', disabled: !sel && !st.q }, 'Filter zurücksetzen');
    reset.addEventListener('click', () => { st.sel = {}; st.q = ''; q.value = ''; st.server = null; st.limit = PAGE; draw(); });
    const close = el('button.btn.btn--small.dk-facets__close', { type: 'button', on: { click: () => drawer(false) } }, 'Fertig');
    put(facets, el('div.dk-facets__head', el('span.label', 'Filter'), reset, close), scope.el, ...groups);
  }

  function drawInfo(n) {
    const total = arr(st.cat.datasets).length;
    const chips = [];
    for (const f of FACETS) for (const v of st.sel[f.key] ?? []) {
      chips.push(el('button.dk-chip', { type: 'button', title: 'Filter entfernen', on: { click: () => { st.sel[f.key].delete(v); draw(); } } },
        el('span.muted', `${f.title}: `), facetLabel(st.cat, f.key, v), el('span.dk-chip__x', { 'aria-hidden': 'true' }, '×')));
    }
    const srvN = st.server && st.serverQ === st.q ? arr(st.cat.datasets).filter((d) => d._srvOnly && passes(d, null)).length : 0;
    put(info, el('span.dk-count', n === total ? `${fmt.int(total)} ${total === 1 ? 'Datensatz' : 'Datensätze'}${st.removed ? ' gelöscht' : ''}`
      : `${fmt.int(n)} von ${fmt.int(total)} Datensätzen`),
    st.q && st.q.length >= 3 && !st.server ? el('span.small.muted', ' · suche auch in Spalten …') : null,
    srvN ? el('span.small.muted', ` · ${srvN} davon über Spalten oder Beschreibung gefunden`) : null,
    ...chips);
  }

  // ---- Karte
  function cardView(d) {
    const meta = isText(d) ? kids(d.jurisdiction, d.identifier, d.ref).join(' · ') : kids(d.publisher_group || d.publisher, periodText(d), d.ref).join(' · ');
    return el('a.card.dk-card', { href: `#daten?d=${d.slug}`, class: `card dk-card${d.lifecycle && d.lifecycle !== 'active' ? ' is-faded' : ''}` },
      el('div.dk-card__top', el('h3.dk-card__title', d.title)),
      el('p.dk-card__meta', meta),
      el('p.dk-card__fig', figures(d), d.uses_30d ? el('span.dk-card__use', ` · ${fmt.int(d.uses_30d)}× genutzt in 30 Tagen`) : null),
      badges(d, st.cat).length ? el('div.dk-badges', badges(d, st.cat)) : null,
      d.summary ? el('p.dk-card__sum', d.summary) : null,
      arr(d.keywords).length ? el('div.dk-kw', arr(d.keywords).slice(0, 5).map((k) => el('span.ds-chip', k))) : null,
      d._srvOnly ? el('p.small.muted.dk-card__why', 'gefunden über Spalten oder Beschreibung') : null);
  }

  // ---- Tabelle
  function tableView(list) {
    const ys = list.flatMap((d) => (d.period_from ? [d.period_from, d.period_to ?? d.period_from] : []));
    const y0 = ys.length ? Math.min(...ys) : 2000;
    const y1 = ys.length ? Math.max(...ys) : 2026;
    const W = 140, H = 12;
    const xs = (y) => (y1 === y0 ? W / 2 : ((y - y0) / (y1 - y0)) * (W - 4) + 2);
    const bar = (d) => {
      const s = svgEl('svg', { class: 'dk-tl', viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img', 'aria-label': periodText(d) ?? 'Zeitraum unbekannt' });
      s.append(svgEl('line', { x1: 2, x2: W - 2, y1: H / 2, y2: H / 2, class: 'dk-tl__track' }));
      if (d.period_from) {
        const a = xs(d.period_from), b = xs(d.period_to ?? d.period_from);
        s.append(svgEl('rect', { x: a, y: 2, width: Math.max(3, b - a), height: H - 4, rx: 2, class: 'dk-tl__bar' }));
      }
      return s;
    };
    const th = (label, key, cls) => {
      const on = st.sort === key;
      const b = el('th', { scope: 'col', class: `${cls ?? ''} dk-th${on ? ' is-on' : ''}`.trim(), 'aria-sort': on ? (ASC.has(key) !== st.rev ? 'ascending' : 'descending') : null },
        el('button.dk-th__b', { type: 'button' }, label, on ? el('span.dk-th__arrow', ASC.has(key) !== st.rev ? ' ↑' : ' ↓') : null));
      b.firstChild.addEventListener('click', () => { if (st.sort === key) st.rev = !st.rev; else { st.sort = key; st.rev = false; } sort.value = st.sort; draw(); });
      return b;
    };
    const tlHead = th('Zeitraum', 'period', 'dk-col-tl');
    tlHead.append(el('span.dk-axis', el('span', String(y0)), el('span', String(y1))));
    return el('div.card.dk-tablecard', el('div.table-wrap', el('table.table.table--compact.dk-table',
      el('thead', el('tr', th('Titel', 'title'), th('Herausgeber', 'publisher', 'dk-col-pub'), tlHead,
        th('Zeilen', 'rows', 'num'), th('Grösse', 'big', 'num'), th('Genutzt (30 Tage)', 'uses', 'num dk-col-use'), el('th', { scope: 'col' }, 'Status'))),
      el('tbody', list.map((d) => {
        const tr = el('tr.clickable', { tabindex: '0', class: `clickable${d.lifecycle && d.lifecycle !== 'active' ? ' is-faded' : ''}` },
          el('td.dk-col-title', el('a', { href: `#daten?d=${d.slug}` }, d.title), el('div.small.muted', kids(d.ref, d.project).join(' · '))),
          el('td.dk-col-pub.small', isText(d) ? (d.issuer ?? d.publisher_group ?? '–') : (d.publisher_group ?? '–')),
          el('td.dk-col-tl', bar(d), el('div.small.muted', periodText(d) ?? '–')),
          el('td.num', isText(d) ? el('span.small.muted', `${d.version} ${d.version === 1 ? 'Fassung' : 'Fassungen'}`) : d.rows != null ? compact(d.rows) : '–'), el('td.num', fmt.bytes(d.bytes)),
          el('td.num.dk-col-use', d.uses_30d != null ? fmt.int(d.uses_30d) : '–'),
          el('td.dk-col-st', el('span.dk-glyphs', glyphs(d))));
        const go = (e) => { if (e.target.closest('a')) return; location.hash = `#daten?d=${d.slug}`; };
        tr.addEventListener('click', go);
        tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(e); });
        return tr;
      })))));
  }

  load();
}

// ---------------------------------------------------------------- Frage an den Bestand (lokales Modell)
const ANSWER = { ja: ['Ja', 'ok'], teilweise: ['Teilweise', 'warn'], nein: ['Nein', 'outline'] };

function askBox(alive) {
  const input = el('input.input.ds-ask__q', { type: 'search', placeholder: 'z. B. Haben wir Jobpostings zum Marketing?', 'aria-label': 'Frage an den Bestand' });
  const go = el('button.btn.btn--primary', { type: 'submit' }, 'Fragen');
  const out = el('div.ds-ask__out', { 'aria-live': 'polite' });
  const form = el('form.row.ds-ask__form', input, go);
  let run = 0;
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const question = input.value.trim();
    if (question.length < 3) { input.focus(); return; }
    const my = ++run;
    go.disabled = true;
    put(out, el('p.small.muted.ds-ask__wait', el('span.ds-ask__spin', { 'aria-hidden': 'true' }), 'Gemma liest den Katalog … (etwa 1–2 Minuten)'));
    try {
      const r = await api('/ask', { question });
      if (!alive() || my !== run) return;
      put(out, answer(r));
    } catch (err) {
      if (my === run) put(out, el('div.alert.alert--error', `Keine Antwort: ${err.message}`));
    } finally { if (my === run) go.disabled = false; }
  });
  const det = el('details.card.dk-ask', { open: askOpen },
    el('summary.dk-ask__sum', el('h3', el('em', 'Frage'), ' an den Bestand'), el('span.small.muted', 'Gemma 4, lokal auf der Spark – antwortet aus den Beschreibungen, nicht aus den Daten')),
    el('div.dk-ask__body', form, out, el('p.small.muted', 'Treffer vor der Verwendung im Datensatz prüfen.')));
  det.addEventListener('toggle', () => { askOpen = det.open; if (det.open) input.focus(); });
  return det;
}

function answer(r) {
  const [label, tone] = ANSWER[r.answer] ?? ANSWER.nein;
  const meta = (x) => [x.ref, x.publisher, x.period, x.geo, x.rows ? `${fmt.int(x.rows)} Zeilen` : null].filter(Boolean).join(' · ');
  const hit = (x) => el('a.card.ds-item.ds-ask__hit', { href: `#daten?d=${x.slug ?? x.id}` },
    el('div.ds-item__head', el('strong', x.title), badge(x.fit === 'direkt' ? 'direkter Treffer' : 'teilweise', x.fit === 'direkt' ? 'ok' : 'warn'),
      x.visibility === 'internal' ? badge('intern', 'warn') : null),
    x.why ? el('p.small', x.why) : null,
    arr(x.columns).length ? el('div.ds-kw', el('span.small.muted', 'Spalten:'), arr(x.columns).map((c) => el('span.ds-chip', c))) : null,
    el('p.small.muted', meta(x)));
  const rel = (x) => el('li', el('a', { href: `#daten?d=${x.slug ?? x.id}` }, x.title), x.why ? el('span.small.muted', ` – ${x.why}`) : null);
  return el('div.ds-ask__answer',
    el('div.ds-ask__verdict', badge(label, tone), r.summary ? el('p', r.summary) : null),
    arr(r.hits).length ? el('div.ds-list', arr(r.hits).map(hit)) : null,
    arr(r.related).length ? el('div.ds-ask__rel', el('h4', 'Verwandt'), el('ul', arr(r.related).map(rel))) : null,
    el('p.small.muted', `${r.candidates ?? 0} Datensätze genauer angesehen · ${r.cached ? 'aus dem Zwischenspeicher' : `${fmt.int(Math.round(r.seconds ?? 0))} s`} · ${r.model ?? ''}`));
}

