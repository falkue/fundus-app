/**
 * Ansicht „Exposition“: Host×Port-Matrix, unerwartete Ports mit Begründung,
 * „Aus dem Internet sichtbar“ (InternetDB), TLS-Ablauf als Bullet.
 */
import { el, pageHead, chartFrame, card, legend, mark, statePill, badge, emptyNote, dl, segmented, clear } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { exposureStats } from '../lib/derive.js';
import { portMatrix, SCOPE_LABEL } from '../charts/matrix.js';
import { tlsBullets } from './dienste.js';
import { exposureChanges } from '../charts/history.js';

export function render(root, data, ctx) {
  const ex = exposureStats(data);
  const lede = ex.total ? `${ex.total} lauschende Sockets auf ${new Set(data.ports.map((p) => p.node)).size} Knoten · ${ex.unexpected.length} unerwartet · ${ex.unrated.length} unbewertet · ${ex.openNoAuth.length} ohne Anmeldung an allen Schnittstellen.` : 'Keine Port-Daten geliefert – die Angriffsfläche ist unbekannt.';
  root.append(pageHead({ title: ex.total ? `${ex.unexpected.length ? numWord(ex.unexpected.length) : 'Keine'} unerwartete ${ex.unexpected.length === 1 ? 'Öffnung' : 'Öffnungen'} nach aussen` : 'Angriffsfläche ohne Daten', accent: ex.total ? 'aussen' : 'Daten', lede }));
  const ins = ctx.insight('exposition'); if (ins) root.append(ins);

  const frame = chartFrame({ title: 'Host × Port', accent: 'Port', sub: 'Zeilen sind Ports, gruppiert nach Bewertung (Unerwartet → Unbewertet → Erwartet; Erwartete eingeklappt). Spalten sind Knoten. Die Zelle nennt die Reichweite der Bindung; kräftig koralle nur, wenn der Port nicht in der Soll-Liste steht, gestrichelt, wenn er noch nicht bewertet ist. Das Schloss markiert Dienste ohne Anmeldung.',
    legend: legend([
      { swatch: 'var(--reach-all)', label: 'alle Schnittstellen' }, { swatch: 'var(--reach-lan)', label: 'LAN' }, { swatch: 'var(--reach-tailnet)', label: 'Tailnet' }, { swatch: 'var(--reach-localhost)', label: 'nur localhost' },
      { mark: 'crit', label: 'unerwartet' }, { hatch: true, label: 'unbewertet' },
    ]) });
  const c = card({});
  c.append(frame.el);
  root.append(c);
  const FILTERS = {
    auffaellig: (p) => p.expected !== true || p.auth === 'none',
    aussen: (p) => p.scope === 'all' || p.scope === 'lan',
    alle: () => true,
  };
  let filter = 'auffaellig';
  const drawMatrix = () => {
    clear(frame.plot);
    const rows = data.ports.filter(FILTERS[filter]);
    if (!data.ports.length) { frame.plot.append(emptyNote('Keine Ports geliefert.')); return; }
    if (!rows.length) { frame.plot.append(emptyNote('Nichts in dieser Auswahl.')); return; }
    const r = portMatrix(frame.plot, rows, data.nodes, data.services);
    frame.setHint(`${r.rows.length} Ports · ${rows.length} Sockets in dieser Auswahl (von ${data.ports.length}). Zelle berühren: Bindung, Prozess, Bewertung, Hinweis.`);
  };
  const seg = segmented([
    { value: 'auffaellig', label: 'Auffällig', count: data.ports.filter(FILTERS.auffaellig).length },
    { value: 'aussen', label: 'LAN / alle', count: data.ports.filter(FILTERS.aussen).length },
    { value: 'alle', label: 'Alle', count: data.ports.length },
  ], filter, (v) => { filter = v; drawMatrix(); }, 'Auswahl');
  frame.el.querySelector('.chart__tools').prepend(seg.el);
  drawMatrix();
  frame.table([
    { key: 'node', label: 'Knoten' }, { key: 'port', label: 'Port', num: true, fmt: (v, r) => `${v}/${r.proto ?? 'tcp'}` }, { key: 'process', label: 'Prozess' }, { key: 'bind', label: 'Bindung' },
    { key: 'scope', label: 'Reichweite', fmt: (v) => SCOPE_LABEL[v] ?? '–' }, { key: 'expected', label: 'Erwartet', fmt: (v) => (v === true ? 'ja' : v === false ? 'nein' : 'unbewertet') },
    { key: 'auth', label: 'Anmeldung', fmt: (v) => ({ none: 'keine', yes: 'ja' }[v] ?? 'unbekannt') }, { key: 'note', label: 'Hinweis' },
  ], [...data.ports].sort((a, b) => a.node.localeCompare(b.node) || a.port - b.port));

  // N5 Änderungen der Exposition
  const ch = card({ title: 'Was sich geändert hat', accent: 'geändert', sub: 'Aus firstSeen/lastSeen der Ports: neu offen in den letzten 7 Tagen, geschlossene Ports der letzten 30 Tage, und wie lange Unbewertetes schon offen ist.' });
  const chWrap = el('div');
  exposureChanges(chWrap, data.ports, data.portsClosed ?? [], { data });
  ch.append(chWrap);
  root.append(ch);

  const grid = el('div.grid.grid--2');
  // Unerwartete Ports
  const un = card({ title: 'Unerwartete Ports', accent: 'Unerwartete', sub: 'Nicht in der Soll-Liste (ports-expected.json). Jede Zeile braucht eine Entscheidung: schliessen oder in die Soll-Liste.' });
  if (!ex.unexpected.length && !ex.unrated.length) un.append(emptyNote(ex.total ? 'Alle Ports entsprechen der Soll-Liste.' : 'Keine Port-Daten.'));
  const list = el('ul.todo');
  for (const p of [...ex.unexpected].sort((a, b) => rank(b) - rank(a))) {
    list.append(el('li.todo__item', mark(p.scope === 'all' || p.auth === 'none' ? 'crit' : 'warn'),
      el('div', el('div.todo__title', `${p.node} · ${p.port}/${p.proto ?? 'tcp'} ${p.process ? `(${p.process})` : ''}`), el('div.todo__detail', [SCOPE_LABEL[p.scope] ?? 'Reichweite unbekannt', p.bind ? `Bindung ${p.bind}` : null, p.auth === 'none' ? 'ohne Anmeldung' : null].filter(Boolean).join(' · ')), p.note ? el('div.todo__action', p.note) : null),
      el('div.todo__meta', badge('unerwartet', 'crit'))));
  }
  const MAX_UNRATED = 8;
  for (const p of ex.unrated.slice(0, MAX_UNRATED)) {
    list.append(el('li.todo__item', mark('unknown'),
      el('div', el('div.todo__title', `${p.node} · ${p.port}/${p.proto ?? 'tcp'} ${p.process ? `(${p.process})` : ''}`), el('div.todo__detail', [SCOPE_LABEL[p.scope] ?? 'Reichweite unbekannt', p.note].filter(Boolean).join(' · '))),
      el('div.todo__meta', badge('unbewertet', 'unknown'))));
  }
  if (list.children.length) un.append(list);
  if (ex.unrated.length > MAX_UNRATED) un.append(el('p.small.muted', `${ex.unrated.length - MAX_UNRATED} weitere unbewertete Ports stehen in der Tabellenansicht der Matrix. Bewertung: ports-expected.json auf Hero.`));
  grid.append(un);

  // Internet
  const inet = card({ title: 'Aus dem Internet sichtbar', accent: 'Internet', sub: 'Shodan InternetDB (wöchentlich aktualisiert), nur öffentliche Adressen.' });
  if (!data.internet.length) inet.append(emptyNote('Keine Aussensicht geliefert.'));
  for (const i of data.internet) {
    inet.append(dl([
      ['Knoten', i.label ?? i.node ?? null], ['Adresse', i.ip ?? null], ['Namen', (i.hostnames ?? []).join(', ') || null],
      ['Offene Ports', (i.ports ?? []).length ? el('span.row', ...(i.ports ?? []).map((p) => badge(String(p), p === 22 ? 'warning' : 'outline'))) : 'keine'],
      ['Stand', i.at ? fmt.ago(i.at) : null], ['Quelle', i.source ?? null],
    ]));
    const vulns = i.vulns ?? [];
    const kev = i.kev ?? [], epss = i.epssHigh ?? [];
    if (!(i.ports ?? []).length) inet.append(el('div.empty', mark(i.known === false ? 'ok' : 'unknown'), i.known === false ? 'InternetDB kennt diese Adresse nicht – keine offenen Ports bekannt.' : 'Keine offenen Ports gemeldet.'));
    if (kev.length) inet.append(el('div.label', `${kev.length} aktiv ausgenutzt (CISA KEV)`), el('div.row', ...kev.map((v) => badge(v, 'crit'))));
    if (epss.length) inet.append(el('div.label', `${epss.length} mit hoher Ausnutzungswahrscheinlichkeit (EPSS)`), el('div.row', ...epss.map((v) => badge(v, 'warning'))));
    if (vulns.length) {
      const rest = vulns.filter((v) => !kev.includes(v) && !epss.includes(v));
      inet.append(el('div.label', `${vulns.length} abgeleitete CVEs insgesamt`));
      inet.append(el('div.row', ...rest.slice(0, 12).map((v) => badge(v, 'outline')), rest.length > 12 ? el('span.small.muted', `+ ${rest.length - 12} weitere`) : null));
      inet.append(el('p.small.muted', 'Aus dem CPE-Versionsabgleich abgeleitet (Shodan). Nur KEV und hoher EPSS zählen als handlungsrelevant.'));
    }
    if ((i.cpes ?? []).length) inet.append(el('p.small.secondary', `Erkannte Software: ${i.cpes.map(cpeShort).join(', ')}`));
  }
  grid.append(inet);
  root.append(grid);

  // TLS
  const tlsApps = data.apps.filter((a) => a.tls !== undefined);
  const tls = card({ title: 'TLS-Ablauf', accent: 'TLS', sub: 'Restlaufzeit je Zertifikat. Der Strich ist die Warnschwelle (30 Tage vor Ablauf); links davon wird es orange, unter 14 Tagen koralle.' });
  const b = tlsApps.length ? tlsBullets(tlsApps) : el('div.bullets', emptyNote('Keine Zertifikate geprüft.'));
  tls.append(b);
  root.append(tls);
}
function rank(p) { return (p.scope === 'all' ? 2 : 0) + (p.auth === 'none' ? 1 : 0); }
function cpeShort(c) { const parts = String(c).split(':'); return parts.length >= 5 ? `${parts[3]} ${parts[4]}` : c; }
function numWord(n) { return ['Keine', 'Eine', 'Zwei', 'Drei', 'Vier', 'Fünf', 'Sechs', 'Sieben', 'Acht', 'Neun', 'Zehn'][n] ?? String(n); }
