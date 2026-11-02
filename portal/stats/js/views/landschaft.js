/**
 * Ansicht „Landschaft“: C4-Deployment-Karte mit Zonen-Bändern, Verbindungs-Tabelle,
 * darunter die Hardware-Landschaft (Schächte, Volumen, Steckbriefe).
 */
import { el, pageHead, chartFrame, card, legend, statePill, dl, mark, headline, emptyNote } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { STATES, norm, worst } from '../lib/state.js';
import { servicesOf } from '../lib/derive.js';
import { renderLandscape, LINK_LABEL as LL } from '../charts/landscape.js';
import { bayStrip, volumeBars, HEALTH } from './hardware.js';

const LINK_LABEL = { backup: 'Sicherung', 'ssh-collect': 'Erhebung', data: 'Datenzugriff', serves: 'bietet an' };

export function render(root, data, ctx) {
  const nodes = data.nodes;
  const problem = nodes.filter((n) => ['warn', 'crit'].includes(norm(n.state)));
  const unknown = nodes.filter((n) => norm(n.state) === 'unknown');
  const lede = nodes.length
    ? `${problem.length ? problem.map((n) => `${n.label ?? n.id} ${STATES[norm(n.state)].word.toLowerCase()}` ).join(', ') : 'Alle Knoten ohne Befund'}${unknown.length ? ` · ${unknown.map((n) => n.label ?? n.id).join(', ')} ohne Daten` : ''}.`
    : 'Keine Knoten geliefert.';
  root.append(pageHead({ title: `${numWord(nodes.length)} Knoten in drei Zonen`, accent: 'Zonen', lede }));
  const ins = ctx.insight('landschaft'); if (ins) root.append(ins);

  const frame = chartFrame({ title: 'Deployment-Karte', accent: 'Karte', sub: 'Zonen als Bänder: je höher, desto erreichbarer. Knoten mit Diensten als Chips, Tailnet-Marke als Pin im Tailnet-Band. Verbindungen laufen im Kanal unter den Karten; die Nummern verweisen auf die Liste darunter. Klick auf einen Knoten öffnet Details.',
    legend: legend([
      { mark: 'ok', label: 'läuft' }, { mark: 'warn', label: 'Achtung' }, { mark: 'crit', label: 'Kritisch' }, { mark: 'off', label: 'absichtlich aus' }, { mark: 'unknown', label: 'keine Daten' },
      { line: true, swatch: 'var(--chart-benchmark)', label: 'Sicherung' }, { line: true, dashed: true, swatch: 'var(--ink-4)', label: 'Erhebung (SSH)' }, { line: true, dashed: true, swatch: 'var(--chart-benchmark)', label: 'Datenzugriff' },
    ]), hint: 'Verbindung oder Knoten berühren für Details.' });
  const c = el('section.card');
  c.append(frame.el);
  const linkList = el('div.map-legend-wrap');
  frame.legend.append(linkList);
  root.append(c);
  const cleanup = renderLandscape(frame.plot, data, { legend: linkList, onNode: (id) => ctx.openNode(id), onLinkHover: (l) => frame.setHint(l ? `${l.from} → ${l.to}: ${l.label ?? LL[l.kind] ?? ''} · ${STATES[norm(l.state)].word}` : 'Verbindung oder Knoten berühren für Details.') });
  frame.table([
    { key: 'from', label: 'Von' }, { key: 'to', label: 'Nach' }, { key: 'kind', label: 'Art', fmt: (v) => LINK_LABEL[v] ?? v ?? '–' },
    { key: 'label', label: 'Was' }, { key: 'state', label: 'Zustand', render: (r) => statePill(r.state) }, { key: 'service', label: 'Dienst', fmt: (v) => data.services.find((s) => s.id === v)?.label ?? v ?? '–' },
  ], data.links.filter((l) => data.nodes.some((n) => n.id === l.from)));

  // Hardware-Landschaft
  root.append(headline('h2', 'Hardware-Landschaft', 'Hardware'));
  const grid = el('div.grid.grid--2');
  for (const n of nodes) grid.append(hardwareCard(n, data, ctx));
  if (!nodes.length) grid.append(emptyNote('Keine Knoten.'));
  root.append(grid);
  return cleanup;
}

function hardwareCard(n, data, ctx) {
  const hw = n.hardware ?? {};
  const c = card({ title: `${n.label ?? n.id}`, accent: null, sub: [hw.model, n.role].filter(Boolean).join(' · ') || 'keine Hardware-Angaben',
    tools: [statePill(n.state), el('button.btn.btn--small', { type: 'button', on: { click: () => ctx.openNode(n.id) } }, 'Details')] });
  if (n.disks?.length) {
    { const ds = n.disks.filter((d) => d.model); const cnt = (st) => ds.filter((d) => (d.smart ?? 'unknown') === st).length;
      const parts = [cnt('ok') && `${cnt('ok')} gesund`, cnt('warn') && `${cnt('warn')} Achtung`, cnt('crit') && `${cnt('crit')} kritisch`, cnt('unknown') && `${cnt('unknown')} ohne SMART-Daten`].filter(Boolean);
      c.append(el('div.label', `${ds.length} Laufwerke · ${parts.join(' · ') || 'SMART unbekannt'}`)); }
    c.append(bayStrip(n.disks));
  } else {
    c.append(dl([
      ['CPU', hw.cpu ?? null], ['RAM', fmt.bytes(hw.ramBytes)], ['GPU', hw.gpu ?? null],
      ['System', n.os?.name ? `${n.os.name} ${n.os.version ?? ''}`.trim() : null],
      ['Uptime', fmt.duration(n.vitals?.uptimeS)], ['Temperatur', fmt.temp(n.vitals?.tempC)],
    ]));
  }
  if (n.volumes?.length) { c.append(el('div.label', 'Volumen')); c.append(volumeBars(n.volumes)); }
  else if (n.state !== 'unknown') c.append(el('p.small.muted', 'Keine Volumen gemeldet.'));
  if (norm(n.state) === 'unknown') c.append(el('div.empty', mark('unknown'), n.stateReason ?? 'Keine Daten'));
  return c;
}
function numWord(n) { return ['Keine', 'Ein', 'Zwei', 'Drei', 'Vier', 'Fünf', 'Sechs', 'Sieben', 'Acht', 'Neun', 'Zehn'][n] ?? String(n); }
