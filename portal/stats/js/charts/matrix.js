/**
 * Host×Port-Matrix – HTML-Tabelle im Circle-Stil (pixelgenau, WebKit-sicher):
 *   Zeilen nach Bewertung gruppiert (Unerwartet offen → Unbewertet → Erwartet, letzte
 *   Gruppe eingeklappt), Spalten = Knoten (breit, mit Zustandsform im Kopf),
 *   Zelle = Reichweite als Wort mit zurückhaltender Tönung, kräftig nur bei „unerwartet“,
 *   gestrichelte Kontur bei „unbewertet“, Schloss-Symbol bei „ohne Anmeldung“.
 *   Rechts eine breite Beschreibungsspalte (Prozess/Dienst · Hinweis).
 *   Oben je Knoten ein gestapelter Balken unerwartet/unbewertet/erwartet.
 *
 * portMatrix(container, ports, nodes, services) → {rows, cols}
 */
import { el, mark, badge } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import * as tip from '../lib/tooltip.js';
import { worst } from '../lib/state.js';

export const SCOPE_LABEL = { all: 'alle Schnittstellen', lan: 'LAN', tailnet: 'Tailnet', localhost: 'nur localhost' };
const SCOPE_SHORT = { all: 'alle', lan: 'LAN', tailnet: 'Tailnet', localhost: 'lokal' };
const SCOPE_RANK = { all: 3, lan: 2, tailnet: 1, localhost: 0 };
const GROUPS = [
  { id: 'unexpected', label: 'Unerwartet offen', sub: 'nicht in der Soll-Liste – schliessen oder bewusst aufnehmen', test: (p) => p.expected === false, open: true },
  { id: 'unrated', label: 'Unbewertet', sub: 'noch ohne Eintrag in ports-expected.json', test: (p) => p.expected === null || p.expected === undefined, open: true },
  { id: 'expected', label: 'Erwartet', sub: 'laut Soll-Liste in Ordnung', test: (p) => p.expected === true, open: false },
];

export function portMatrix(container, ports, nodes, services = []) {
  const cols = nodes.filter((n) => ports.some((p) => p.node === n.id));
  const svcLabel = (id) => services.find((s) => s.id === id)?.label ?? null;
  const keyOf = (p) => `${p.port}/${p.proto ?? 'tcp'}`;

  // Zusammenfassung je Knoten als gestapelter Balken
  const summary = el('div.pm-summary');
  for (const c of cols) {
    const mine = ports.filter((p) => p.node === c.id);
    const counts = GROUPS.map((g) => mine.filter(g.test).length);
    const total = mine.length || 1;
    const bar = el('div.pm-summary__bar');
    GROUPS.forEach((g, i) => { if (counts[i]) bar.append(el('span', { class: `pm-summary__seg pm-summary__seg--${g.id}`, style: { width: `${(100 * counts[i]) / total}%` }, title: `${g.label}: ${counts[i]}` })); });
    summary.append(el('div.pm-summary__item', el('div.pm-summary__head', mark(c.state), el('strong', c.label ?? c.id), el('span.muted', `${mine.length} Sockets`)), bar,
      el('div.pm-summary__nums', ...GROUPS.map((g, i) => el('span', { class: `pm-summary__num pm-summary__num--${g.id}` }, `${counts[i]} ${g.label.toLowerCase().split(' ')[0]}`)))));
  }
  container.append(summary);

  // Tabelle
  const table = el('table.pm');
  const colgroup = el('colgroup', el('col', { class: 'pm__col-port' }), ...cols.map(() => el('col', { class: 'pm__col-node' })), el('col', { class: 'pm__col-desc' }));
  table.append(colgroup);
  table.append(el('thead', el('tr', el('th', { scope: 'col' }, 'Port'), ...cols.map((c) => el('th', { scope: 'col', class: 'pm__th-node' }, el('span.row', mark(c.state), c.label ?? c.id))), el('th', { scope: 'col' }, 'Prozess · Hinweis'))));
  const allRows = [];
  for (const g of GROUPS) {
    const gp = ports.filter(g.test);
    const rowMap = new Map();
    for (const p of gp) {
      const k = keyOf(p);
      if (!rowMap.has(k)) rowMap.set(k, { key: k, port: p.port, proto: p.proto ?? 'tcp', cells: new Map(), rank: 0, group: g.id, ports: [] });
      const r = rowMap.get(k);
      r.cells.set(p.node, p); r.ports.push(p);
      r.rank = Math.max(r.rank, SCOPE_RANK[p.scope] ?? 0);
    }
    const rows = [...rowMap.values()].sort((a, b) => b.rank - a.rank || a.port - b.port);
    const tbody = el('tbody', { class: `pm__group pm__group--${g.id}`, 'data-open': String(g.open || rows.length <= 3) });
    const head = el('tr.pm__group-row');
    const btn = el('button.pm__group-btn', { type: 'button', 'aria-expanded': String(tbody.dataset.open === 'true') },
      el('span.tree__caret', { 'aria-hidden': 'true' }, caret()), el('strong', g.label), el('span.badge', { class: `badge badge--${g.id === 'unexpected' ? 'crit' : g.id === 'unrated' ? 'unknown' : 'outline'}` }, `${rows.length} ${rows.length === 1 ? 'Port' : 'Ports'}`), el('span.small.muted', g.sub));
    btn.addEventListener('click', () => { const open = tbody.dataset.open !== 'true'; tbody.dataset.open = String(open); btn.setAttribute('aria-expanded', String(open)); });
    head.append(el('td', { colspan: String(cols.length + 2) }, btn));
    tbody.append(head);
    if (!rows.length) tbody.append(el('tr.pm__row', el('td', { colspan: String(cols.length + 2) }, el('span.small.muted', 'keine'))));
    for (const r of rows) {
      const tr = el('tr.pm__row');
      tr.append(el('td.pm__port', el('span.pm__portnum', String(r.port)), el('span.pm__proto', `/${r.proto}`)));
      for (const c of cols) {
        const p = r.cells.get(c.id);
        const td = el('td.pm__cell-td');
        if (p) {
          const cell = el('span.pm__cell', { class: `pm__cell pm__cell--${p.scope ?? 'unknown'} pm__cell--${p.expected === false ? 'unexpected' : p.expected === true ? 'expected' : 'unrated'}`, tabindex: 0, role: 'img',
            'aria-label': `${c.label} Port ${r.key}: ${SCOPE_LABEL[p.scope] ?? 'Reichweite unbekannt'}${p.expected === false ? ', unerwartet' : p.expected === true ? '' : ', unbewertet'}${p.auth === 'none' ? ', ohne Anmeldung' : ''}` });
          if (p.expected === false) cell.append(mark('crit'));
          cell.append(el('span', SCOPE_SHORT[p.scope] ?? '?'));
          if (p.auth === 'none') cell.append(el('span.pm__lock', { title: 'ohne Anmeldung' }, lock()));
          const content = { title: `${c.label ?? c.id} · Port ${r.key}`, rows: [['Prozess', p.process ?? null], ['Dienst', svcLabel(p.service)], ['Bindung', p.bind ?? null], ['Reichweite', SCOPE_LABEL[p.scope] ?? 'unbekannt'], ['Erwartet', p.expected === true ? 'ja' : p.expected === false ? 'nein' : 'unbewertet'], ['Anmeldung', { none: 'keine', yes: 'ja' }[p.auth] ?? 'unbekannt'], ['Seit', p.since ? fmt.ago(p.since) : null], ['Hinweis', p.note ?? null]] };
          cell.addEventListener('pointerenter', (e) => tip.show(e, content)); cell.addEventListener('pointermove', (e) => tip.move(e)); cell.addEventListener('pointerleave', tip.hide);
          cell.addEventListener('focus', (e) => { const b = e.target.getBoundingClientRect(); tip.show({ clientX: b.left + b.width / 2, clientY: b.top }, content); }); cell.addEventListener('blur', tip.hide);
          td.append(cell);
        } else td.append(el('span.pm__cell.pm__cell--empty', { 'aria-hidden': 'true' }, '–'));
        tr.append(td);
      }
      const procs = [...new Set(r.ports.map((p) => svcLabel(p.service) ?? p.process).filter(Boolean))];
      const notes = [...new Set(r.ports.map((p) => p.note).filter(Boolean))];
      const desc = el('td.pm__desc', el('span.pm__desc-main', procs.join(', ') || 'unbekannter Prozess'), notes.length ? el('span.pm__desc-note', { title: notes.join(' · ') }, notes.join(' · ')) : null);
      tr.append(desc);
      tbody.append(tr);
      allRows.push(r);
    }
    table.append(tbody);
  }
  container.append(el('div.pm-wrap', table));
  return { rows: allRows, cols };
}

function caret() {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 16 16'); s.setAttribute('class', 'tree__caret'); s.setAttribute('aria-hidden', 'true');
  const u = document.createElementNS('http://www.w3.org/2000/svg', 'use'); u.setAttribute('href', '#ic-caret'); s.append(u);
  return s;
}
function lock() {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 12 12'); s.setAttribute('width', '10'); s.setAttribute('height', '10'); s.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', 'M2.5 5.5h7v5h-7zM4 5.5V3.8a2 2 0 0 1 4 0'); p.setAttribute('fill', 'none'); p.setAttribute('stroke', 'currentColor'); p.setAttribute('stroke-width', '1.4');
  s.append(p);
  return s;
}
export { worst };
