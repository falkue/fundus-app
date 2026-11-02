/**
 * Prüfstand als Symbol (ein Zeichen pro Folie, ohne Text auf der Kachel) und Sperren als zweites Zeichen.
 * Farbe UND Form tragen die Bedeutung (auch bei Farbschwäche lesbar); die Erklärung erscheint beim Darüberfahren
 * oder Antippen (data-tip). Symbole: <symbol id="st-…"> in index.html.
 *
 * Zuordnung aus slide_status (fundus/reviews.py):
 *   conf  leuchtend grüner Haken  bestätigt (Konfidenz 3)
 *   ok    grüner Haken            geprüft, eingearbeitet (Konfidenz 2)
 *   old   grüner Ring + Punkt     seit der Prüfung geändert, geprüfte ältere Fassung liegt bereit
 *   open  oranges Dreieck         offene Fehler
 *   dec   rote Raute              wartet auf Falks Entscheidung
 *   un    gestrichelter Kreis     ungeprüft (auch: geändert ohne geprüfte Fassung)
 *   nosrc roter Kreis, gestrichen Zahlen ohne Quelle
 *   na    grauer Punkt            braucht keine Prüfung (Titel, Agenda, Abschnitt)
 */
import { el, svgEl } from '../lib/dom.js';

export const STAGE_KEYS = ['conf', 'ok', 'old', 'open', 'dec', 'un', 'nosrc', 'na'];
export const STAGE_NAME = {
  conf: 'Verwendbar – von einer Person bestätigt', ok: 'Verwendbar – geprüft', old: 'Geändert – geprüfte Fassung liegt bereit',
  open: 'Offene Fehler', dec: 'Wartet auf deine Entscheidung', un: 'Noch ungeprüft', nosrc: 'Zahlen ohne Quelle',
  na: 'Braucht keine Prüfung',
};

const arr = (v) => (Array.isArray(v) ? v : []);
const day = (s) => (s ? new Date(String(s).replace(' ', 'T') + (/[zZ+]/.test(s) ? '' : 'Z')).toLocaleDateString('de-CH') : '');

/** Schlüssel des Symbols für eine Folie (x: Galerie-Eintrag, s: slide_status). */
export function stageKey(x, s) {
  if (!s?.stage) return 'na';                                    // Abschnitt: kein Prüfstand
  const fb = s.feedback ?? {};
  if (fb.decision) return 'dec';
  if (s.stage === 'open') return 'open';
  if (s.stage === 'confirmed') return 'conf';
  if (s.stage === 'checked' || s.stage === 'incorporated') return 'ok';
  if (s.stage === 'not_needed') return 'na';
  if (s.stage === 'no_sources') return 'nosrc';
  if (s.verified && !s.verified.is_current) return 'old';
  return 'un';
}

/** Kurze Erklärung (zwei, drei Zeilen) für das Symbol. */
export function stageTip(key, x, s) {
  const who = arr(s?.reviewers).join(', ');
  const v = s?.version ? `v${s.version}` : '';
  const fb = s?.feedback ?? {};
  // Prüfvermerk des Absenders (erhaltene Folie, unverändert): „geprüft bei <Absender>“
  if ((key === 'conf' || key === 'ok') && s?.foreign && s.label) return `Verwendbar – ${s.label} (Konfidenz ${s.level})\n${v} · gilt, solange die Folie unverändert bleibt`;
  switch (key) {
    case 'conf': return `Verwendbar – geprüft und von einer Person bestätigt (Konfidenz 3)\n${v}${who ? ` · ${who}` : ''}`;
    case 'ok': return `${s?.stage === 'incorporated' ? 'Verwendbar – alle Punkte der Prüfung eingearbeitet' : 'Verwendbar – geprüft, nichts zu ändern'} (Konfidenz 2)\n${v}${who ? ` · ${who}` : ''}`;
    case 'old': return `Seit der Prüfung geändert – die geprüfte Fassung v${s.verified.version} liegt bereit\naktuell ${v}, noch nicht wieder geprüft`;
    case 'open': return `${fb.open || 'Offene'} ${fb.open === 1 ? 'offener Fehler' : 'offene Fehler'} – bitte einarbeiten\n${v}${who ? ` · geprüft von ${who}` : ''}`;
    case 'dec': return `${fb.decision} ${fb.decision === 1 ? 'Punkt wartet' : 'Punkte warten'} auf deine Entscheidung${fb.open ? `\ndazu ${fb.open} offene Fehler` : ''}`;
    case 'nosrc': return 'Zahlen ohne Quelle – Quellen in der Folie ergänzen\nnoch ungeprüft';
    case 'na': return s?.stage === 'not_needed' ? `Braucht keine Prüfung${s.no_sources ? ` (${s.no_sources})` : ''}` : 'Abschnitt – braucht keine Prüfung';
    default: return s?.checked_version != null ? `Seit der Prüfung geändert – noch nicht wieder geprüft\naktuell ${v}` : 'Noch ungeprüft';
  }
}

/** Erklärung der Sperre (Archiv oder laufende Prüfung) oder null. */
export function lockTip(x) {
  if (x.locked) return `Archiviert seit ${day(x.locked.locked_at)} – gesperrt${x.locked.note ? `\n${x.locked.note}` : ''}\nweiterarbeiten nur an einer Kopie`;
  const r = arr(x.in_review);
  if (!r.length) return null;
  const lines = r.map((q) => (q.gemini ? 'Gemini prüft gerade' : `${q.people} prüft${q.until ? ` (Einladung bis ${day(q.until)})` : ''}`));
  return `In Prüfung – gesperrt, bis die Prüfung fertig ist\n${[...new Set(lines)].join('\n')}`;
}

export function symbol(id, cls = 'st-ic') {
  const s = svgEl('svg', { class: cls, viewBox: '0 0 22 22', 'aria-hidden': 'true', focusable: 'false' });
  s.append(svgEl('use', { href: `#${id}` }));
  return s;
}

/** Symbol mit Erklärung: Hover/Fokus zeigt sie, Antippen schaltet sie (Touch). */
export function tipIcon(id, tip, cls = '') {
  const b = el('span.st-tip', { class: `st-tip ${cls}`.trim(), tabindex: '0', role: 'img', 'aria-label': tip.replace(/\n/g, ' · '), data: { tip } },
    symbol(id));
  b.addEventListener('click', (e) => {
    e.stopPropagation(); e.preventDefault();
    const on = !b.classList.contains('is-tip');
    document.querySelectorAll('.st-tip.is-tip').forEach((o) => o.classList.remove('is-tip'));
    b.classList.toggle('is-tip', on);
  });
  return b;
}
document.addEventListener('click', () => document.querySelectorAll('.st-tip.is-tip').forEach((o) => o.classList.remove('is-tip')));

/** Zeichen über dem Vorschaubild einer Kachel: Prüfstand unten rechts, Sperre unten links. */
export function tileSymbols(x, s) {
  const key = stageKey(x, s);
  const lock = lockTip(x);
  return el('span.st-layer',
    lock ? tipIcon(x.locked ? 'st-lock' : 'st-rev', lock, 'st-tip--lock') : null,
    tipIcon(`st-${key}`, stageTip(key, x, s), 'st-tip--stage'));
}
