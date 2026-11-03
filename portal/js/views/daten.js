/**
 * Ansicht „Datensätze“ (#daten): Datenkatalog und Archiv der grossen Tabellen in Fundus (fundus/datasets.py).
 * Drei Reiter – Katalog (suchen, Facetten, Karten oder Tabelle), Abdeckung (Thema × Region, Zeiträume), Archiv
 * (Prüfsummen, Lebenszyklus, Referenzen, Speicher) – und der Steckbrief eines Datensatzes (#daten?d=<slug>).
 * Hash: #daten?tab=katalog|abdeckung|archiv, Filter und Suche ebenfalls im Hash (Links funktionieren).
 * Daten: GET /api/datasets/catalog, /api/datasets/<id>; Einzelheiten in daten-katalog.js, daten-abdeckung.js,
 * daten-archiv.js, daten-detail.js, gemeinsame Bausteine in daten-lib.js.
 */
import { el, pageHead, segmented } from '../lib/dom.js';
import { hp, uploadDialog } from './daten-lib.js';
import * as katalog from './daten-katalog.js';
import * as abdeckung from './daten-abdeckung.js';
import * as archiv from './daten-archiv.js';
import * as detail from './daten-detail.js';

const TABS = [
  { value: 'katalog', label: 'Katalog', view: katalog, title: 'Datensätze im Katalog', accent: 'Katalog',
    lede: 'Grosse Tabellen für Analysen – gespeichert, beschrieben, zitierbar und auffindbar. Das lokale Modell beschreibt jeden Datensatz und seine Spalten; deine Angaben haben Vorrang. Claude-Sessions finden sie mit fundus_dataset_search.' },
  { value: 'abdeckung', label: 'Abdeckung', view: abdeckung, title: 'Was die Datensätze abdecken', accent: 'abdecken',
    lede: 'Wo der Bestand dicht ist und wo Lücken sind: Themen je Region und die Zeiträume der Datensätze. Ein Klick führt in den gefilterten Katalog oder zum Datensatz.' },
  { value: 'archiv', label: 'Archiv', view: archiv, title: 'Das Archiv der Datensätze', accent: 'Archiv',
    lede: 'Bestandserhaltung: Prüfsummen der gespeicherten Dateien, ersetzte und zurückgezogene Datensätze, Referenzen auf Rohdaten ausserhalb von Fundus und der Speicherbedarf.' }];

export function render(root, ctx) {
  let alive = true;
  const isAlive = () => alive;
  const ref = hp().get('d');
  if (ref) {
    detail.render(root, ctx, ref, isAlive);
    return () => { alive = false; };
  }
  const tab = TABS.find((t) => t.value === hp().get('tab')) ?? TABS[0];
  const up = el('button.btn.btn--primary', { type: 'button' }, 'Hochladen …');
  up.addEventListener('click', () => uploadDialog(ctx, null, (r) => { location.hash = `#daten?d=${r.slug}`; }));
  const tabs = segmented(TABS.map(({ value, label }) => ({ value, label })), tab.value,
    (v) => { location.hash = v === 'katalog' ? '#daten' : `#daten?tab=${v}`; }, 'Bereich');
  tabs.el.classList.add('dk-tabs');
  root.append(pageHead({ title: tab.title, accent: tab.accent, lede: tab.lede, tools: [up] }), tabs.el);
  const body = el('div.dk-body');
  root.append(body);
  tab.view.render(body, ctx, isAlive);
  return () => { alive = false; };
}
