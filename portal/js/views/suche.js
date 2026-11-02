/**
 * Bereich „Foliensuche“ des Fundus-Portals: eine Frage über alle Projekte und Foliensätze, Treffer nach Aspekten der
 * Frage gegliedert (lokales Modell), Filter nach Erstellungsjahr, Konfidenz und Projekt. Eine Auswahl lässt sich unter
 * einem Namen speichern; gespeicherte Suchen stehen in der Seitenleiste und lassen sich bearbeiten und neu suchen.
 * Sessions nutzen sie über fundus_slides(['search:<slug>']).
 * Hash: #suche (Suche), #suche?q=…&years=…&levels=…&project=…&favorites=1 (Suche mit Frage; nur Favoriten auch ohne Frage =
 * alle Favoriten), #suche?c=<id> (gespeicherte Suche).
 * Daten: GET /api/slide-search, GET/POST /api/collections[/<id>].
 */
import { el, pageHead, card, emptyNote, badge, confirmDialog, headline, fullscreenToggle, dialogKeys } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { api, SLIDE_STAGE } from './review-ui.js';
import { favButton } from './fav.js';

const arr = (v) => (Array.isArray(v) ? v : []);
const LEVELS = [['', 'alle Konfidenzen'], ['2,3', 'geprüft oder bestätigt (≥ 2)'], ['1,2,3', 'mindestens 1'], ['3', 'nur bestätigt (3)'],
  ['0', 'nur ungeprüft (0)'], ['0,1', 'zweifelhaft (≤ 1)']];

function params() { return new URLSearchParams(location.hash.split('?')[1] ?? ''); }

/** Prüfstand einer Folie: Balken, Stufe, ggf. fixierte geprüfte Fassung. */
function stage(s) {
  if (!s.stage) return null;
  const [label, tone] = SLIDE_STAGE[s.stage] ?? [s.stage, 'outline'];
  const lv = s.confidence ?? 0;
  const v = s.verified_version;
  return el('span.rv-stage', { title: `Prüfstand: ${label}, Konfidenz ${lv} von 3` },
    el('span.rv-meter', { 'aria-hidden': 'true' }, [0, 1, 2].map((i) => el('span', { class: i < lv ? `rv-meter__on rv-meter__on--${lv}` : null }))),
    badge(label, tone),
    v && !v.is_current ? el('span.rv-verified', { title: v.note ?? '' }, `geprüft: v${v.version}`) : null);
}

/** Kachel: Vorschau, Titel, Projekt · Jahr, Prüfstand; Häkchen = in die Auswahl. */
function tile(s, { picked, onPick, onOpen, ctx }) {
  const t = el('button.fg-tile', { type: 'button', 'aria-label': s.title ?? s.ref });
  t.append(el('span.fg-tile__img', s.preview_url ? el('img', { src: s.preview_url, alt: '', loading: 'lazy', decoding: 'async', width: 640, height: 360 })
    : el('span.small.muted', 'keine Vorschau')));
  t.append(el('span.fg-tile__title', { title: s.title ?? s.ref }, s.title || s.ref));
  t.append(el('span.fg-tags', stage(s), el('span.fg-tag', `${s.project}${s.set_title ? ` · ${s.set_title}` : ''}`), s.year ? el('span.fg-tag', String(s.year)) : null,
    s.visibility === 'internal' ? badge('nur im Projekt', 'warning') : null));
  t.addEventListener('click', () => onOpen(s));
  const box = el('input.fg-check__box', { type: 'checkbox', checked: picked, 'aria-label': `${s.title ?? s.ref} in die Auswahl` });
  const wrap = el('div.fg-tile-wrap', { role: 'listitem', class: picked ? 'fg-tile-wrap fg-tile-wrap--sel' : 'fg-tile-wrap' },
    t, favButton(s, ctx), el('label.fg-check', { title: 'Markieren' }, box));
  box.addEventListener('click', () => { wrap.classList.toggle('fg-tile-wrap--sel', box.checked); onPick(box.checked); });
  return wrap;
}

/** Grossansicht der Treffer: blättern (Knöpfe, Pfeiltasten, Wischen), Vollbild, Link ins Projekt. */
function preview(list, start, ctx) {
  document.querySelector('dialog.sx-lightbox')?.remove();
  let i = Math.max(0, start);
  const dlg = el('dialog.fg-lightbox.sx-lightbox', { 'aria-label': 'Folie gross' });
  const img = el('img.fg-lb__img', { alt: '' });
  const side = el('div.fg-lb__side');
  const prev = el('button.btn.fg-lb__nav', { type: 'button', 'aria-label': 'Vorherige Folie' }, '←');
  const next = el('button.btn.fg-lb__nav', { type: 'button', 'aria-label': 'Nächste Folie' }, '→');
  const close = el('button.btn.fg-lb__close', { type: 'button', 'aria-label': 'Schliessen' }, '✕');
  const pos = el('span.small.muted');
  const frame = el('div.fg-lb__frame', img);
  const stageBox = el('div.fg-lb__stage', frame);
  const fs = fullscreenToggle(dlg, 'fg-lightbox--full');
  fs.corner(frame, img);
  dlg.append(el('div.fg-lb', el('div.fg-lb__bar', prev, pos, next, el('span.fg-tools__grow'), fs, close),
    el('div.fg-lb__main', stageBox, side)));
  const show = () => {
    const s = list[i];
    pos.textContent = `${i + 1} / ${list.length}${s.aspect ? ` · ${s.aspect}` : ''}`;
    prev.disabled = i === 0; next.disabled = i === list.length - 1;
    img.src = s.png_url ?? s.preview_url ?? ''; img.alt = s.title ?? s.ref;
    const v = s.verified_version;
    const open = el('a.btn', { href: `#folien?p=${encodeURIComponent(s.project)}` }, 'Im Projekt öffnen');
    open.addEventListener('click', () => dlg.close());
    side.replaceChildren(...[
      stage(s) ? el('div.fg-lb__stage-info', stage(s), el('p.small.muted.fg-lb__why', `Konfidenz ${s.confidence ?? 0} von 3`)) : null,
      el('h3.fg-lb__title', s.title || s.ref),
      el('div.row.fv-lb', favButton(s, ctx, null, 'btn')),
      s.so_what ? el('p.secondary.fg-lb__sowhat', s.so_what) : null,
      el('p.small', `${s.project}${s.set_title ? ` · ${s.set_title}` : ''} · erstellt ${s.year ?? '–'} · v${s.version}`),
      v && !v.is_current ? el('p.small', 'Geprüfte Fassung: ', el('a', { href: v.urls?.png ?? '#', target: '_blank', rel: 'noopener' }, `v${v.version} öffnen`)) : null,
      el('div.row.fg-lb__links', open, s.png_url ? el('a.btn', { href: s.png_url, target: '_blank', rel: 'noopener' }, 'PNG öffnen') : null)].filter(Boolean));
  };
  const go = (d) => { const j = i + d; if (j >= 0 && j < list.length) { i = j; show(); } };
  prev.addEventListener('click', () => go(-1));
  next.addEventListener('click', () => go(1));
  close.addEventListener('click', () => dlg.close());
  dialogKeys(dlg, (e) => {   // am Dokument: auch wenn Safari den Fokus aus dem Dialog nimmt
    if (e.key === 'ArrowLeft') { e.preventDefault(); go(-1); }
    if (e.key === 'ArrowRight') { e.preventDefault(); go(1); }
  });
  let x0 = null;   // Wischen auf dem iPad
  stageBox.addEventListener('touchstart', (e) => { x0 = e.touches[0].clientX; }, { passive: true });
  stageBox.addEventListener('touchend', (e) => {
    if (x0 === null) return;
    const dx = e.changedTouches[0].clientX - x0; x0 = null;
    if (Math.abs(dx) > 50) go(dx < 0 ? 1 : -1);
  });
  dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
  dlg.addEventListener('close', () => { if (document.fullscreenElement) document.exitFullscreen?.(); dlg.remove(); });
  document.body.append(dlg);
  show();
  dlg.showModal?.();
}

/** Namen erfragen (Circle-Dialog statt window.prompt). */
function askName(title, value = '') {
  return new Promise((resolve) => {
    const dlg = el('dialog.dialog');
    const input = el('input.input', { type: 'text', value, placeholder: 'z. B. KI im Autohandel – Händlerökonomie', 'aria-label': 'Name' });
    const ok = el('button.btn.btn--primary', { type: 'button' }, 'Speichern');
    const no = el('button.btn', { type: 'button' }, 'Abbrechen');
    dlg.append(el('div.dialog__body', headline('h3', title), input, el('div.row.dialog__actions', no, ok)));
    const done = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    ok.addEventListener('click', () => done(input.value.trim() || null));
    no.addEventListener('click', () => done(null));
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') done(input.value.trim() || null); });
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); done(null); });
    document.body.append(dlg);
    dlg.showModal?.();
    input.focus();
  });
}

/** Treffer nach Aspekten: je Aspekt Name, Satz und Kachelraster. */
function groups(aspects, slides, pick, onPick, ctx) {
  const byId = new Map(slides.map((s) => [s.id, s]));
  const gs = arr(aspects).length ? aspects : [{ name: slides.length && slides.every((s) => s.favorite) ? 'Favoriten' : 'Treffer', slides: slides.map((s) => s.id) }];
  // Blätter-Reihenfolge der Grossansicht = Anzeige-Reihenfolge über alle Aspekte
  const flat = gs.flatMap((a) => arr(a.slides).map((i) => byId.get(i)).filter(Boolean).map((s) => ({ ...s, aspect: a.name })));
  const onOpen = (s) => preview(flat, flat.findIndex((x) => x.id === s.id && x.aspect === s.aspect), ctx);
  return gs.map((a) => {
    const list = arr(a.slides).map((i) => byId.get(i)).filter(Boolean).map((s) => ({ ...s, aspect: a.name }));
    if (!list.length) return null;
    return el('section.sx-aspect',
      el('div.sx-aspect__head', el('h3.sx-aspect__name', a.name), el('span.small.muted', fmt.plural(list.length, 'Folie', 'Folien'))),
      a.summary ? el('p.small.secondary.sx-aspect__sum', a.summary) : null,
      el('div.fg-grid', { role: 'list' }, list.map((s) => tile(s, { picked: pick.has(s.id), onPick: (v) => onPick(s.id, v), onOpen, ctx }))));
  });
}

export function render(root, ctx) {
  let alive = true;
  const p = params();
  const cid = p.get('c');
  const head = pageHead({ title: cid ? 'Gespeicherte Suche' : 'Foliensuche', accent: cid ? 'Suche' : 'Foliensuche',
    lede: cid ? 'Lade …' : 'Eine Frage über alle Projekte und Foliensätze. Die Treffer kommen nach Aspekten der Frage gegliedert; markiere mit dem Häkchen, was in die gespeicherte Auswahl kommt.' });
  const body = el('div.sx-page', { 'aria-live': 'polite' });
  root.append(head, body);
  const fail = (e) => { if (alive) body.replaceChildren(el('div.alert.alert--error', el('strong', 'Nicht ladbar. '), e.message)); };

  // ------------------------------------------------------------ Suche
  async function searchView() {
    let meta = { years: [] }, projects = [];
    try { [meta, projects] = await Promise.all([api('/api/collections'), api('/api/exhibits').then((d) => arr(d.projects))]); } catch { /* Filter ohne Listen */ }
    if (!alive) return;
    const q = el('input.input.sx-q', { type: 'search', value: p.get('q') ?? '', placeholder: 'Frage, z. B. Wie verändert KI die Arbeit im Spital?', 'aria-label': 'Frage' });
    const year = el('select.select', { 'aria-label': 'Erstellungsjahr' }, el('option', { value: '' }, 'alle Jahre'),
      arr(meta.years).map((y) => el('option', { value: String(y.year), selected: p.get('years') === String(y.year) }, `${y.year} (${fmt.int(y.slides)})`)));
    const lv = el('select.select', { 'aria-label': 'Konfidenz' }, LEVELS.map(([v, t]) => el('option', { value: v, selected: (p.get('levels') ?? '') === v }, t)));
    const proj = el('select.select', { 'aria-label': 'Projekt' }, el('option', { value: '' }, 'alle Projekte'),
      projects.map((x) => el('option', { value: x.project, selected: p.get('project') === x.project }, x.title || x.project)));
    const favBox = el('input', { type: 'checkbox', checked: p.get('favorites') === '1', 'aria-label': 'Nur Favoriten' });
    const fav = el('label.fv-only', { title: 'Nur Folien mit Stern – ohne Frage: alle Favoriten' }, favBox, ' Nur Favoriten');
    const go = el('button.btn.btn--primary', { type: 'submit' }, 'Suchen');
    const form = el('form.sx-form', { role: 'search' }, q, el('div.sx-filters', el('span.label', 'Erstellt'), year, el('span.label', 'Konfidenz'), lv,
      el('span.label', 'Projekt'), proj, fav), go);
    const out = el('div.sx-results');
    body.replaceChildren(card({ title: 'Frage stellen', accent: 'Frage' }, form), out);
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const n = new URLSearchParams();
      if (q.value.trim()) n.set('q', q.value.trim());
      if (year.value) n.set('years', year.value);
      if (lv.value) n.set('levels', lv.value);
      if (proj.value) n.set('project', proj.value);
      if (favBox.checked) n.set('favorites', '1');
      history.replaceState(null, '', `#suche?${n}`);
      run(n);
    });
    let runId = 0;
    if (p.get('q') || p.get('favorites') === '1') run(p);
    else q.focus();

    // Zwei Schritte: erst die Treffer (ein paar Sekunden), dann die Gliederung nach Aspekten durch das lokale Modell
    // (bis ~30 s, beim zweiten Mal aus dem Zwischenspeicher); Markierungen bleiben beim Umsortieren erhalten.
    async function run(n) {
      const me = ++runId;
      out.replaceChildren(el('p.muted', 'Suche …'));
      go.disabled = true;
      const pick = new Set();      // nichts vormarkiert: du wählst, was in die Auswahl kommt
      const quick = new URLSearchParams(n);
      quick.set('aspects', '0');
      let r;
      try { r = await api(`/api/slide-search?${quick}`); } catch (e) { out.replaceChildren(el('div.alert.alert--error', e.message)); go.disabled = false; return; }
      go.disabled = false;
      if (!alive || me !== runId) return;
      if (!arr(r.slides).length) { out.replaceChildren(emptyNote(n.get('favorites') === '1' ? 'Keine Favoriten mit diesen Filtern. Den Stern setzt du an einer Folie.' : 'Keine Folien zu dieser Frage mit diesen Filtern.')); return; }
      if (!n.get('q')) { draw(r, pick, false); return; }   // alle Favoriten ohne Frage: keine Aspekte
      draw(r, pick, true);
      try {
        const full = await api(`/api/slide-search?${n}`);
        if (alive && me === runId) draw(full, pick, false);
      } catch (e) {
        if (alive && me === runId) out.querySelector('.sx-pending')?.replaceWith(el('div.alert', `Ohne Aspekte: ${e.message}`));
      }
    }

    function draw(r, pick, pending) {
      const slides = arr(r.slides);
      const unrelated = new Set(arr(r.unrelated));
      const count = el('span.small.muted');
      const save = el('button.btn.btn--primary', { type: 'button' }, 'Auswahl speichern …');
      const all = el('button.btn', { type: 'button' }, 'Alle markieren');
      all.addEventListener('click', () => {
        const on = pick.size < slides.length;
        slides.forEach((s) => (on ? pick.add(s.id) : pick.delete(s.id)));
        out.querySelectorAll('.fg-check__box').forEach((b) => { b.checked = on; b.closest('.fg-tile-wrap')?.classList.toggle('fg-tile-wrap--sel', on); });
        upd();
      });
      const upd = () => {
        count.textContent = pick.size ? `${fmt.int(pick.size)} von ${fmt.int(slides.length)} Folien markiert` : `${fmt.int(slides.length)} Folien – markiere, was du speichern willst`;
        save.disabled = !pick.size; all.textContent = pick.size < slides.length ? 'Alle markieren' : 'Markierung aufheben';
      };
      const onPick = (id, v) => { if (v) pick.add(id); else pick.delete(id); upd(); };
      const asp = arr(r.aspects).filter((a) => a.name !== 'Treffer' || r.aspects.length > 1);
      const extra = unrelated.size ? [{ name: 'Passt wohl nicht zur Frage', summary: 'Vom Modell aussortiert – per Häkchen trotzdem aufnehmen.', slides: [...unrelated] }] : [];
      save.addEventListener('click', async () => {
        const name = await askName('Suche speichern');
        if (!name) return;
        const keep = (a) => ({ name: a.name, summary: a.summary, slides: arr(a.slides).filter((i) => pick.has(i)) });
        try {
          const c = await api('/api/collections', { name, question: r.question, filters: r.filters ?? null,
            aspects: [...asp, ...extra].map(keep).filter((a) => a.slides.length), slides: slides.filter((s) => pick.has(s.id)).map((s) => s.id) });
          ctx.toast(`Gespeichert als „${c.name}“ – Sessions nutzen sie mit ${c.ref}.`, 5000);
          await ctx.refreshNav?.();
          location.hash = `#suche?c=${c.id}`;
        } catch (e) { ctx.toast(`Nicht gespeichert: ${e.message}`, 5000); }
      });
      upd();
      out.replaceChildren(...[
        el('div.sx-bar', count, all, save),
        pending ? el('p.small.muted.sx-pending', 'Gliedere nach Aspekten der Frage … (das lokale Modell braucht bis zu einer halben Minute)') : null,
        !pending && r.aspects_note ? el('div.alert', 'Ohne Aspekte: das lokale Modell war nicht verfügbar (', r.aspects_note, ').') : null,
        ...groups([...asp, ...extra], slides, pick, onPick, ctx)].filter(Boolean));
    }
  }

  // ------------------------------------------------------------ gespeicherte Suche
  async function collectionView() {
    let c;
    try { c = await api(`/api/collections/${cid}`); } catch (e) { fail(e); return; }
    if (!alive) return;
    head.querySelector('h1').replaceWith(headline('h1', c.name, c.name.split(' ')[0]));
    const f = c.filters ?? {};
    head.querySelector('.lede').textContent = [c.question ? `Frage: „${c.question}“` : null,
      f.years ? `erstellt ${f.years.join(', ')}` : null, f.levels ? `Konfidenz ${f.levels.join(', ')}` : null,
      `${fmt.plural(c.count, 'Folie', 'Folien')} aus ${fmt.plural(arr(c.projects).length, 'Projekt', 'Projekten')}`].filter(Boolean).join(' · ');
    const pick = new Set();      // markiert = zum Entfernen
    const removeBtn = el('button.btn', { type: 'button' }, 'Markierte entfernen');
    const onPick = (id, v) => { if (v) pick.add(id); else pick.delete(id); removeBtn.disabled = !pick.size; };
    removeBtn.disabled = true;
    removeBtn.addEventListener('click', async () => {
      const gone = [...pick];
      try { await api(`/api/collections/${c.id}`, { action: 'update', remove: gone }); ctx.toast(`${fmt.plural(gone.length, 'Folie', 'Folien')} entfernt.`); await ctx.refreshNav?.(); collectionView(); }
      catch (e) { ctx.toast(`Nicht gespeichert: ${e.message}`, 5000); }
    });
    const rename = el('button.btn', { type: 'button' }, 'Umbenennen');
    rename.addEventListener('click', async () => {
      const name = await askName('Neuer Name', c.name);
      if (!name || name === c.name) return;
      try { await api(`/api/collections/${c.id}`, { action: 'update', name }); await ctx.refreshNav?.(); collectionView(); }
      catch (e) { ctx.toast(`Nicht umbenannt: ${e.message}`, 5000); }
    });
    const del = el('button.btn.btn--danger', { type: 'button' }, 'Löschen');
    del.addEventListener('click', async () => {
      if (!await confirmDialog({ title: `„${c.name}“ löschen?`, text: 'Nur die gespeicherte Auswahl wird gelöscht, die Folien bleiben.', confirmLabel: 'Löschen' })) return;
      try { await api(`/api/collections/${c.id}`, { action: 'delete' }); await ctx.refreshNav?.(); location.hash = '#suche'; }
      catch (e) { ctx.toast(`Nicht gelöscht: ${e.message}`, 5000); }
    });
    const again = el('button.btn', { type: 'button', disabled: !c.question }, 'Neu suchen');
    const fresh = el('div.sx-fresh');
    again.addEventListener('click', async () => {
      fresh.replaceChildren(el('p.muted', 'Suche mit derselben Frage und denselben Filtern im heutigen Bestand …'));
      let r;
      try { r = await api(`/api/collections/${c.id}`, { action: 'rerun' }); } catch (e) { fresh.replaceChildren(el('div.alert.alert--error', e.message)); return; }
      if (!r.new.length) { fresh.replaceChildren(card({ title: 'Neu suchen', accent: 'Neu' }, emptyNote('Keine neuen Folien – die Auswahl ist aktuell.'))); return; }
      const add = new Set();
      const addBtn = el('button.btn.btn--primary', { type: 'button', disabled: true }, 'Markierte hinzufügen');
      const byAsp = {};
      r.new.forEach((s) => { (byAsp[s.aspect ?? 'Weitere'] ??= []).push(s.id); });
      fresh.replaceChildren(card({ title: `${fmt.plural(r.new.length, 'neue Folie', 'neue Folien')}`, accent: 'neue',
        sub: 'Markiere, was in die Auswahl soll.' }, el('div.sx-bar', addBtn),
      ...groups(Object.entries(byAsp).map(([name, slides]) => ({ name, slides })), r.new, add, (id, v) => { if (v) add.add(id); else add.delete(id); addBtn.disabled = !add.size; }, ctx)));
      addBtn.addEventListener('click', async () => {
        try {
          for (const [name, ids] of Object.entries(byAsp)) {
            const mine = ids.filter((i) => add.has(i));
            if (mine.length) await api(`/api/collections/${c.id}`, { action: 'update', add: mine, aspect: name === 'Weitere' ? null : name });
          }
          ctx.toast(`${fmt.plural(add.size, 'Folie', 'Folien')} hinzugefügt.`); await ctx.refreshNav?.(); collectionView();
        } catch (e) { ctx.toast(`Nicht gespeichert: ${e.message}`, 5000); }
      });
    });
    body.replaceChildren(...[
      card({ title: 'Für Sessions', accent: 'Sessions', sub: `Sag einer Session: „Nimm die gespeicherte Suche ${c.name}“. Sie holt die Folien mit fundus_slides(['${c.ref}']) – auch zusammen mit Foliensätzen (set:<projekt>/<satz>) und Decks (deck:<projekt>/<deck>). Zur Prüfung: fundus_review(action='gemini', refs=['${c.ref}']).` },
        el('div.sx-bar', el('code.sx-ref', c.ref), again, rename, removeBtn, del)),
      c.hidden ? el('div.alert', c.hidden) : null,
      fresh,
      ...groups(c.aspects, arr(c.slides), pick, onPick, ctx)].filter(Boolean));
    ctx.setStatus('ok', 'verbunden');
  }

  (cid ? collectionView() : searchView()).catch(fail);
  return () => { alive = false; };
}
