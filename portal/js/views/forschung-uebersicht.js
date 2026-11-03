/**
 * Forschung › Übersicht (#forschung): key figures, search over hypotheses and findings, series cards (status board
 * Idee · präregistriert · läuft · Ergebnis and verdict bar), the work list (Interpretation fehlt, Daten neuer als Ergebnis,
 * Idee ohne Präregistrierung) and the evidence map theory × verdict with findings as their own mark.
 * Data: GET /api/research (board) – passed in from forschung.js; search: GET /api/research/hypotheses?q=, /findings?q=.
 */
import { el, card, chartFrame, emptyNote } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { arr, kids, api, put, listItems, VERDICTS, VERDICT, BOARD, STATUS, FLAG, vpill, verdictBar, verdictLegend, interpChip, exploratoryChip, hypHref, findHref, itemHref,
  seriesLabel, day } from './forschung-lib.js';
import { evidenceMap, MAP_COLS, verdictKey } from './forschung-svg.js';

const WORK = [
  { key: 'interpretation_missing', title: 'Interpretation fehlt', sub: 'Ergebnisse und Findings ohne vollständige Interpretation (Pflicht: Aussage, Erklärung, Grenzen, Theorie).', tab: 'interpretation' },
  { key: 'data_newer_than_result', title: 'Daten neuer als Ergebnis', sub: 'Ein Eingangsdatensatz hat inzwischen eine neuere Version als die, mit der gerechnet wurde.', tab: 'herkunft' },
  { key: 'idea_without_prereg', title: 'Idee ohne Präregistrierung', sub: 'Seit mehr als 14 Tagen erfasst, aber noch nicht eingefroren.', tab: 'prereg' },
];
const WORK_SHOW = 6;

export function render(root, ctx, board, alive) {
  const s = board.stats ?? {};
  const v = s.verdicts ?? {};
  const withResult = VERDICTS.reduce((a, k) => a + (v[k] ?? 0), 0);
  const wl = board.worklist ?? {};
  const wlN = WORK.reduce((a, w) => a + arr(wl[w.key]).length, 0);
  const tile = (label, value, foot, href) => el(href ? 'a.kpi.dk-kpi.fo-kpi' : 'div.kpi.kpi--static.dk-kpi.fo-kpi', { href: href ?? null },
    el('span.kpi__label', label), el('span.kpi__value', value), foot ? el('span.kpi__foot', foot) : null);
  const kpis = el('div.fo-kpis',
    tile('Hypothesen', fmt.int(s.hypotheses), `${fmt.int(arr(board.series).filter((x) => x.slug).length)} Serien`, '#forschung?tab=hypothesen'),
    tile('mit Ergebnis', fmt.int(withResult), `${fmt.int((s.hypotheses ?? 0) - withResult)} noch ohne`, '#forschung?tab=hypothesen&sort=result'),
    tile('Findings', fmt.int(s.findings), 'explorativ, ohne vorherige Hypothese', '#forschung?tab=findings'),
    tile('Arbeitsliste', fmt.int(wlN), 'offene Punkte', '#fo-work'));
  const all = card({ title: 'Alle Urteile', accent: 'Urteile', sub: 'Jedes Urteil zählt gleich – nicht gestützte und unentschiedene Ergebnisse sind Ergebnisse. Klick auf ein Segment zeigt die Hypothesen.' },
    verdictBar(v, { href: (k) => `#forschung?tab=hypothesen&verdict=${k}` }), verdictLegend(v));

  root.append(kpis, searchBox(alive), all,
    el('h2.fo-h2', 'Serien'), el('div.fo-series', arr(board.series).filter((x) => x.hypotheses || x.findings).map(seriesCard)),
    workCard(wl), mapCard(board.evidence_map));
}

// ---------------------------------------------------------------- search over hypotheses and findings
function searchBox(alive) {
  const input = el('input.input.fo-search__q', { type: 'search', placeholder: 'In Hypothesen und Findings suchen – Aussage, Präregistrierung, Ergebnis, Interpretation, Zitat …', 'aria-label': 'Forschung durchsuchen' });
  const out = el('div.fo-search__out', { 'aria-live': 'polite' });
  let tmr, run = 0;
  input.addEventListener('input', () => {
    clearTimeout(tmr);
    const text = input.value.trim();
    if (text.length < 2) { put(out); return; }
    tmr = setTimeout(async () => {
      const my = ++run;
      try {
        const [hs, fs] = await Promise.all([api(`/hypotheses?q=${encodeURIComponent(text)}&limit=8`), api(`/findings?q=${encodeURIComponent(text)}&limit=6`)]);
        if (!alive() || my !== run) return;
        const H = listItems(hs, 'hypotheses'), F = listItems(fs, 'findings');
        put(out, !H.length && !F.length ? el('p.small.muted', 'Nichts gefunden.') : el('div.fo-search__res',
          H.length ? el('div', el('span.label', `Hypothesen${hs.total > H.length ? ` (${fmt.int(hs.total)})` : ''}`), el('ul.fo-hits', H.map((h) => el('li', el('a', { href: hypHref(h) }, el('strong', h.code ?? ''), ' ', h.title),
            el('span.fo-hits__meta', vpill(h.verdict, { none: STATUS[h.status] ?? '–' }))))),
          el('a.small', { href: `#forschung?tab=hypothesen&q=${encodeURIComponent(text)}` }, 'Alle in der Liste →')) : null,
          F.length ? el('div', el('span.label', 'Findings'), el('ul.fo-hits', F.map((f) => el('li', el('a', { href: findHref(f) }, el('strong', f.code ?? ''), ' ', f.title), el('span.fo-hits__meta', exploratoryChip()))))) : null));
      } catch (e) { if (my === run) put(out, el('p.small.muted', `Suche nicht möglich: ${e.message}`)); }
    }, 220);
  });
  return el('div.card.fo-search', el('label.label', { for: 'fo-search-q' }, 'Suchen'), Object.assign(input, { id: 'fo-search-q' }), out);
}

// ---------------------------------------------------------------- series cards
function seriesCard(x) {
  const st = x.status ?? {};
  const href = x.slug ? `#forschung?tab=hypothesen&series=${encodeURIComponent(x.slug)}` : '#forschung?tab=hypothesen&series=none';
  const others = kids(st.revised ? `${st.revised} überarbeitet` : null, st.withdrawn ? `${st.withdrawn} zurückgezogen` : null).join(' · ');
  return el('section.card.fo-scard',
    el('div.fo-scard__top', el('span.label', x.slug ? seriesLabel(x.slug) : 'Einzeln'), x.programme ? el('span.small.muted', x.programme) : null),
    el('h3.fo-scard__title', el('a', { href }, x.title)),
    x.summary ? el('p.fo-scard__sum', x.summary) : null,
    !x.hypotheses ? el('p.small.muted', 'Nur Findings – keine Hypothesen in dieser Serie.') : el('ol.fo-board', { 'aria-label': 'Status der Hypothesen' }, BOARD.map((k, i) => el('li', { class: `fo-board__col${st[k] ? '' : ' is-zero'}` },
      el('a', { href: `${href}&status=${k}` }, el('span.fo-board__n', fmt.int(st[k] ?? 0)), el('span.fo-board__l', STATUS[k])),
      i < BOARD.length - 1 ? el('span.fo-board__arrow', { 'aria-hidden': 'true' }, '→') : null))),
    others ? el('p.small.muted.fo-scard__others', others) : null,
    x.hypotheses ? verdictBar(x.verdicts, { href: (k) => `${href}&verdict=${k}` }) : null,
    el('div.fo-scard__foot',
      !x.hypotheses ? null : el('a', { href }, `${fmt.int(x.hypotheses)} ${x.hypotheses === 1 ? 'Hypothese' : 'Hypothesen'}`),
      x.findings ? el('a', { href: `#forschung?tab=findings&series=${encodeURIComponent(x.slug ?? 'none')}` }, `${fmt.int(x.findings)} ${x.findings === 1 ? 'Finding' : 'Findings'}`) : null,
      x.interpretation_missing ? el('a.fo-scard__miss', { href: `${href}&ip=missing` }, `${fmt.int(x.interpretation_missing)} ohne Interpretation`) : null,
      x.updated_at ? el('span.small.muted', `zuletzt ${day(x.updated_at)}`) : null));
}

// ---------------------------------------------------------------- work list
function workCard(wl) {
  const box = el('section.card.fo-work#fo-work', el('div.card__head', el('h3', el('em', 'Arbeitsliste'))),
    el('p.card__sub', 'Was offen ist, damit jedes Ergebnis vollständig, aktuell und präregistriert dasteht.'));
  const cols = el('div.fo-work__cols');
  for (const w of WORK) {
    const items = arr(wl[w.key]);
    const list = el('ul.fo-work__list');
    let open = false;
    const fill = () => {
      put(list, (open ? items : items.slice(0, WORK_SHOW)).map((x) => el('li',
        el('a', { href: itemHref(x, w.tab) }, el('strong', x.code ?? ''), ' ', x.title),
        el('span.fo-work__meta', x.type === 'finding' ? exploratoryChip() : x.verdict ? vpill(x.verdict) : null,
          w.key === 'data_newer_than_result' && x.detail ? el('span.small.muted', x.detail) : null,
          w.key === 'idea_without_prereg' && x.days != null ? el('span.small.muted', `seit ${x.days} Tagen`) : null,
          w.key === 'interpretation_missing' && x.interpretation_state === 'partial' ? el('span.small.muted', 'unvollständig') : null))),
      items.length > WORK_SHOW ? el('li.fo-work__more', el('button.btn.btn--ghost.btn--small', { type: 'button', on: { click: () => { open = !open; fill(); } } },
        open ? 'weniger' : `alle ${fmt.int(items.length)} zeigen`)) : null);
    };
    fill();
    cols.append(el('div.fo-work__col', el('h4', w.title, el('span.fo-work__n', fmt.int(items.length))), el('p.small.muted', w.sub),
      items.length ? list : el('p.small.fo-work__none', 'Nichts offen.')));
  }
  box.append(cols);
  return box;
}

// ---------------------------------------------------------------- evidence map
let mapSeries = null;   // series shown in the evidence map (theory ids are per series); kept while the view lives
function mapCard(em) {
  const all = arr(em?.rows);
  const f = chartFrame({ title: 'Evidenzkarte: Theorie × Urteil', accent: 'Evidenzkarte', sub: 'Je Theorie: wie viele Hypothesen gestützt, teilweise gestützt, nicht gestützt oder unentschieden sind, wie viele noch ohne Ergebnis – und Findings als eigene, explorative Marke. Fläche wächst mit der Zahl. Klick öffnet die Liste.' });
  if (!all.length) { f.plot.append(emptyNote('Noch keine Theorien verknüpft.')); return f.el; }
  const tally = (r) => MAP_COLS.reduce((a, c) => a + (r[c.key] ?? 0), 0);
  const seriesList = [...new Set(all.map((r) => r.series ?? ''))];
  const bySeries = new Map(seriesList.map((sl) => [sl, all.filter((r) => (r.series ?? '') === sl)]));
  if (mapSeries === null || !bySeries.has(mapSeries)) {
    // default: the series with the most results on the map
    mapSeries = seriesList.slice().sort((x, y) => bySeries.get(y).reduce((a, r) => a + tally(r), 0) - bySeries.get(x).reduce((a, r) => a + tally(r), 0))[0];
  }
  const sel = seriesList.length > 1 ? el('select.input.fo-sel', { 'aria-label': 'Serie der Evidenzkarte' },
    seriesList.map((sl) => el('option', { value: sl }, `${sl ? seriesLabel(sl) : 'ohne Serie'} (${bySeries.get(sl).length} Theorien)`))) : null;
  if (sel) { sel.value = mapSeries; f.el.querySelector('.chart__tools')?.prepend(sel); }
  const draw = () => {
    const rows = bySeries.get(mapSeries) ?? [];
    put(f.plot, evidenceMap(rows, (r, col) => {
      if (col === 'findings') location.hash = `#forschung?tab=findings${r.series ? `&series=${encodeURIComponent(r.series)}` : ''}`;
      else location.hash = `#forschung?tab=hypothesen&theory=${encodeURIComponent(r.key ?? `${r.series ?? ''}|${r.theory}`)}${col === 'open' ? '&verdict=none' : `&verdict=${col}`}`;
    }));
    f.table(MAP_COLS.reduce((cs, c) => cs.concat([{ key: c.key, label: c.label, num: true, fmt: (n) => fmt.int(n ?? 0) }]), [{ key: 'name', label: 'Theorie' }]), rows);
  };
  sel?.addEventListener('change', () => { mapSeries = sel.value; draw(); });
  draw();
  f.legend.append(verdictKey());
  return f.el;
}
export { VERDICT, FLAG, interpChip };
