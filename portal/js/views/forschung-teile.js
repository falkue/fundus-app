/**
 * Parts shared by the hypothesis and the finding page: the interpretation editor (seven sections, complete/incomplete,
 * versions), connections (links to hypotheses/findings), theory cards with scholars and works, the change log and the
 * evidence list (quotes with speaker role, photos/videos from the media archive, datasets, documents).
 */
import { el, card, emptyNote, badge } from '../lib/dom.js';
import { arr, kids, put, SECTIONS, interpState, interpChip, md, RELATION, vpill, statusChip, refLink, refHref, EVIDENCE, when, day, hypHref, findHref, linkify } from './forschung-lib.js';

// ---------------------------------------------------------------- interpretation editor
/**
 * interp: current {sections, author, at, version}; history: older versions (newest first, may include current);
 * save(sections) → Promise (rejects with Error). readOnly: e.g. withdrawn items.
 */
export function interpretationEditor({ interp, history, save, readOnly, verdict, ctx }) {
  const root = el('div.fo-ie');
  let current = interp ?? null;
  let viewing = null;          // older version shown read-only
  let mode = 'edit';
  const values = Object.fromEntries(SECTIONS.map((s) => [s.key, current?.sections?.[s.key] ?? '']));
  const status = el('div.fo-ie__status', { 'aria-live': 'polite' });
  const saveB = el('button.btn.btn--primary', { type: 'button', disabled: true }, 'Speichern');
  const resetB = el('button.btn', { type: 'button', disabled: true }, 'Verwerfen');
  const modeSeg = el('div.seg', { role: 'radiogroup', 'aria-label': 'Ansicht' });
  const sectionsBox = el('div.fo-ie__sections');
  const versions = el('div.fo-ie__versions');
  const dirty = () => SECTIONS.some((s) => (values[s.key] ?? '') !== (current?.sections?.[s.key] ?? ''));

  function drawStatus() {
    const st = interpState({ sections: values });
    const steps = el('ol.fo-ie__steps', SECTIONS.filter((s) => s.req).map((s) => el('li', { class: String(values[s.key] ?? '').trim() ? 'is-done' : null, title: s.label },
      el('span.fo-ie__tick', { 'aria-hidden': 'true' }), s.label.split(':')[0])));
    put(status, interpChip(st.state),
      el('span.small', `${st.done} von ${st.need} Pflichtteilen`, st.filled > st.done ? `, ${st.filled - st.done} weitere Teile` : '', ' ausgefüllt'),
      steps,
      current?.at ? el('span.small.muted.fo-ie__by', `Version ${current.version ?? 1} · ${current.author ?? '–'} · ${when(current.at)}`) : el('span.small.muted.fo-ie__by', 'noch nie gespeichert'),
      dirty() ? el('span.fo-ie__dirty', 'ungespeicherte Änderungen') : null);
    saveB.disabled = !dirty() || readOnly;
    resetB.disabled = !dirty();
  }

  function drawModes() {
    put(modeSeg, ...[['edit', 'Bearbeiten'], ['read', 'Lesen']].map(([v, l]) => {
      const b = el('button.seg__btn', { type: 'button', role: 'radio', 'aria-checked': String(mode === v) }, l);
      b.addEventListener('click', () => { mode = v; viewing = null; drawModes(); drawSections(); });
      return b;
    }));
  }

  function drawSections() {
    const src = viewing ? viewing.sections ?? {} : values;
    const editing = mode === 'edit' && !viewing && !readOnly;
    put(sectionsBox, viewing ? el('div.alert.alert--info.fo-ie__old', el('span', `Ältere Version ${viewing.version ?? ''} von ${viewing.author ?? '–'}, ${when(viewing.at)} – nur lesen.`),
      el('button.btn.btn--small', { type: 'button', on: { click: () => { viewing = null; drawSections(); drawVersions(); } } }, 'Zur aktuellen Fassung')) : null,
    SECTIONS.map((s, i) => {
      const val = String(src[s.key] ?? '');
      const filled = !!val.trim();
      const head = el('div.fo-ie__head', el('span.fo-ie__num', String(i + 1)), el('h4', s.label),
        s.req ? el('span.fo-ie__req', { class: `fo-ie__req${filled ? ' is-done' : ''}` }, filled ? 'Pflicht · ausgefüllt' : 'Pflicht · fehlt') : el('span.fo-ie__opt', 'optional'));
      if (!editing) {
        return el('section.fo-ie__sec', { class: `fo-ie__sec${filled ? '' : ' is-empty'}${s.req ? ' is-req' : ''}` }, head,
          filled ? md(val) : el('p.small.muted', s.req ? 'fehlt noch' : 'leer'));
      }
      const ta = el('textarea.input.fo-ie__ta', { rows: 3, 'aria-label': s.label, placeholder: s.hint, id: `fo-ie-${s.key}` });
      ta.value = val;
      const grow = () => { ta.style.height = 'auto'; ta.style.height = `${Math.min(520, ta.scrollHeight + 2)}px`; };
      ta.addEventListener('input', () => { values[s.key] = ta.value; grow(); drawStatus(); sec.classList.toggle('is-empty', !ta.value.trim()); head.querySelector('.fo-ie__req')?.replaceWith(s.req ? el('span.fo-ie__req', { class: `fo-ie__req${ta.value.trim() ? ' is-done' : ''}` }, ta.value.trim() ? 'Pflicht · ausgefüllt' : 'Pflicht · fehlt') : el('span.fo-ie__opt', 'optional')); });
      requestAnimationFrame(grow);
      const sec = el('section.fo-ie__sec', { class: `fo-ie__sec${filled ? '' : ' is-empty'}${s.req ? ' is-req' : ''}` }, head, el('p.fo-ie__hint', s.hint), ta);
      return sec;
    }));
  }

  function drawVersions() {
    const hs = arr(history).filter((h) => h && h.sections);
    if (hs.length < 2) { put(versions); return; }
    put(versions, el('details.fo-ie__vers', el('summary', `${hs.length} Versionen`),
      el('ul', hs.map((h) => el('li', el('button.btn.btn--ghost.btn--small', { type: 'button', on: { click: () => { viewing = (h.version === current?.version) ? null : h; mode = 'read'; drawModes(); drawSections(); } } },
        `Version ${h.version ?? '?'}`), el('span.small.muted', ` ${h.author ?? '–'} · ${when(h.at)} · ${interpState(h).done} von ${interpState(h).need} Pflichtteilen`))))));
  }

  saveB.addEventListener('click', async () => {
    saveB.disabled = true;
    const sections = Object.fromEntries(SECTIONS.map((s) => [s.key, String(values[s.key] ?? '').trim()]));
    try {
      const next = await save(sections);
      current = next ?? { ...current, sections, version: (current?.version ?? 0) + 1, at: new Date().toISOString() };
      for (const s of SECTIONS) values[s.key] = current.sections?.[s.key] ?? '';
      ctx?.toast(interpState(current).complete ? 'Interpretation gespeichert – vollständig.' : 'Interpretation gespeichert – noch unvollständig.');
      drawStatus(); drawSections(); drawVersions();
    } catch (e) { ctx?.toast(`Nicht gespeichert: ${e.message}`, 6000); drawStatus(); }
  });
  resetB.addEventListener('click', () => { for (const s of SECTIONS) values[s.key] = current?.sections?.[s.key] ?? ''; drawStatus(); drawSections(); });

  const lessons = verdict === 'not_supported' || verdict === 'inconclusive'
    ? el('p.small.fo-ie__lesson', verdict === 'inconclusive' ? 'Unentschiedene Ergebnisse sind Ergebnisse: Was wissen wir jetzt, das wir vorher nicht wussten? Wie breit ist das Intervall, welche Stichprobe bräuchte es? War die Messung gültig?'
      : 'Nicht gestützte Ergebnisse sind gleich wichtig wie gestützte: Was widerlegt das Ergebnis, was nicht? War die Messung gültig, reichte die Präzision?') : null;
  if (readOnly) mode = 'read';
  drawModes(); drawStatus(); drawSections(); drawVersions();
  put(root, el('div.fo-ie__bar', status, el('div.row.fo-ie__actions', modeSeg, readOnly ? null : resetB, readOnly ? null : saveB)), lessons, sectionsBox, versions);
  return root;
}

// ---------------------------------------------------------------- connections and theory cards
export function linksCard(links, opts = {}) {
  const ls = arr(links);
  const groups = new Map();
  for (const l of ls) {
    const dir = l.direction === 'in' ? 'in' : 'out';
    const label = RELATION[l.relation]?.[dir] ?? l.relation;
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(l);
  }
  const item = (l) => {
    const isF = (l.type ?? (String(l.ref ?? '').startsWith('finding') ? 'finding' : 'hypothesis')) === 'finding';
    const href = isF ? findHref(l) : hypHref(l);
    return el('li.fo-link', el('a', { href }, el('strong', l.code ?? (isF ? `Finding ${l.id}` : `Hypothese ${l.id}`)), ' ', l.title ?? ''),
      el('span.fo-link__meta', isF ? el('span.fo-explo.fo-explo--small', 'explorativ') : null, l.verdict ? vpill(l.verdict) : l.status ? statusChip(l.status) : null));
  };
  return card({ title: 'Verbindungen', accent: 'Verbindungen', sub: opts.sub ?? 'Schärft, repliziert, widerspricht, erweitert, konkurriert – und woraus etwas entstanden ist.' },
    ls.length ? el('div.fo-links', [...groups].map(([label, items]) => el('div.fo-links__grp', el('span.label', label), el('ul.fo-links__list', items.map(item)))))
      : el('p.small.muted', 'Noch keine Verbindungen.'));
}

export function theoryCards(theories) {
  const ts = arr(theories);
  if (!ts.length) return card({ title: 'Theorie', accent: 'Theorie' }, el('p.small.muted', 'Keine Theorie verknüpft.'));
  return el('div.fo-theories', ts.map((t) => el('section.card.fo-theory',
    el('span.label', 'Theorie'),
    el('h4.fo-theory__name', t.name ?? t.id),
    t.claim ? el('p.fo-theory__claim', t.claim) : null,
    arr(t.scholars).length ? el('ul.fo-theory__sch', arr(t.scholars).map((s) => el('li', el('strong', s.name),
      arr(s.works).length ? el('ul', arr(s.works).map((w) => el('li', w.title, w.doi ? el('span', ' · ', el('a', { href: `https://doi.org/${w.doi}`, target: '_blank', rel: 'noopener' }, `DOI ${w.doi}`)) : null))) : null))) : null)));
}

// ---------------------------------------------------------------- change log
const ACTION = { create: 'erfasst', prereg: 'präregistriert (eingefroren)', start: 'Analyse begonnen', deviate: 'Abweichung erfasst', result: 'Ergebnis erfasst',
  interpret: 'Interpretation gespeichert', link: 'verknüpft', withdraw: 'zurückgezogen', revise: 'überarbeitet', evidence: 'Beleg ergänzt', derive: 'Hypothese abgeleitet',
  update: 'geändert', import: 'importiert' };
export function logList(log) {
  const ls = arr(log).slice().sort((a, b) => String(b.at ?? b.ts ?? '').localeCompare(String(a.at ?? a.ts ?? '')));
  if (!ls.length) return emptyNote('Noch keine Einträge.');
  return el('ol.fo-log', ls.map((e) => el('li',
    el('span.fo-log__ts', when(e.at ?? e.ts)),
    el('span.fo-log__what', el('strong', ACTION[e.action] ?? e.action ?? '–'), e.detail ? el('span', ' – ', ...linkify(e.detail)) : null),
    el('span.fo-log__by', e.by ?? e.actor ?? ''))));
}

// ---------------------------------------------------------------- evidence
export function evidenceList(items, opts = {}) {
  const ev = arr(items);
  if (!ev.length) return emptyNote(opts.empty ?? 'Noch keine Belege.');
  const quotes = ev.filter((e) => e.kind === 'quote');
  const media = ev.filter((e) => e.kind === 'photo' || e.kind === 'video');
  const rest = ev.filter((e) => !['quote', 'photo', 'video'].includes(e.kind));
  return el('div.fo-ev',
    quotes.length ? el('section.fo-ev__grp', el('h4', `Zitate (${quotes.length})`), el('p.small.muted', 'Wörtlich, nur mit Rolle der sprechenden Person – nie mit Namen.'),
      el('div.fo-quotes', quotes.map((q) => el('figure.fo-quote', el('blockquote', `„${String(q.quote ?? q.text ?? '').replace(/^[„"]|[“"]$/g, '')}“`),
        el('figcaption', el('span.fo-quote__who', q.speaker ?? q.role ?? 'Rolle nicht angegeben'), q.note ? el('span.small.muted', ` · ${q.note}`) : null))))) : null,
    media.length ? el('section.fo-ev__grp', el('h4', `Fotos und Videos (${media.length})`), el('div.fo-media', media.map(mediaItem))) : null,
    rest.length ? el('section.fo-ev__grp', opts.plain ? null : el('h4', `Weitere Belege (${rest.length})`), el('ul.fo-evlist', rest.map((e) => el('li',
      el('span.fo-evlist__kind', EVIDENCE[e.kind] ?? e.kind),
      el('span.fo-evlist__ref', e.ref ? refLink(e.ref, `${e.ref}${e.version ? ` v${e.version}` : ''}`) : '–', e.title ? el('span', ` ${e.title}`) : null),
      e.note ? el('span.small.muted.fo-evlist__note', e.note) : null)))) : null);
}

/** Photo or video from the media archive: picture from the evidence item or, if missing, from /api/photos/<id>. */
function mediaItem(e) {
  const id = String(e.ref ?? '').split(':')[1];
  const img = el('img', { alt: e.note ?? e.ref ?? '', loading: 'lazy', decoding: 'async' });
  const link = el('a.fo-media__img', { target: '_blank', rel: 'noopener', title: 'Grösser öffnen' }, img,
    e.kind === 'video' ? el('span.fo-media__play', { 'aria-hidden': 'true' }, '▶') : null);
  const fig = el('figure.fo-media__item', link,
    el('figcaption', el('span.fo-evlist__kind', EVIDENCE[e.kind]), ' ', el('span.fo-ref', e.ref ?? ''), e.note ? el('div.small', e.note) : null));
  const setUrls = (p) => {
    if (p?.thumb_url) img.src = p.thumb_url; else fig.classList.add('is-missing');
    link.href = p?.view_url ?? p?.thumb_url ?? '#';
    if (p?.title && !e.note) img.alt = p.title;
  };
  if (e.thumb_url) setUrls(e);
  else if (id) fetch(`/api/photos/${id}`, { cache: 'no-store' }).then((r) => (r.ok ? r.json() : null)).then(setUrls).catch(() => setUrls(null));
  else setUrls(null);
  return fig;
}

// ---------------------------------------------------------------- small facts grid
export function facts(items) {
  return el('dl.fo-facts', kids(...items).filter(([, v]) => v !== undefined).map(([k, v]) => el('div', el('dt', k), el('dd', v === null || v === '' ? '–' : v))));
}
export { day, refHref, badge };
