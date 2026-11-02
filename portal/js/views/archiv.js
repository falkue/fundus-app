/**
 * Ansicht „Archiv“ (#archiv): archivierte Folien aller Projekte (fundus/archive.py). Sie bleiben über Suche, Foliensuche
 * und Decks auffindbar, erscheinen aber nicht mehr in ihrem Projekt und Foliensatz. Hier: suchen, nach Projekt filtern,
 * gross ansehen, markieren und zurückholen (wieder im Projekt gelistet).
 * Daten: GET /api/archive?q=&project=; POST /api/archive {action: 'unarchive', target: 'slide', refs}.
 */
import { el, pageHead, emptyNote, badge } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { openViewer } from './viewer.js';

const arr = (v) => (Array.isArray(v) ? v : []);

export function render(root, ctx) {
  let alive = true;
  const hp = new URLSearchParams(location.hash.split('?')[1] ?? '');
  const st = { q: hp.get('q') ?? '', project: hp.get('p') ?? '', items: [], sel: new Set() };
  const q = el('input.input.ar-q', { type: 'search', placeholder: 'Im Archiv suchen (Titel, Kennung, Projekt)', value: st.q, 'aria-label': 'Im Archiv suchen' });
  const psel = el('select.input', { 'aria-label': 'Projekt' });
  const info = el('p.small.muted');
  const bar = el('div.row.ar-bar');
  const grid = el('div.fg-grid.ar-grid', { role: 'list' });
  root.append(pageHead({ title: 'Archiv', accent: 'Archiv',
    lede: 'Folien, die nicht mehr in ihrem Projekt gelistet sein sollen, aber auffindbar bleiben – für dich, für jede Claude-Session, in Suche, Foliensuche und Decks. Zurückholen listet sie wieder im Projekt.' }),
  el('div.row.ar-tools', q, psel), info, bar, grid);

  let tmr;
  q.addEventListener('input', () => { clearTimeout(tmr); tmr = setTimeout(() => { st.q = q.value.trim(); load(); }, 300); });
  psel.addEventListener('change', () => { st.project = psel.value; load(); });

  async function load() {
    const p = new URLSearchParams();
    if (st.q) p.set('q', st.q);
    if (st.project) p.set('project', st.project);
    history.replaceState(null, '', `#archiv${p.toString() ? `?${p.toString().replace('project=', 'p=')}` : ''}`);
    try {
      const r = await fetch(`/api/archive?${p}`, { cache: 'no-store' }).then((x) => x.json());
      if (!alive) return;
      if (r.error) throw new Error(r.error);
      st.items = arr(r.items);
      st.sel = new Set([...st.sel].filter((id) => st.items.some((x) => x.id === id)));
      psel.replaceChildren(el('option', { value: '' }, 'Alle Projekte'),
        ...arr(r.projects).map((x) => el('option', { value: x.project }, `${x.title || x.project} (${fmt.int(x.n)})`)));
      psel.value = st.project;
      info.textContent = r.total ? `${fmt.int(r.total)} archivierte ${r.total === 1 ? 'Folie' : 'Folien'}${r.total > st.items.length ? ` (die ersten ${st.items.length})` : ''}` : '';
      draw();
    } catch (e) { info.textContent = `Nicht ladbar: ${e.message}`; }
  }

  async function unarchive(ids) {
    try {
      const r = await fetch('/api/archive', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'unarchive', target: 'slide', refs: ids.map(String) }) }).then((x) => x.json());
      if (r.error) throw new Error(r.error);
      ctx.toast(`${fmt.int(r.unarchived)} ${r.unarchived === 1 ? 'Folie' : 'Folien'} zurückgeholt – wieder im Projekt gelistet.`);
      ids.forEach((id) => st.sel.delete(id));
      load(); ctx.refreshNav?.();
    } catch (e) { ctx.toast(`Nicht zurückgeholt: ${e.message}`, 6000); }
  }

  function draw() {
    const n = st.sel.size;
    const back = el('button.btn.btn--primary', { type: 'button', disabled: !n }, n ? `${fmt.int(n)} zurückholen` : 'Zurückholen');
    back.addEventListener('click', () => unarchive([...st.sel]));
    const all = el('button.btn.btn--ghost.btn--small', { type: 'button', disabled: !st.items.length }, 'Alle markieren');
    all.addEventListener('click', () => { st.items.forEach((x) => st.sel.add(x.id)); draw(); });
    const none = n ? el('button.btn.btn--ghost.btn--small', { type: 'button' }, 'Keine') : null;
    none?.addEventListener('click', () => { st.sel.clear(); draw(); });
    bar.replaceChildren(...[all, none, el('span.fg-tools__grow'), back].filter(Boolean));
    if (!st.items.length) { grid.replaceChildren(emptyNote(st.q || st.project ? 'Nichts im Archiv für diese Auswahl.' : 'Das Archiv ist leer.')); return; }
    const view = st.items.map((x) => ({ png: x.png, preview: x.preview, title: x.title || x.slug, sub: `${x.project_title || x.project} · ${x.set_title || x.set}`, href: null }));
    grid.replaceChildren(...st.items.map((x, i) => {
      const sel = st.sel.has(x.id);
      const t = el('button.fg-tile', { type: 'button', 'aria-label': x.title || x.slug },
        el('span.fg-tile__img', x.preview ? el('img', { src: x.preview, alt: '', loading: 'lazy' }) : null),
        el('span.fg-tile__title', x.title || x.slug),
        el('span.fg-tags', badge(x.project_title || x.project), el('span', `archiviert ${fmt.date(x.archived_at.replace(' ', 'T'))}`)));
      t.addEventListener('click', () => openViewer(view, i));
      const box = el('input.fg-check__box', { type: 'checkbox', checked: sel, 'aria-label': 'Folie markieren' });
      box.addEventListener('change', () => { if (box.checked) st.sel.add(x.id); else st.sel.delete(x.id); draw(); });
      return el('div.fg-tile-wrap', { role: 'listitem', class: sel ? 'fg-tile-wrap fg-tile-wrap--sel' : 'fg-tile-wrap' },
        t, el('label.fg-check', { title: 'Markieren' }, box));
    }));
  }

  load();
  return () => { alive = false; };
}
