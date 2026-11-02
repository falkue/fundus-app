/**
 * KI-Einschätzung je Ansicht (aus interpretations.json, erzeugt von interpret.py auf Hero
 * mit lokalem Modell). Karte im Stil von Falks AiInsightCard: violetter Hauch, Tag,
 * Einschätzung, Handlungsfelder mit Prioritäts-Pill, Fusszeile mit Modell und Stand.
 * Fehlt die Datei oder die Ansicht → Karte „noch keine Einschätzung“ (oder gar nichts).
 */
import { el, badge, headline } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';

const PRIO = { hoch: ['Hoch', 'crit'], mittel: ['Mittel', 'warn'], niedrig: ['Niedrig', 'unknown'] };
const CATEGORY_VIEW = { exposure: '#exposition', patch: '#patches', eol: '#patches', backup: '#sicherung', service: '#dienste', capacity: '#verlauf', hardware: '#landschaft', ai: '#ki' };

export function insightCard(viewId, interp, data, { quiet = false } = {}) {
  const v = interp?.views?.[viewId] ?? null;
  const c = el('section.ai-card', { 'aria-label': 'KI-Einschätzung' });
  c.append(el('div.ai-card__tag', `Einschätzung · ${v?.model ?? interp?.model ?? 'lokales Modell'}`));
  if (!interp || !v) {
    if (quiet) return null;
    c.append(el('p.ai-card__text', 'Noch keine Einschätzung.'));
    c.append(el('p', interp?.skipped ? `interpret.py hat übersprungen: ${interp.skipped}` : 'interpretations.json liegt nicht vor oder enthält diese Ansicht nicht. Die Diagramme sind davon unabhängig.'));
    return c;
  }
  c.append(el('p.ai-card__text', v.assessment ?? 'Keine Einschätzung geliefert.'));
  const actions = Array.isArray(v.actions) ? v.actions : [];
  if (actions.length) {
    c.append(el('div.ai-card__label', 'Handlungsfelder'));
    const ul = el('ul.ai-card__actions');
    // Handlungsfeld gilt als erledigt, wenn ALLE verwiesenen Befunde nicht mehr offen sind
    const open = new Set((data?.findings ?? []).map((x) => x.id));
    const isDone = (a) => { const r = (a.refs ?? []).filter(Boolean); return r.length > 0 && r.every((id) => !open.has(id)); };
    const sorted = [...actions].sort((x, y) => isDone(x) - isDone(y));
    for (const a of sorted) {
      const done = isDone(a);
      const [word, tone] = done ? ['Erledigt', 'ok'] : (PRIO[a.priority] ?? ['–', 'unknown']);
      const li = el(done ? 'li.ai-card__action.is-done' : 'li.ai-card__action');
      li.append(el('div.ai-card__action-head', badge(word, tone), el('strong', a.title ?? '–')));
      if (a.why) li.append(el('p.ai-card__why', a.why));
      const refs = (a.refs ?? []).filter(Boolean);
      if (refs.length) {
        const row = el('div.ai-card__refs');
        for (const id of refs) {
          const f = data?.findings?.find((x) => x.id === id);
          const solved = (data?.findingsResolved ?? []).find((x) => x.id === id);
          row.append(f
            ? el('a.ai-card__ref', { href: CATEGORY_VIEW[f.category] ?? '#lage', title: f.title }, f.title)
            : el('span.ai-card__ref.is-resolved', { title: 'Befund nicht mehr offen' }, `${solved?.title ?? id} · gelöst`));
        }
        li.append(row);
      }
      ul.append(li);
    }
    c.append(ul);
  }
  const stale = isStale(interp, data);
  const foot = el('div.ai-card__foot',
    el('span', `${v.model ?? interp.model ?? 'Modell unbekannt'} · Stand ${v.at ? fmt.ago(v.at) : interp.generated ? fmt.ago(interp.generated) : 'unbekannt'}`),
    el('span', 'KI-Einschätzung, kann irren – massgeblich sind die Befunde.'));
  const outdated = actions.some((a) => (a.refs ?? []).some((id) => id && !(data?.findings ?? []).some((x) => x.id === id)));
  if (stale || outdated) foot.append(el('span.ai-card__stale', outdated
    ? 'Teile dieser Einschätzung sind überholt – sie wird bei der nächsten Änderung neu erstellt.'
    : `Grundlage älter als der Datenstand (${fmt.dateTime(interp.basis)}).`));
  c.append(foot);
  return c;
}

function isStale(interp, data) {
  const basis = fmt.parseDate(interp?.basis), gen = fmt.parseDate(data?.generated);
  return !!(basis && gen && gen - basis > 3 * 3.6e6);
}
export { headline };
