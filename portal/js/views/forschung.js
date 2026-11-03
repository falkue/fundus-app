/**
 * Ansicht „Forschung“ (#forschung): Hypothesen (streng präregistriert) und Findings (explorativ) mit Ergebnissen,
 * Interpretationen und Belegen (fundus/research.py). Reiter Übersicht · Hypothesen · Findings; Einzelseiten
 * #forschung?h=<id> (forschung-hypothese.js) und #forschung?f=<id> (forschung-finding.js).
 * Hash: #forschung?tab=uebersicht|hypothesen|findings, Filter ebenfalls im Hash.
 * Daten: GET /api/research (Übersicht: Serien, Arbeitsliste, Evidenzkarte), /api/research/hypotheses, /api/research/findings.
 */
import { el, pageHead, segmented } from '../lib/dom.js';
import { hp, api, put, normBoard } from './forschung-lib.js';
import * as uebersicht from './forschung-uebersicht.js';
import * as liste from './forschung-liste.js';
import * as hypothese from './forschung-hypothese.js';
import * as finding from './forschung-finding.js';

const TABS = [
  { value: 'uebersicht', label: 'Übersicht', title: 'Was wir geprüft und gefunden haben', accent: 'geprüft',
    lede: 'Hypothesen werden vor jeder Analyse präregistriert und eingefroren; jedes Urteil zählt gleich – auch nicht gestützt und unentschieden. Findings sind explorativ: ohne vorherige Hypothese, nie als bestätigt ausgewiesen.' },
  { value: 'hypothesen', label: 'Hypothesen', title: 'Hypothesen und ihre Urteile', accent: 'Urteile',
    lede: 'Alle Hypothesen mit Präregistrierung, Ergebnis und Stand der Interpretation. Filter und Suche bleiben im Link erhalten.' },
  { value: 'findings', label: 'Findings', title: 'Findings aus der Exploration', accent: 'Exploration',
    lede: 'Befunde ohne vorherige Hypothese – aus Interviews, Beobachtung, Dokumenten, Fotos und explorativer Datenanalyse. Aus einem Finding lässt sich eine Hypothese ableiten, die dann präregistriert geprüft wird.' }];

let boardCache = null;   // {at, promise}
export function loadBoard(force = false) {
  if (!force && boardCache && Date.now() - boardCache.at < 30000) return boardCache.promise;
  const promise = api('').then(normBoard);
  boardCache = { at: Date.now(), promise };
  promise.catch(() => { if (boardCache?.promise === promise) boardCache = null; });
  return promise;
}

export function render(root, ctx) {
  let alive = true;
  const isAlive = () => alive;
  const p = hp();
  if (p.get('h')) { hypothese.render(root, ctx, p.get('h'), isAlive); return () => { alive = false; }; }
  if (p.get('f')) { finding.render(root, ctx, p.get('f'), isAlive); return () => { alive = false; }; }
  const tab = TABS.find((t) => t.value === p.get('tab')) ?? TABS[0];
  const tabs = segmented(TABS.map(({ value, label }) => ({ value, label })), tab.value,
    (v) => { location.hash = v === 'uebersicht' ? '#forschung' : `#forschung?tab=${v}`; }, 'Bereich');
  tabs.el.classList.add('dk-tabs');
  root.append(pageHead({ title: tab.title, accent: tab.accent, lede: tab.lede }), tabs.el);
  const body = el('div.fo-body');
  root.append(body);
  put(body, el('div.dk-skel', Array.from({ length: 3 }, () => el('div.dk-skel__card'))));
  loadBoard(tab.value === 'uebersicht').then((board) => {
    if (!alive) return;
    put(body);
    if (tab.value === 'uebersicht') uebersicht.render(body, ctx, board, isAlive);
    else liste.render(body, ctx, tab.value === 'findings' ? 'findings' : 'hypotheses', isAlive, board);
  }).catch((e) => {
    if (!alive) return;
    if (tab.value !== 'uebersicht') { put(body); liste.render(body, ctx, tab.value === 'findings' ? 'findings' : 'hypotheses', isAlive, null); return; }
    put(body, el('div.alert.alert--error', e.status === 404 ? 'Der Fundus-Server kennt den Forschungsteil noch nicht (nach dem nächsten Neustart des Servers verfügbar).' : `Nicht ladbar: ${e.message}`));
  });
  return () => { alive = false; };
}
