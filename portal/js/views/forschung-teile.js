/**
 * Parts shared by the hypothesis and the finding page (both are plain documents): the interpretation section
 * (reading view, empty state and the seven-part editor), "Verwandt" (links, theory), the evidence list and the
 * change log for "Technische Details".
 */
import { el } from '../lib/dom.js';
import { arr, kids, put, SECTIONS, interpState, md, RELATION, resultPill, refLink, EVIDENCE, when, hypHref, findHref, isFinding, linkify,
  STATUS } from './forschung-lib.js';

// ---------------------------------------------------------------- interpretation
/**
 * The section body: reading view (filled parts with their headings), or an empty state with "Interpretation schreiben".
 * save(sections) → Promise<new interpretation | null>. readOnly hides the buttons (withdrawn items).
 */
export function interpretationSection({ interp, save, readOnly, ctx, verdict }) {
  const box = el('div.fo-interp');
  let current = interp ?? null;
  const show = () => {
    const st = interpState(current);
    if (!st.filled) {
      put(box, el('div.fo-empty', el('p', 'Noch keine Interpretation.'),
        el('p.fo-muted', 'Sie erklärt, was das Ergebnis bedeutet, warum es so ausfiel und wo seine Grenzen sind.'),
        readOnly ? null : el('button.btn.btn--primary', { type: 'button', on: { click: edit } }, 'Interpretation schreiben')));
      return;
    }
    put(box, SECTIONS.filter((s) => String(current.sections?.[s.key] ?? '').trim()).map((s) => el('div.fo-interp__part', el('h3', s.label), md(current.sections[s.key]))),
      el('p.fo-muted.fo-interp__by', kids(st.complete ? null : `Unvollständig: ${st.need - st.done} von ${st.need} Pflichtteilen fehlen.`,
        current.author ? `Von ${current.author}` : null, current.at ? when(current.at) : null).join(' · ')),
      readOnly ? null : el('button.btn', { type: 'button', on: { click: edit } }, 'Bearbeiten'));
  };
  const edit = () => put(box, interpretationEditor({ interp: current, ctx, verdict,
    onCancel: show,
    save: async (sections) => { const next = await save(sections); current = next ?? { ...(current ?? {}), sections, at: new Date().toISOString() }; show(); return current; } }));
  show();
  return box;
}

/** The seven-part editor: a label, a short hint and a text field per part; four parts are needed. */
export function interpretationEditor({ interp, save, onCancel, ctx, verdict }) {
  const values = Object.fromEntries(SECTIONS.map((s) => [s.key, interp?.sections?.[s.key] ?? '']));
  const status = el('span.fo-muted', { 'aria-live': 'polite' });
  const saveB = el('button.btn.btn--primary', { type: 'button' }, 'Speichern');
  const cancelB = el('button.btn', { type: 'button', on: { click: () => onCancel?.() } }, 'Abbrechen');
  const drawStatus = () => {
    const st = interpState({ sections: values });
    status.textContent = st.complete ? 'Alle Pflichtteile ausgefüllt.' : `${st.done} von ${st.need} Pflichtteilen ausgefüllt.`;
  };
  const parts = SECTIONS.map((s) => {
    const ta = el('textarea.input.fo-ed__ta', { rows: 3, id: `fo-ie-${s.key}`, 'aria-label': s.label });
    ta.value = values[s.key];
    const grow = () => { ta.style.height = 'auto'; ta.style.height = `${Math.min(480, ta.scrollHeight + 2)}px`; };
    ta.addEventListener('input', () => { values[s.key] = ta.value; grow(); drawStatus(); });
    requestAnimationFrame(grow);
    return el('div.fo-ed__part', el('label', { for: `fo-ie-${s.key}` }, s.label, s.req ? el('span.fo-muted', ' · Pflicht') : null), el('p.fo-muted.fo-ed__hint', s.hint), ta);
  });
  saveB.addEventListener('click', async () => {
    saveB.disabled = true;
    try {
      await save(Object.fromEntries(SECTIONS.map((s) => [s.key, String(values[s.key] ?? '').trim()])));
      ctx?.toast('Interpretation gespeichert.');
    } catch (e) { saveB.disabled = false; ctx?.toast(`Nicht gespeichert: ${e.message}`, 6000); }
  });
  drawStatus();
  const lesson = verdict === 'not_supported' || verdict === 'inconclusive'
    ? el('p.fo-muted', 'Auch ein nicht gestütztes oder unentschiedenes Ergebnis ist ein Ergebnis: Was wissen wir jetzt, was wir vorher nicht wussten?') : null;
  return el('div.fo-ed', lesson, parts, el('div.fo-ed__bar', saveB, cancelB, status));
}

// ---------------------------------------------------------------- Verwandt
/** Links to other hypotheses and findings, plus the theory the item is anchored in. */
export function relatedList({ links, theories, extra }) {
  const items = arr(links).map((l) => {
    const label = RELATION[l.relation]?.[l.direction === 'in' ? 'in' : 'out'] ?? l.relation_label ?? l.relation;
    const f = isFinding(l);
    return el('li', el('span.fo-muted', `${label} `), el('a', { href: f ? findHref(l) : hypHref(l) }, kids(l.code, l.title).join(' – ')),
      f ? el('span.fo-muted', ' · Finding') : l.verdict ? el('span.fo-muted', ` · ${resultPill(l.verdict).textContent}`) : l.status ? el('span.fo-muted', ` · ${STATUS[l.status] ?? l.status}`) : null);
  });
  const th = arr(theories).map((t) => el('li', el('span.fo-muted', 'Theorie '), t.name ?? t.id,
    arr(t.scholars).length ? el('span.fo-muted', ` · ${arr(t.scholars).map((s) => s.name ?? s).join('; ')}`) : null));
  const all = [...arr(extra), ...items, ...th];
  return all.length ? el('ul.fo-list', all) : el('p.fo-muted', 'Keine Verbindungen.');
}

// ---------------------------------------------------------------- evidence (findings)
export function evidenceList(items) {
  const ev = arr(items);
  if (!ev.length) return el('p.fo-muted', 'Noch keine Belege.');
  const quotes = ev.filter((e) => e.kind === 'quote');
  const media = ev.filter((e) => e.kind === 'photo' || e.kind === 'video');
  const rest = ev.filter((e) => !['quote', 'photo', 'video'].includes(e.kind));
  return el('div.fo-ev',
    quotes.map((q) => el('figure.fo-quote', el('blockquote', `„${String(q.quote ?? q.text ?? '').replace(/^[„"]|[“"]$/g, '')}“`),
      el('figcaption.fo-muted', kids(q.speaker ?? 'Rolle nicht angegeben', q.note).join(' · ')))),
    media.length ? el('div.fo-media', media.map(mediaItem)) : null,
    rest.length ? el('ul.fo-list', rest.map((e) => el('li', el('span.fo-muted', `${EVIDENCE[e.kind] ?? e.kind} `),
      e.kind === 'file' ? el('span', fileName(e.ref)) : refLink(e.ref, kids(e.ref, e.title).join(' – ')),
      e.version ? el('span.fo-muted', ` · Version ${e.version}`) : null,
      e.note && e.kind !== 'file' ? el('span.fo-muted', ` · ${e.note}`) : null))) : null);
}
const fileName = (p) => String(p ?? '').split('/').pop();

/** Photo or video from the media archive: picture from the item or, if missing, from /api/photos/<id>. */
function mediaItem(e) {
  const id = String(e.ref ?? '').split(':')[1];
  const img = el('img', { alt: e.note ?? e.ref ?? '', loading: 'lazy', decoding: 'async' });
  const link = el('a.fo-media__img', { target: '_blank', rel: 'noopener', title: 'Grösser öffnen' }, img);
  const fig = el('figure.fo-media__item', link, el('figcaption.fo-muted', kids(EVIDENCE[e.kind], e.note).join(' · ')));
  const setUrls = (p) => {
    if (p?.thumb_url) img.src = p.thumb_url; else fig.classList.add('is-missing');
    link.href = p?.view_url ?? p?.thumb_url ?? '#';
  };
  if (e.thumb_url) setUrls(e);
  else if (id) fetch(`/api/photos/${id}`, { cache: 'no-store' }).then((r) => (r.ok ? r.json() : null)).then(setUrls).catch(() => setUrls(null));
  else setUrls(null);
  return fig;
}

// ---------------------------------------------------------------- change log (Technische Details)
const ACTION = { create: 'erfasst', prereg: 'Plan festgelegt', start: 'Auswertung begonnen', deviate: 'Abweichung festgehalten', result: 'Ergebnis abgelegt',
  interpret: 'Interpretation gespeichert', link: 'verknüpft', withdraw: 'zurückgezogen', revise: 'überarbeitet', evidence: 'Beleg ergänzt', derive: 'Hypothese abgeleitet',
  update: 'geändert', import: 'importiert' };
export function logList(log) {
  const ls = arr(log).slice().sort((a, b) => String(b.at ?? '').localeCompare(String(a.at ?? '')));
  if (!ls.length) return el('p.fo-muted', 'Keine Einträge.');
  return el('ul.fo-list.fo-log', ls.slice(0, 60).map((e) => el('li', el('span.fo-muted', `${when(e.at)} · `), ACTION[e.action] ?? e.action ?? '–',
    e.by ? el('span.fo-muted', ` · ${e.by}`) : null)));
}

/** Calm definition list; rows without a value are left out. */
export function defs(rows) {
  const r = rows.filter(([, v]) => v !== null && v !== undefined && v !== '' && !(Array.isArray(v) && !v.length));
  return el('dl.fo-dl', r.map(([k, v]) => el('div', el('dt', k), el('dd', typeof v === 'string' ? el('span', ...linkify(v)) : v))));
}
