/**
 * Ansicht „Schutz“: NIST-CSF-2.0-Raster – sechs Spalten GV/ID/PR/DE/RS/RC mit den
 * Prüfungen (checks) als Kacheln mit Zustandsform und Beleg.
 */
import { el, pageHead, card, mark, legend, emptyNote } from '../lib/dom.js';
import { STATES, norm, worst } from '../lib/state.js';

const FN = [
  { code: 'GV', name: 'Govern', de: 'Steuern' }, { code: 'ID', name: 'Identify', de: 'Erkennen' }, { code: 'PR', name: 'Protect', de: 'Schützen' },
  { code: 'DE', name: 'Detect', de: 'Entdecken' }, { code: 'RS', name: 'Respond', de: 'Reagieren' }, { code: 'RC', name: 'Recover', de: 'Wiederherstellen' },
];

export function render(root, data, ctx) {
  const checks = data.checks;
  const crit = checks.filter((c) => norm(c.state) === 'crit').length;
  const warn = checks.filter((c) => norm(c.state) === 'warn').length;
  const unknown = checks.filter((c) => norm(c.state) === 'unknown').length;
  const ok = checks.filter((c) => norm(c.state) === 'ok').length;
  const lede = checks.length ? `${checks.length} Prüfungen: ${ok} erfüllt, ${warn} teilweise, ${crit} verletzt, ${unknown} ohne Daten. Die Prüfungen sind an CIS Controls v8.1 (IG1) angelehnt und werden aus den Messwerten errechnet – niemand schätzt einen Reifegrad.` : 'Keine Prüfungen geliefert.';
  root.append(pageHead({ title: crit ? `${numWord(crit)} ${crit === 1 ? 'Schutzmassnahme ist' : 'Schutzmassnahmen sind'} verletzt` : checks.length ? 'Schutz ohne verletzte Prüfungen' : 'Schutz ohne Daten', accent: crit ? 'verletzt' : checks.length ? 'Prüfungen' : 'Daten', lede }));
  const ins = ctx.insight('schutz'); if (ins) root.append(ins);

  const c = card({ title: 'NIST CSF 2.0 · sechs Funktionen', accent: 'Funktionen', sub: 'Jede Kachel ist eine automatische Prüfung mit Beleg. Links der Zustand als Form und Farbe.' });
  if (!checks.length) c.append(emptyNote('Keine Prüfungen.'));
  const grid = el('div.csf');
  for (const f of FN) {
    const list = checks.filter((x) => x.function === f.code).sort((a, b) => rank(b) - rank(a));
    const col = el('div.csf__col');
    const w = list.length ? worst(list.map((x) => x.state)) : 'unknown';
    col.append(el('div.csf__head', el('span.csf__code', f.code), el('span.csf__name', `${f.name} · ${f.de}`), el('span.csf__summary', mark(w), list.length ? `${list.filter((x) => norm(x.state) === 'ok').length} von ${list.length} erfüllt` : 'keine Prüfung')));
    for (const x of list) {
      const s = norm(x.state);
      col.append(el('div.csf__tile', { class: `csf__tile csf__tile--${s}`, title: STATES[s].word },
        el('div.csf__tile-head', mark(s), el('span', x.label ?? x.id)),
        el('div.csf__tile-ev', x.evidence ?? 'kein Beleg')));
    }
    if (!list.length) col.append(el('div.csf__tile.csf__tile--unknown', el('div.csf__tile-head', mark('unknown'), el('span', 'Keine Prüfung hinterlegt'))));
    grid.append(col);
  }
  c.append(grid);
  c.append(legend([{ mark: 'ok', label: 'erfüllt' }, { mark: 'warn', label: 'teilweise' }, { mark: 'crit', label: 'verletzt' }, { mark: 'unknown', label: 'keine Daten' }]));
  c.append(el('p.small.muted', 'Hinweis: Die Prüfungen sind an CIS Controls v8.1, Implementation Group 1 (56 Safeguards für kleine Umgebungen) angelehnt. Tiers und Profile des CSF werden bewusst nicht verwendet.'));
  root.append(c);
}
function rank(x) { return { crit: 4, warn: 3, unknown: 2, ok: 1, off: 0 }[norm(x.state)]; }
function numWord(n) { return ['Keine', 'Eine', 'Zwei', 'Drei', 'Vier', 'Fünf', 'Sechs', 'Sieben', 'Acht', 'Neun', 'Zehn'][n] ?? String(n); }
