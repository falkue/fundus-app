/**
 * Forschung › Hypothesen (#forschung?tab=hypothesen&series=…&verdict=…&status=…&kind=…&theory=…&ip=missing&q=…&sort=…)
 * and Findings (#forschung?tab=findings&…): filter in the browser over the whole list (GET /api/research/hypotheses,
 * /api/research/findings); from 3 characters the server search adds matches in pre-registrations, results and
 * interpretations. Every verdict is a filter of equal rank; no default sort puts "gestützt" first.
 */
import { el, emptyNote, segmented } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { arr, kids, hp, api, put, listItems, VERDICTS, VERDICT, STATUS, KIND, STRENGTH, STRENGTH_LABEL, FSTATUS, FLAG, vpill, vmark, statusChip, kindChip,
  interpChip, exploratoryChip, hypHref, findHref, seriesLabel, day, hay, verdictBar } from './forschung-lib.js';
import { ciGlyph } from './forschung-svg.js';
import { strengthScale } from './forschung-finding.js';

const PAGE = 80;
const codeKey = (c) => String(c ?? '').replace(/\d+/g, (d) => d.padStart(5, '0'));
const H_SORTS = [{ value: 'code', label: 'Code' }, { value: 'result', label: 'Neueste Ergebnisse zuerst' }, { value: 'created', label: 'Zuletzt erfasst' },
  { value: 'verdict', label: 'Nach Urteil gruppiert' }, { value: 'n', label: 'Grösste Fallzahl' }];
const F_SORTS = [{ value: 'updated', label: 'Zuletzt geändert' }, { value: 'code', label: 'Code' }, { value: 'strength', label: 'Nach Stärke' }, { value: 'evidence', label: 'Meiste Belege' }];

async function loadAll(kind) {
  const d = await api(`/${kind}?limit=2000`);
  return listItems(d, kind);
}

export function render(root, ctx, kind, alive, board) {
  const isH = kind === 'hypotheses';
  const p = hp();
  const st = { q: p.get('q') ?? '', series: p.get('series') ?? '', verdict: p.get('verdict') ?? '', status: p.get('status') ?? '', kindF: p.get('kind') ?? '',
    theory: p.get('theory') ?? '', ip: p.get('ip') ?? '', flag: p.get('flag') ?? '', strength: p.get('strength') ?? '', sort: p.get('sort') ?? (isH ? 'code' : 'updated'),
    limit: PAGE, items: null, server: null, serverQ: '' };
  const q = el('input.input.dk-q', { type: 'search', value: st.q, placeholder: isH ? 'Suchen – Code, Titel, Theorie, Datensatz, Text der Präregistrierung …' : 'Suchen – Titel, Aussage, Kontext, Zitat …', 'aria-label': 'Suchen' });
  const controls = {};
  const sel = (key, label, opts) => {
    const s = el('select.input.fo-sel', { 'aria-label': label }, el('option', { value: '' }, label), opts.map(([v, l]) => el('option', { value: v }, l)));
    s.value = st[key];
    s.addEventListener('change', () => { st[key] = s.value; st.limit = PAGE; draw(); });
    controls[key] = s;
    return s;
  };
  const seriesOpts = arr(board?.series).filter((s) => s.slug).map((s) => [s.slug, `${seriesLabel(s.slug)} – ${s.title}`]).concat([['none', 'ohne Serie']]);
  // theory filter value = '<series slug>|<theory id>' (theory ids are per series, e.g. 'A' or 'T1')
  const tkey = (r) => r.key ?? `${r.series ?? ''}|${r.theory}`;
  const theoryOpts = arr(board?.evidence_map?.rows).map((r) => [tkey(r), r.series ? `${seriesLabel(r.series)}: ${r.name ?? r.theory}` : (r.name ?? r.theory)]);
  const sort = el('select.input.dk-sort', { 'aria-label': 'Sortierung' }, (isH ? H_SORTS : F_SORTS).map((s) => el('option', { value: s.value }, s.label)));
  sort.value = st.sort;
  sort.addEventListener('change', () => { st.sort = sort.value; draw(); });
  const verdictSeg = isH ? segmented([{ value: '', label: 'alle Urteile' }, ...VERDICTS.map((v) => ({ value: v, label: VERDICT[v] })), { value: 'none', label: 'ohne Ergebnis' }],
    st.verdict, (v) => { st.verdict = v; st.limit = PAGE; draw(); }, 'Urteil') : null;
  if (verdictSeg) {
    verdictSeg.el.classList.add('fo-vseg');
    verdictSeg.el.querySelectorAll('.seg__btn').forEach((b, i) => { const v = [null, ...VERDICTS, 'open'][i]; if (v) b.prepend(vmark(v)); });
  }
  const filters = isH
    ? [sel('series', 'alle Serien', seriesOpts), sel('status', 'jeder Status', Object.entries(STATUS)), sel('kindF', 'jede Art', Object.entries(KIND)),
      theoryOpts.length ? sel('theory', 'jede Theorie', theoryOpts) : null,
      sel('ip', 'Interpretation: alle', [['missing', 'Interpretation fehlt oder unvollständig'], ['complete', 'Interpretation vollständig']]),
      sel('flag', 'alle Hinweise', [['data_newer_than_result', FLAG.data_newer_than_result], ['interpretation_missing', FLAG.interpretation_missing]])]
    : [sel('series', 'alle Serien', seriesOpts), sel('kindF', 'jede Art', Object.entries(KIND)), sel('strength', 'jede Stärke', STRENGTH.map((s) => [s, STRENGTH_LABEL[s]])),
      sel('status', 'jeder Status', Object.entries(FSTATUS)), sel('ip', 'Interpretation: alle', [['missing', 'Interpretation fehlt oder unvollständig'], ['complete', 'Interpretation vollständig']])];
  const info = el('div.dk-info', { 'aria-live': 'polite' });
  const results = el('div.fo-results');
  const summary = el('div.fo-listsum');
  put(root, el('div.fo-toolbar', q, sort), verdictSeg?.el, el('div.fo-filters', filters.filter(Boolean)), summary, info, results);

  let tmr, stmr;
  q.addEventListener('input', () => { clearTimeout(tmr); tmr = setTimeout(() => { st.q = q.value.trim(); st.limit = PAGE; draw(); serverSearch(); }, 160); });

  function serverSearch() {
    clearTimeout(stmr);
    const text = st.q;
    if (text.length < 3) { st.server = null; st.serverQ = ''; return; }
    stmr = setTimeout(async () => {
      try {
        const r = await api(`/${kind}?q=${encodeURIComponent(text)}&limit=300`);
        if (!alive() || text !== st.q) return;
        st.server = new Set(listItems(r, kind).map((x) => x.id));
        st.serverQ = text;
        draw();
      } catch { /* browser search only */ }
    }, 380);
  }

  async function load() {
    put(results, el('div.dk-skel', Array.from({ length: 4 }, () => el('div.dk-skel__card'))));
    try {
      st.items = await loadAll(kind);
      if (!alive()) return;
      for (const x of st.items) x._hay = hay(x.code, x.title, x.statement, x.effect, x.series, x.project, arr(x.theory_refs), x.kind, KIND[x.kind], STATUS[x.status], VERDICT[x.verdict], x.context ? Object.values(x.context) : null, x.method);
      draw();
      if (st.q) serverSearch();
    } catch (e) { put(results, el('div.alert.alert--error', `Liste nicht ladbar: ${e.message}`)); }
  }

  function writeHash() {
    const h = new URLSearchParams({ tab: isH ? 'hypothesen' : 'findings' });
    for (const k of ['q', 'series', 'verdict', 'status', 'theory', 'ip', 'flag', 'strength']) if (st[k]) h.set(k, st[k]);
    if (st.kindF) h.set('kind', st.kindF);
    if (st.sort !== (isH ? 'code' : 'updated')) h.set('sort', st.sort);
    const s = `#forschung?${h}`;
    if (location.hash !== s) history.replaceState(null, '', s);
  }

  function passes(x, except) {
    const tokens = st.q.toLowerCase().split(/\s+/).filter(Boolean);
    const srv = st.server && st.serverQ === st.q && st.server.has(x.id);
    if (tokens.length && !srv && !tokens.every((t) => x._hay.includes(t))) return false;
    if (st.series && (st.series === 'none' ? x.series : (x.series?.slug ?? x.series) !== st.series)) return false;
    if (except !== 'verdict' && st.verdict && (st.verdict === 'none' ? !!x.verdict : x.verdict !== st.verdict)) return false;
    if (st.status && x.status !== st.status) return false;
    if (st.kindF && x.kind !== st.kindF) return false;
    if (st.theory) {
      const [ts, tid] = st.theory.includes('|') ? st.theory.split('|') : ['', st.theory];
      if (!arr(x.theory_refs).includes(tid) || (ts && (x.series?.slug ?? x.series) !== ts)) return false;
    }
    if (st.strength && x.strength !== st.strength) return false;
    const ipState = x.interpretation_state ?? (x.interpretation_complete ? 'complete' : 'missing');
    if (st.ip === 'missing' && (ipState === 'complete' || (isH && !x.verdict))) return false;
    if (st.ip === 'complete' && ipState !== 'complete') return false;
    if (st.flag && !arr(x.flags).includes(st.flag)) return false;
    return true;
  }

  function sorter() {
    const vi = (x) => (x.verdict ? VERDICTS.indexOf(x.verdict) : 9);
    return {
      code: (a, b) => codeKey(a.code).localeCompare(codeKey(b.code)),
      result: (a, b) => String(b.result_at ?? (b.verdict ? b.updated_at : '') ?? '').localeCompare(String(a.result_at ?? (a.verdict ? a.updated_at : '') ?? '')) || codeKey(a.code).localeCompare(codeKey(b.code)),
      created: (a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? '')),
      verdict: (a, b) => vi(a) - vi(b) || codeKey(a.code).localeCompare(codeKey(b.code)),
      n: (a, b) => (b.n ?? -1) - (a.n ?? -1),
      updated: (a, b) => String(b.updated_at ?? b.created_at ?? '').localeCompare(String(a.updated_at ?? a.created_at ?? '')),
      strength: (a, b) => STRENGTH.indexOf(b.strength) - STRENGTH.indexOf(a.strength),
      evidence: (a, b) => (b.evidence_n ?? 0) - (a.evidence_n ?? 0),
    }[st.sort] ?? (() => 0);
  }

  function draw() {
    if (!st.items) return;
    writeHash();
    const shown = st.items.filter((x) => passes(x)).sort(sorter());
    if (isH) {
      const base = st.items.filter((x) => passes(x, 'verdict'));
      const counts = Object.fromEntries(VERDICTS.map((v) => [v, base.filter((x) => x.verdict === v).length]));
      put(summary, el('div.fo-listsum__bar', verdictBar(counts, { href: (v) => { const h = new URLSearchParams(location.hash.split('?')[1]); h.set('verdict', v); return `#forschung?${h}`; } })),
        el('span.small.muted', `${fmt.int(base.filter((x) => !x.verdict).length)} ohne Ergebnis`));
    }
    const total = st.items.length;
    const chips = [];
    const chip = (label, key) => chips.push(el('button.dk-chip', { type: 'button', title: 'Filter entfernen', on: { click: () => { st[key] = ''; if (controls[key]) controls[key].value = ''; draw(); } } },
      label, el('span.dk-chip__x', { 'aria-hidden': 'true' }, '×')));
    if (st.theory) chip(`Theorie: ${arr(board?.evidence_map?.rows).find((r) => tkey(r) === st.theory)?.name ?? st.theory}`, 'theory');
    if (st.flag) chip(FLAG[st.flag] ?? st.flag, 'flag');
    put(info, el('span.dk-count', shown.length === total ? `${fmt.int(total)} ${isH ? (total === 1 ? 'Hypothese' : 'Hypothesen') : (total === 1 ? 'Finding' : 'Findings')}`
      : `${fmt.int(shown.length)} von ${fmt.int(total)}`), st.server && st.serverQ === st.q ? el('span.small.muted', ' · auch im Text von Präregistrierung, Ergebnis und Interpretation gesucht') : null, ...chips);
    if (!shown.length) { put(results, emptyNote(total ? 'Nichts passt zu Suche und Filtern.' : isH ? 'Noch keine Hypothesen.' : 'Noch keine Findings.')); return; }
    const list = shown.slice(0, st.limit);
    const more = shown.length > list.length ? el('div.row.dk-more', el('button.btn', { type: 'button', on: { click: () => { st.limit += PAGE; draw(); } } }, `Weitere ${Math.min(PAGE, shown.length - list.length)} anzeigen`),
      el('button.btn.btn--ghost', { type: 'button', on: { click: () => { st.limit = Infinity; draw(); } } }, `Alle ${fmt.int(shown.length)} anzeigen`)) : null;
    put(results, isH ? hypTable(list) : el('div.fo-fcards', list.map(findingCard)), more);
  }

  function hypTable(list) {
    // effect and n columns only when the list carries numbers (imported briefs have neither)
    const hasCI = list.some((x) => Number.isFinite(x.estimate));
    const hasN = list.some((x) => x.n != null);
    return el('div.card.dk-tablecard', el('div.table-wrap', el('table.table.table--compact.fo-htable',
      el('thead', el('tr', el('th', { scope: 'col' }, 'Code'), el('th', { scope: 'col' }, 'Hypothese'), el('th', { scope: 'col' }, 'Status'), el('th', { scope: 'col' }, 'Urteil'),
        hasCI ? el('th.fo-col-ci', { scope: 'col', title: 'Schätzer mit 95-%-Konfidenzintervall, Null in der Mitte' }, 'Effekt') : null, hasN ? el('th.num.fo-col-n', { scope: 'col' }, 'Fallzahl') : null, el('th', { scope: 'col' }, 'Interpretation'))),
      el('tbody', list.map((x) => {
        const tr = el('tr.clickable', { tabindex: '0' },
          el('td.fo-col-code', el('a', { href: hypHref(x) }, x.code ?? `#${x.id}`)),
          el('td.fo-col-title', el('a', { href: hypHref(x) }, x.title), el('div.small.muted', kids(seriesLabel(x.series), KIND[x.kind], (x.frozen_at ?? x.preregistered_at) ? `präregistriert ${day(x.frozen_at ?? x.preregistered_at)}` : x.verdict ? 'ohne Präregistrierung' : null).join(' · '))),
          el('td.fo-col-st', statusChip(x.status)),
          el('td.fo-col-v', vpill(x.verdict, { none: '–' })),
          hasCI ? el('td.fo-col-ci', ciGlyph(x) ?? el('span.muted', '–')) : null,
          hasN ? el('td.num.fo-col-n', x.n != null ? fmt.int(x.n) : '–') : null,
          el('td.fo-col-ip', x.verdict ? interpChip(x.interpretation_state ?? (x.interpretation_complete ? 'complete' : 'missing'), true) : null,
            arr(x.flags).includes('data_newer_than_result') ? el('div', el('span.fo-flagchip', { title: FLAG.data_newer_than_result }, 'Daten neuer')) : null));
        const go = (e) => { if (e.target.closest('a')) return; location.hash = hypHref(x); };
        tr.addEventListener('click', go);
        tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(e); });
        return tr;
      })))));
  }

  function findingCard(x) {
    return el('a.card.fo-fcard', { href: findHref(x) },
      el('div.fo-fcard__top', el('span.fo-fcard__code', x.code ?? `F${x.id}`), exploratoryChip(), kindChip(x.kind), x.status === 'draft' ? el('span.fo-status', FSTATUS.draft) : null),
      el('h3.fo-fcard__title', x.title),
      strengthScale(x.strength),
      el('p.small.muted.fo-fcard__meta', kids(seriesLabel(x.series), x.project, x.evidence_n != null ? `${x.evidence_n} ${x.evidence_n === 1 ? 'Beleg' : 'Belege'}${x.quotes_n ? `, davon ${x.quotes_n} ${x.quotes_n === 1 ? 'Zitat' : 'Zitate'}` : ''}` : null, day(x.updated_at ?? x.created_at)).join(' · ')),
      el('div.fo-fcard__foot', interpChip(x.interpretation_state ?? (x.interpretation_complete ? 'complete' : 'missing')),
        arr(x.derived_hypotheses).length ? el('span.small', `→ ${arr(x.derived_hypotheses).join(', ')}`) : null));
  }

  load();
}
