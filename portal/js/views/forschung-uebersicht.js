/**
 * Forschung › Übersicht nach Theorie und Serie (#forschung?view=uebersicht): a secondary page with two plain count
 * tables – per series, and per theory within a chosen series (theory ids are per series). Every count links into the
 * list with the matching filters. No colours, no shapes.
 * Data: GET /api/research (board, evidence_map), normalised by normBoard.
 */
import { el } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { arr, api, put, VERDICTS, VERDICT, seriesLabel, normBoard } from './forschung-lib.js';
import { backLink } from './forschung-finding.js';

let mapSeries = null;   // chosen series of the theory table, kept while the portal is open

export function render(root, ctx, alive) {
  const body = el('div.fo-overview', el('p.fo-muted', 'Lade …'));
  root.append(backLink(), el('h1.fo-title', 'Übersicht nach Theorie und Serie'),
    el('p.fo-summary', 'Wie viele Hypothesen je Serie und je Theorie gestützt, teilweise gestützt, nicht gestützt oder unentschieden sind. Jede Zahl führt in die gefilterte Liste.'), body);
  api('').then(normBoard).then((b) => { if (alive()) put(body, seriesTable(b.series), theoryTable(arr(b.evidence_map?.rows))); })
    .catch((e) => put(body, el('p', `Nicht ladbar: ${e.message}`)));
}

const list = (params) => `#forschung?${new URLSearchParams(params)}`;
const cell = (n, href) => el('td.num', n ? el('a', { href }, fmt.int(n)) : el('span.fo-muted', '0'));

function seriesTable(series) {
  const rows = arr(series).filter((s) => s.hypotheses || s.findings);
  return el('section.fo-sec', el('h2', 'Nach Serie'), el('div.fo-tablewrap', el('table.fo-ctable',
    el('thead', el('tr', el('th', 'Serie'), el('th.num', 'Hypothesen'), ...VERDICTS.map((v) => el('th.num', VERDICT[v])), el('th.num', 'Findings'), el('th.num', 'ohne Interpretation'))),
    el('tbody', rows.map((s) => {
      const sl = s.slug ?? 'none';
      return el('tr', el('td', el('a', { href: list({ series: sl }) }, s.slug ? seriesLabel(s) : 'ohne Serie'), /^serie-\d/.test(s.slug ?? '') ? el('div.fo-muted', s.title ?? '') : null),
        cell(s.hypotheses, list({ series: sl, art: 'h' })),
        ...VERDICTS.map((v) => cell(s.verdicts?.[v], list({ series: sl, result: v }))),
        cell(s.findings, list({ series: sl, art: 'f' })),
        cell(s.interpretation_missing, list({ series: sl, ip: 'missing' })));
    })))));
}

function theoryTable(all) {
  const box = el('section.fo-sec', el('h2', 'Nach Theorie'));
  if (!all.length) { box.append(el('p.fo-muted', 'Noch keine Theorien verknüpft.')); return box; }
  const seriesKeys = [...new Set(all.map((r) => r.series ?? ''))];
  const total = (sl) => all.filter((r) => (r.series ?? '') === sl).reduce((a, r) => a + VERDICTS.reduce((b, v) => b + (r[v] ?? 0), 0), 0);
  if (mapSeries === null || !seriesKeys.includes(mapSeries)) mapSeries = seriesKeys.slice().sort((a, b) => total(b) - total(a))[0];
  const sel = el('select.input.fo-select', { 'aria-label': 'Serie' }, seriesKeys.map((sl) => el('option', { value: sl }, sl ? seriesLabel(sl) : 'ohne Serie')));
  sel.value = mapSeries;
  const wrap = el('div.fo-tablewrap');
  const draw = () => {
    const rows = all.filter((r) => (r.series ?? '') === mapSeries);
    put(wrap, el('table.fo-ctable',
      el('thead', el('tr', el('th', 'Theorie'), ...VERDICTS.map((v) => el('th.num', VERDICT[v])), el('th.num', 'noch kein Ergebnis'), el('th.num', 'Findings'))),
      el('tbody', rows.map((r) => {
        const th = `${r.series ?? ''}|${r.theory}`;
        return el('tr', el('td', r.name ?? r.theory, arr(r.scholars).length ? el('div.fo-muted', arr(r.scholars).join('; ')) : null),
          ...VERDICTS.map((v) => cell(r[v], list({ theory: th, result: v }))),
          cell(r.open, list({ theory: th, result: 'none' })),
          cell(r.findings, list({ theory: th, art: 'f' })));
      }))));
  };
  sel.addEventListener('change', () => { mapSeries = sel.value; draw(); });
  draw();
  box.append(el('label.fo-form__row.fo-form__row--inline', el('span', 'Serie'), sel), wrap);
  return box;
}
