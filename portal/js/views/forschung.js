/**
 * Ansicht „Forschung“ (#forschung): Hypothesen (vor der Auswertung festgelegt) und Findings (explorativ) mit Ergebnis,
 * Interpretation und Belegen (fundus/research.py). Hauptseite ist eine Liste (forschung-liste.js); Einzelseiten
 * #forschung?h=<id> (forschung-hypothese.js) und #forschung?f=<id> (forschung-finding.js); Nebenseite
 * #forschung?view=uebersicht (forschung-uebersicht.js).
 */
import { el } from '../lib/dom.js';
import { hp } from './forschung-lib.js';
import * as liste from './forschung-liste.js';
import * as hypothese from './forschung-hypothese.js';
import * as finding from './forschung-finding.js';
import * as uebersicht from './forschung-uebersicht.js';

export function render(root, ctx) {
  let alive = true;
  const isAlive = () => alive;
  const p = hp();
  const page = el('div.fo');
  root.append(page);
  if (p.get('h')) hypothese.render(page, ctx, p.get('h'), isAlive);
  else if (p.get('f')) finding.render(page, ctx, p.get('f'), isAlive);
  else if (p.get('view') === 'uebersicht') uebersicht.render(page, ctx, isAlive);
  else liste.render(page, ctx, isAlive);
  return () => { alive = false; };
}
