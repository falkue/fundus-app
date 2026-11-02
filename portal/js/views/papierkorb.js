/**
 * Ansicht „Papierkorb“ (#papierkorb): gelöschte Folien, Foliensätze und Projekte (fundus/trash.py) – von Falk im Portal
 * oder von Claude-Sessions (fundus_exhibit_trash). Jeder Eintrag: Vorschau, Herkunft, wer und wann, Tage bis zum
 * endgültigen Löschen (30 Tage). Wiederherstellen legt alles an den alten Ort zurück (samt Decks und gespeicherten
 * Suchen); „Endgültig löschen“ und „Papierkorb leeren“ löschen Folien, Versionen, Feedback und nicht mehr gebrauchte
 * Dateien – nicht rückgängig zu machen.
 * Daten: GET /api/trash; POST /api/trash {action: restore | purge | empty}.
 */
import { el, pageHead, emptyNote, badge, confirmDialog, icon } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';

const arr = (v) => (Array.isArray(v) ? v : []);
const KIND = { exhibit: 'Folie', set: 'Foliensatz', project: 'Projekt' };
const WHO = (w) => (!w ? '' : w === 'portal' ? 'im Portal' : w === 'cli' ? 'im Terminal' : `von ${w.replace(/^mcp:?/, 'Session ')}`);

async function api(body) {
  const r = await fetch('/api/trash', body === undefined ? { cache: 'no-store' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error) throw new Error(d.error ?? `HTTP ${r.status}`);
  return d;
}

export function render(root, ctx) {
  let alive = true;
  const list = el('div.tr-list');
  const info = el('p.small.muted');
  const empty = el('button.btn', { type: 'button' }, icon('ic-trash', 'tr-ic'), 'Papierkorb leeren');
  root.append(pageHead({ title: 'Papierkorb', accent: 'Papierkorb',
    lede: 'Gelöschte Folien, Foliensätze und Projekte – aus dem Portal oder von Claude-Sessions. Sie sind aus Suche, Decks und gespeicherten Suchen genommen und werden nach 30 Tagen endgültig gelöscht. Wiederherstellen legt alles an den alten Ort zurück.',
    tools: [empty] }), info, list);

  async function load() {
    try {
      const d = await api();
      if (!alive) return;
      empty.disabled = !d.entries;
      info.textContent = d.entries ? `${fmt.int(d.entries)} ${d.entries === 1 ? 'Eintrag' : 'Einträge'} mit ${fmt.int(d.slides)} ${d.slides === 1 ? 'Folie' : 'Folien'}` : '';
      list.replaceChildren(...(arr(d.items).length ? arr(d.items).map(row) : [emptyNote('Der Papierkorb ist leer.')]));
    } catch (e) { info.textContent = `Nicht ladbar: ${e.message}`; }
  }

  function row(t) {
    const prev = el('div.tr-prev', arr(t.previews).length
      ? [el('img', { src: t.previews[0], alt: '', loading: 'lazy' }), t.slides > 1 ? el('span.tr-more', `+${fmt.int(t.slides - 1)}`) : null].filter(Boolean)
      : el('span.small.muted', `${fmt.int(t.slides)} Folien`));
    const restore = el('button.btn.btn--primary', { type: 'button' }, 'Wiederherstellen');
    const purge = el('button.btn', { type: 'button' }, 'Endgültig löschen');
    const box = el('div.card.tr-item', prev,
      el('div.tr-main',
        el('div.tr-title', badge(KIND[t.kind] ?? t.kind), el('strong', t.label || t.ref)),
        el('p.small.muted', `${t.kind === 'exhibit' ? 'aus' : 'war'} ${t.ref}${t.kind !== 'exhibit' ? ` · ${fmt.int(t.slides)} Folien` : ''}`),
        el('p.small', `Gelöscht ${fmt.dateTime(t.trashed_at.replace(' ', 'T'))} ${WHO(t.trashed_by)} · `,
          el('span', { class: t.days_left <= 3 ? 'tr-soon' : '' }, t.days_left <= 1 ? 'wird in der nächsten Nacht endgültig gelöscht' : `noch ${t.days_left} Tage`))),
      el('div.row.tr-actions', restore, purge));
    restore.addEventListener('click', async () => {
      try {
        const r = await api({ action: 'restore', id: t.id });
        ctx.toast(`Wiederhergestellt: ${r.to}${arr(r.notes).length ? ` (${r.notes.join('; ')})` : ''}.`, 6000);
        box.remove(); load(); ctx.refreshNav?.();
      } catch (e) { ctx.toast(`Nicht wiederhergestellt: ${e.message}`, 6000); }
    });
    purge.addEventListener('click', async () => {
      if (!await confirmDialog({ title: `${KIND[t.kind]} endgültig löschen?`, text: `„${t.label || t.ref}“ mit ${fmt.int(t.slides)} Folien, allen Versionen und Feedback wird gelöscht. Das lässt sich nicht rückgängig machen.`, confirmLabel: 'Endgültig löschen' })) return;
      try {
        const r = await api({ action: 'purge', ids: [t.id] });
        ctx.toast(`Endgültig gelöscht: ${fmt.int(r.slides)} Folien.`);
        load(); ctx.refreshNav?.();
      } catch (e) { ctx.toast(`Nicht gelöscht: ${e.message}`, 6000); }
    });
    return box;
  }

  empty.addEventListener('click', async () => {
    if (!await confirmDialog({ title: 'Papierkorb leeren?', text: 'Alles im Papierkorb wird endgültig gelöscht – Folien, Versionen, Feedback und Dateien, die keine andere Folie braucht. Das lässt sich nicht rückgängig machen.', confirmLabel: 'Endgültig löschen' })) return;
    try {
      const r = await api({ action: 'empty' });
      ctx.toast(`Papierkorb geleert: ${fmt.int(r.slides)} Folien endgültig gelöscht.`);
      load(); ctx.refreshNav?.();
    } catch (e) { ctx.toast(`Nicht geleert: ${e.message}`, 6000); }
  });

  load();
  return () => { alive = false; };
}
