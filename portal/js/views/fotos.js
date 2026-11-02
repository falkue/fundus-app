/**
 * Ansicht „Mediathek“ (#mediathek, früher #fotos): Medienarchiv der Feldforschung – Fotos und Videos – für Folien und Grafiken (fundus/photos.py,
 * Videos: fundus/videos.py). Videos: Kachel mit ▶ und Dauer, in der Grossansicht abspielbar, Ton (ohne/Umgebung/Sprache).
 *
 * Links Suche und Filter (Erhebung/Album, Prüfstand, Freigabe, Personen, Format, Schlagworte je Dimension mit Anzahl),
 * rechts das Raster. Kacheln markieren → gemeinsam verschlagworten, bestätigen, in ein Album legen, Rechte setzen.
 * Grossansicht: Beschreibung und Interpretation des lokalen Modells (gemma4), Schlagworte als Chips – Vorschläge des
 * Modells gestrichelt mit ✓/✕, eigene durchgezogen –, Rechte (Einwilligung, Verwendung, Gesichter), Gruppen, Verwendung
 * in Folien, ähnliche Fotos. Prüfliste: neu beschriebene Fotos nacheinander, Enter = bestätigen und weiter.
 * Hochladen: Dateien, Ordner-Inhalte, Foliensätze/PDFs (deren Bilder) → POST /api/photos/upload.
 * Daten: GET /api/photos, /api/photos/meta, /api/photos/<id>; POST /api/photos/<id>, /api/photos/bulk, /api/photo-groups.
 */
import { el, clear, pageHead, emptyNote, dl, segmented, badge, mark, icon, fullscreenToggle, dialogKeys, confirmDialog } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';

const arr = (v) => (Array.isArray(v) ? v : []);
const kids = (...a) => a.filter((x) => x !== null && x !== undefined && x !== false);
const STATUS = { new: 'wartet auf Beschreibung', annotated: 'beschrieben, noch nicht geprüft', confirmed: 'geprüft', failed: 'Beschreibung fehlgeschlagen' };
const CLEARED = { external: ['ok', 'freigegeben für Kundenfolien und Veröffentlichungen'], teaching: ['ok', 'freigegeben für die Lehre'],
  internal: ['warn', 'nur intern verwendbar'], none: ['crit', 'gesperrt (keine Einwilligung)'] };
const CONSENT = [{ value: 'unknown', label: 'unbekannt' }, { value: 'yes', label: 'liegt vor' }, { value: 'no', label: 'keine' }];
const USAGE = [{ value: 'internal', label: 'intern' }, { value: 'teaching', label: 'Lehre' }, { value: 'publication', label: 'Veröffentlichung' }];
const FACES = [{ value: 'unknown', label: 'unbekannt' }, { value: 'no', label: 'keine' }, { value: 'yes', label: 'erkennbar' }];
const AUDIO = [{ value: 'unknown', label: 'unbekannt' }, { value: 'none', label: 'kein Ton' }, { value: 'ambient', label: 'Umgebung' }, { value: 'speech', label: 'Sprache' }];
const dur = (s) => { const t = Math.round(s || 0); return t >= 3600 ? `${Math.floor(t / 3600)}:${String(Math.floor(t % 3600 / 60)).padStart(2, '0')}:${String(t % 60).padStart(2, '0')}` : `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`; };
const noun = (x) => (x?.media === 'video' ? 'Video' : 'Foto');
const DIM_ORDER = ['setting', 'activity', 'role', 'artefact', 'phase', 'method', 'mood', 'industry', 'region', 'keyword'];
const SIZE_KEY = 'fundus-photo-tile';

async function api(path, body) {
  const r = await fetch(`/api${path}`, body === undefined ? { cache: 'no-store' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error ?? `HTTP ${r.status}`);
  return d;
}
function hashParams() { return new URLSearchParams((location.hash.split('?')[1]) ?? ''); }

export function render(root, ctx) {
  let alive = true;
  const hp = hashParams();
  const st = {
    q: hp.get('q') ?? '', group: hp.get('g') ?? '', status: hp.get('s') ?? '', purpose: 'all', people: '', orientation: '', media: hp.get('m') ?? '',
    dims: {}, sort: 'taken', photos: [], total: 0, facets: {}, meta: null, sel: new Set(), last: null,
  };
  try { st.dims = JSON.parse(hp.get('d') || '{}'); } catch { st.dims = {}; }
  const setHash = () => {
    const p = new URLSearchParams();
    if (st.q) p.set('q', st.q);
    if (st.group) p.set('g', st.group);
    if (st.status) p.set('s', st.status);
    if (st.media) p.set('m', st.media);
    if (Object.keys(st.dims).length) p.set('d', JSON.stringify(st.dims));
    const h = `#mediathek${p.toString() ? `?${p}` : ''}`;
    if (location.hash !== h) history.replaceState(null, '', h);
  };

  const info = el('p.small.muted.ph-info');
  const worker = el('p.small.ph-worker');
  const grid = el('div.fg-grid.ph-grid', { role: 'list' });
  const selbar = el('div.ph-selbar', { hidden: true });
  const filters = el('aside.ph-filters', { 'aria-label': 'Filter' });
  const reviewBtn = el('button.btn', { type: 'button' }, 'Prüfliste');
  const upBtn = el('button.btn.btn--primary', { type: 'button' }, 'Hochladen …');
  const size = el('input.ph-size', { type: 'range', min: '120', max: '420', step: '10', 'aria-label': 'Grösse der Kacheln' });
  const tilePx = (() => { try { return Number(localStorage.getItem(SIZE_KEY)) || 210; } catch { return 210; } })();
  size.value = String(tilePx);
  const applySize = () => { grid.style.setProperty('--fg-tile', `${size.value}px`); try { localStorage.setItem(SIZE_KEY, size.value); } catch { /* privat */ } };
  size.addEventListener('input', applySize); applySize();

  root.append(pageHead({ title: 'Mediathek', accent: 'Mediathek', lede: 'Ausgewählte Fotos und Videos aus der Feldforschung für Folien, Grafiken und Präsentationen. Das lokale Modell auf der Spark beschreibt und verschlagwortet jedes Foto und Video; deine eigenen Schlagworte und Entscheidungen haben Vorrang.',
    tools: [reviewBtn, upBtn] }));
  root.append(el('div.ph-layout', filters, el('div.ph-main', el('div.ph-top', info, el('span.fg-tools__grow'), el('label.small.muted.ph-size-l', 'Grösse ', size)), worker, selbar, grid)));

  reviewBtn.addEventListener('click', () => { st.status = 'annotated'; st.sort = 'imported'; load().then(() => { if (st.photos.length) openLightbox(0, true); else ctx.toast('Keine neu beschriebenen Fotos zu prüfen.'); }); });
  upBtn.addEventListener('click', () => uploadDialog());

  // ---------------------------------------------------------------- Daten
  let seq = 0;
  async function load() {
    const my = ++seq;
    setHash();
    const p = new URLSearchParams({ purpose: st.purpose, limit: '500', sort: st.sort });
    if (st.q) p.set('q', st.q);
    if (st.group) p.set('group', st.group);
    if (st.status) p.set('status', st.status);
    if (st.people) p.set('people', st.people);
    if (st.orientation) p.set('orientation', st.orientation);
    if (st.media) p.set('media', st.media);
    if (Object.keys(st.dims).length) p.set('dims', JSON.stringify(st.dims));
    info.textContent = 'Lade Fotos …';
    try {
      const [r, meta] = await Promise.all([api(`/photos?${p}`), api('/photos/meta')]);
      if (my !== seq || !alive) return;
      st.photos = arr(r.photos); st.total = r.total; st.facets = r.facets ?? {}; st.meta = meta;
      for (const id of [...st.sel]) if (!st.photos.some((x) => x.id === id)) st.sel.delete(id);
      draw();
    } catch (e) { if (my === seq) info.textContent = `Fotos nicht ladbar: ${e.message}`; }
  }

  function draw() {
    const m = st.meta ?? {};
    const s = m.stats ?? {};
    info.textContent = `${fmt.int(st.total)} von ${fmt.int(s.n ?? 0)} Einträgen${s.videos ? ` (davon ${fmt.int(s.videos)} Videos)` : ''}${st.total > st.photos.length ? ` (die ersten ${st.photos.length} gezeigt)` : ''}`;
    const w = m.worker ?? {};
    worker.replaceChildren(...kids(
      w.pending ? el('span', mark(w.waiting_since ? 'warn' : 'ok'), w.waiting_since
        ? ` ${fmt.int(w.pending)} Fotos/Videos warten auf Beschreibung – das Modell ist gerade belegt (seit ${w.waiting_since}).`
        : ` Das lokale Modell beschreibt gerade ${fmt.int(w.pending)} neue Fotos/Videos (etwa 10–20 s pro Stück).`) : null,
      s.annotated ? el('span', ` ${fmt.int(s.annotated)} beschrieben, noch nicht geprüft.`) : null,
      s.failed ? el('span', mark('crit'), ` ${fmt.int(s.failed)} fehlgeschlagen.`) : null));
    reviewBtn.textContent = s.annotated ? `Prüfliste (${fmt.int(s.annotated)})` : 'Prüfliste';
    drawFilters();
    drawSelbar();
    clear(grid);
    if (!st.photos.length) {
      grid.append(emptyNote(s.n ? 'Nichts für diese Auswahl.' : 'Noch keine Fotos oder Videos. Mit „Hochladen …“ oder in einer Claude-Session (fundus_photo_import) hinzufügen.'));
      return;
    }
    st.photos.forEach((x, i) => grid.append(tile(x, i)));
  }

  function tile(x, i) {
    const sel = st.sel.has(x.id);
    const t = el('button.fg-tile.ph-tile', { type: 'button', 'aria-label': x.title || x.caption || `${noun(x)} ${x.id}` });
    const fig = el('span.fg-tile__img.ph-tile__img', el('img', { src: x.thumb_url, alt: '', loading: 'lazy', decoding: 'async' }));
    if (x.media === 'video') fig.append(el('span.ph-play', { title: `Video, ${dur(x.duration)}${x.audio === 'none' ? ', ohne Ton' : ''}` }, `▶ ${dur(x.duration)}`));
    if (x.status !== 'confirmed') fig.append(el('span.ph-dot', { class: `ph-dot ph-dot--${x.status}`, title: STATUS[x.status] }));
    if (x.faces === 'yes') fig.append(el('span.ph-face', { title: x.consent === 'yes' ? 'erkennbare Personen, Einwilligung liegt vor' : 'erkennbare Personen' }, x.consent === 'yes' ? '☺✓' : '☺'));
    t.append(fig, el('span.fg-tile__title.ph-tile__cap', x.title || x.caption || (x.status === 'new' ? 'wird beschrieben …' : '–')));
    t.addEventListener('click', (e) => { if (e.shiftKey || e.metaKey || e.ctrlKey) { toggleSel(x.id, i, e.shiftKey); return; } openLightbox(i, false); });
    const box = el('input.fg-check__box', { type: 'checkbox', checked: sel, 'aria-label': 'Foto markieren' });
    box.addEventListener('click', (e) => toggleSel(x.id, i, e.shiftKey, box.checked));
    return el('div.fg-tile-wrap', { role: 'listitem', class: sel ? 'fg-tile-wrap fg-tile-wrap--sel' : 'fg-tile-wrap' },
      t, el('label.fg-check', { title: 'Markieren (Umschalt: Bereich)' }, box));
  }
  function toggleSel(id, i, range, on = !st.sel.has(id)) {
    if (range && st.last !== null) {
      const [a, b] = [Math.min(st.last, i), Math.max(st.last, i)];
      for (let k = a; k <= b; k++) { if (on) st.sel.add(st.photos[k].id); else st.sel.delete(st.photos[k].id); }
    } else if (on) st.sel.add(id); else st.sel.delete(id);
    st.last = i;
    draw();
  }

  // ---------------------------------------------------------------- Filter
  function drawFilters() {
    const m = st.meta ?? {};
    const q = el('input.input.ph-q', { type: 'search', placeholder: 'Suchen, z. B. „Workaround Pflege“', value: st.q, 'aria-label': 'Fotos suchen' });
    let tmr;
    q.addEventListener('input', () => { clearTimeout(tmr); tmr = setTimeout(() => { st.q = q.value.trim(); load(); }, 350); });
    const groups = arr(m.groups);
    const gsel = el('select.input', { 'aria-label': 'Erhebung oder Album' },
      el('option', { value: '' }, 'Alle Erhebungen und Alben'),
      ['survey', 'album'].map((k) => {
        const gs = groups.filter((g) => g.kind === k);
        return gs.length ? el('optgroup', { label: k === 'survey' ? 'Erhebungen' : 'Alben' }, gs.map((g) => el('option', { value: String(g.id) }, `${g.title} (${g.n})`))) : null;
      }));
    gsel.value = st.group;
    gsel.addEventListener('change', () => { st.group = gsel.value; load(); });
    const stat = segmented([{ value: '', label: 'Alle' }, { value: 'unconfirmed', label: 'Ungeprüft' }, { value: 'confirmed', label: 'Geprüft' }],
      st.status === 'annotated' ? 'unconfirmed' : st.status, (v) => { st.status = v; st.sort = 'taken'; load(); }, 'Prüfstand');
    const purpose = el('select.input', { 'aria-label': 'Freigabe' },
      [['all', 'Alle Freigaben'], ['external', 'freigegeben: Kundenfolien'], ['teaching', 'freigegeben: Lehre'], ['internal', 'mindestens intern']].map(([v, l]) => el('option', { value: v }, l)));
    purpose.value = st.purpose;
    purpose.addEventListener('change', () => { st.purpose = purpose.value; load(); });
    const people = segmented([{ value: '', label: 'Alle' }, { value: 'none', label: 'ohne Personen' }, { value: 'some', label: 'mit Personen' }], st.people, (v) => { st.people = v; load(); }, 'Personen');
    const ori = segmented([{ value: '', label: 'Alle' }, { value: 'landscape', label: 'quer' }, { value: 'portrait', label: 'hoch' }], st.orientation, (v) => { st.orientation = v; load(); }, 'Format');
    const media = segmented([{ value: '', label: 'Alle' }, { value: 'photo', label: 'Fotos' }, { value: 'video', label: 'Videos' }], st.media, (v) => { st.media = v; load(); }, 'Medium');
    const active = Object.entries(st.dims).flatMap(([d, vs]) => vs.map((v) => [d, v]));
    const dims = m.dims ?? {};
    const facetBlocks = DIM_ORDER.filter((d) => st.facets[d] || st.dims[d]).map((d) => {
      const counts = st.facets[d] ?? {};
      const vals = Object.keys(counts);
      for (const v of st.dims[d] ?? []) if (!vals.includes(v)) vals.push(v);
      const lab = (v) => arr(dims[d]?.values).find((o) => o.value === v)?.label ?? v.replace(/-/g, ' ');
      const box = el('details.ph-facet', { open: d !== 'keyword' || (st.dims[d] ?? []).length > 0 },
        el('summary', dims[d]?.label ?? d),
        el('div.ph-chips', vals.slice(0, d === 'keyword' ? 40 : 30).map((v) => {
          const on = (st.dims[d] ?? []).includes(v);
          const b = el('button.ph-chip', { type: 'button', 'aria-pressed': String(on), class: `ph-chip${on ? ' ph-chip--on' : ''}` }, lab(v), el('span.ph-chip__n', String(counts[v] ?? 0)));
          b.addEventListener('click', () => {
            const cur = new Set(st.dims[d] ?? []);
            if (cur.has(v)) cur.delete(v); else cur.add(v);
            if (cur.size) st.dims[d] = [...cur]; else delete st.dims[d];
            load();
          });
          return b;
        })));
      return box;
    });
    const reset = active.length ? el('button.btn.btn--small', { type: 'button' }, `Schlagwort-Filter löschen (${active.length})`) : null;
    reset?.addEventListener('click', () => { st.dims = {}; load(); });
    filters.replaceChildren(q, gsel, el('span.label', 'Medium'), media.el, el('span.label', 'Prüfstand'), stat.el, el('span.label', 'Freigabe'), purpose,
      el('span.label', 'Personen'), people.el, el('span.label', 'Format'), ori.el, ...kids(reset), ...facetBlocks);
    if (document.activeElement?.classList?.contains('ph-q')) q.focus();
  }

  // ---------------------------------------------------------------- Mehrfachauswahl
  function drawSelbar() {
    const n = st.sel.size;
    selbar.hidden = !n;
    if (!n) { selbar.replaceChildren(); return; }
    const ids = [...st.sel];
    const run = async (body, msg) => {
      try { await api('/photos/bulk', { ids, ...body }); ctx.toast(msg); await load(); } catch (e) { ctx.toast(`Nicht gespeichert: ${e.message}`, 5000); }
    };
    const addTag = tagInput(async (d, v) => run({ action: 'tag', tags: { add: [{ dim: d, value: v }] } }, `Schlagwort für ${n} Fotos gesetzt.`));
    const confirmB = el('button.btn', { type: 'button' }, 'Als geprüft markieren');
    confirmB.addEventListener('click', () => run({ action: 'confirm' }, `${n} Fotos geprüft.`));
    const album = el('select.input', { 'aria-label': 'In Album oder Erhebung legen' }, el('option', { value: '' }, 'In Gruppe legen …'),
      arr(st.meta?.groups).map((g) => el('option', { value: String(g.id) }, `${g.kind === 'survey' ? 'Erhebung' : 'Album'}: ${g.title}`)),
      el('option', { value: 'new' }, '+ neues Album …'));
    album.addEventListener('change', async () => {
      if (album.value === 'new') {
        const title = prompt('Name des neuen Albums');
        if (title?.trim()) await run({ action: 'group_add', group: { kind: 'album', title: title.trim() } }, `Album „${title.trim()}“ angelegt.`);
      } else if (album.value) await run({ action: 'group_add', group: Number(album.value) }, `${n} Fotos zugeordnet.`);
      album.value = '';
    });
    const rights = el('select.input', { 'aria-label': 'Rechte setzen' }, el('option', { value: '' }, 'Rechte setzen …'),
      el('optgroup', { label: 'Einwilligung' }, CONSENT.map((o) => el('option', { value: `consent:${o.value}` }, `Einwilligung: ${o.label}`))),
      el('optgroup', { label: 'Verwendung' }, USAGE.map((o) => el('option', { value: `usage:${o.value}` }, `Verwendung: ${o.label}`))));
    rights.addEventListener('change', async () => {
      const [k, v] = rights.value.split(':');
      if (k) await run({ action: 'update', changes: { [k]: v } }, `Rechte für ${n} Fotos gesetzt.`);
      rights.value = '';
    });
    const again = el('button.btn', { type: 'button' }, 'Neu beschreiben lassen');
    again.addEventListener('click', () => run({ action: 'reannotate' }, `${n} Fotos in die Warteschlange gelegt.`));
    const hide = el('button.btn', { type: 'button' }, 'Ausblenden');
    hide.addEventListener('click', async () => {
      if (await confirmDialog({ title: `${n} Fotos ausblenden?`, text: 'Sie verschwinden aus Suche und Raster; die Dateien bleiben erhalten.', confirmLabel: 'Ausblenden' }))
        run({ action: 'remove' }, `${n} Fotos ausgeblendet.`);
    });
    const none = el('button.btn.btn--small', { type: 'button' }, 'Auswahl aufheben');
    none.addEventListener('click', () => { st.sel.clear(); draw(); });
    const all = el('button.btn.btn--small', { type: 'button' }, 'alle gezeigten');
    all.addEventListener('click', () => { st.photos.forEach((x) => st.sel.add(x.id)); draw(); });
    selbar.replaceChildren(el('strong', `${n} markiert`), all, none, addTag, confirmB, album, rights, again, hide);
  }

  /** Feld „Schlagwort hinzufügen“: Dimension wählen, Wert tippen (Vorschläge aus dem Vokabular). */
  function tagInput(onAdd) {
    const dims = st.meta?.dims ?? {};
    const dsel = el('select.input.ph-tag__dim', { 'aria-label': 'Dimension' }, DIM_ORDER.map((d) => el('option', { value: d }, dims[d]?.label ?? d)));
    dsel.value = 'keyword';
    const listId = `ph-dl-${Math.random().toString(36).slice(2)}`;
    const list = el('datalist', { id: listId });
    const fill = () => list.replaceChildren(...arr(dims[dsel.value]?.values).map((o) => el('option', { value: o.label }, o.value)));
    fill();
    dsel.addEventListener('change', fill);
    const inp = el('input.input.ph-tag__val', { type: 'text', placeholder: 'Schlagwort', list: listId, 'aria-label': 'Schlagwort' });
    const go = el('button.btn', { type: 'button' }, '+');
    const submit = () => {
      const raw = inp.value.trim();
      if (!raw) return;
      const hit = arr(dims[dsel.value]?.values).find((o) => o.label.toLowerCase() === raw.toLowerCase() || o.value === raw);
      if (!hit && dims[dsel.value] && !dims[dsel.value].open) { ctx.toast(`„${raw}“ gibt es in ${dims[dsel.value].label} nicht – als freies Schlagwort unter „Schlagworte“ eintragen.`, 5000); return; }
      onAdd(dsel.value, hit?.value ?? raw);
      inp.value = '';
    };
    go.addEventListener('click', submit);
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
    return el('span.ph-tag', dsel, inp, list, go);
  }

  // ---------------------------------------------------------------- Grossansicht
  function openLightbox(start, review) {
    document.querySelector('dialog.fg-lightbox')?.remove();
    let i = start;
    let changed = false;
    const list = st.photos;
    const dlg = el('dialog.fg-lightbox.ph-lightbox', { 'aria-label': 'Foto gross' });
    const img = el('img.fg-lb__img', { alt: '' });
    const vid = el('video.fg-lb__img.ph-video', { controls: true, playsinline: true, preload: 'metadata', hidden: true });
    const side = el('div.fg-lb__side');
    const prev = el('button.btn.fg-lb__nav', { type: 'button', 'aria-label': 'Vorheriges Foto' }, '←');
    const next = el('button.btn.fg-lb__nav', { type: 'button', 'aria-label': 'Nächstes Foto' }, '→');
    const close = el('button.btn.fg-lb__close', { type: 'button', 'aria-label': 'Schliessen' }, '✕');
    const pos = el('span.small.muted');
    const fs = fullscreenToggle(dlg, 'fg-lightbox--full');
    const frame = el('div.fg-lb__frame', img, vid);
    fs.corner(frame, img);
    dlg.append(el('div.fg-lb', el('div.fg-lb__bar', prev, pos, next, review ? el('span.badge', 'Prüfliste') : null, el('span.fg-tools__grow'), fs, close),
      el('div.fg-lb__main', el('div.fg-lb__stage', frame), side)));
    let my = 0;

    async function save(body, msg) {
      const x = list[i];
      try {
        const d = await api(`/photos/${x.id}`, body);
        changed = true;
        Object.assign(x, { title: d.title, caption: d.caption, status: d.status, consent: d.consent, usage: d.usage, faces: d.faces, cleared_for: d.cleared_for });
        if (msg) ctx.toast(msg);
        showDetail(d);
        return d;
      } catch (e) { ctx.toast(`Nicht gespeichert: ${e.message}`, 5000); return null; }
    }

    function show() {
      const x = list[i];
      pos.textContent = `${i + 1} / ${list.length}`;
      prev.disabled = i === 0; next.disabled = i === list.length - 1;
      vid.pause();
      if (x.media === 'video') {
        img.hidden = true; vid.hidden = false; vid.poster = x.view_url; vid.src = x.video_url;
      } else {
        vid.hidden = true; vid.removeAttribute('src'); vid.load(); img.hidden = false;
        img.src = x.view_url; img.alt = x.alt_en || x.caption || '';
      }
      side.replaceChildren(el('p.small.muted', 'Lade …'));
      const k = ++my;
      api(`/photos/${x.id}`).then((d) => { if (k === my) showDetail(d); }).catch((e) => side.replaceChildren(el('p.small', `Nicht ladbar: ${e.message}`)));
    }

    function showDetail(d) {
      const byDim = {};
      for (const t of arr(d.tags)) (byDim[t.dim] ??= []).push(t);
      const rejected = arr(d.tags).filter((t) => t.state === 'rejected');
      const chips = DIM_ORDER.filter((dm) => arr(byDim[dm]).some((t) => t.state !== 'rejected')).map((dm) => el('div.ph-dimrow',
        el('span.ph-dimrow__label', st.meta?.dims?.[dm]?.label ?? dm),
        el('span.ph-chips', arr(byDim[dm]).filter((t) => t.state !== 'rejected').map((t) => {
          const proposed = t.state === 'proposed';
          const c = el('span.ph-tagchip', { class: `ph-tagchip ph-tagchip--${proposed ? 'proposed' : t.source}`,
            title: proposed ? 'Vorschlag des Modells' : t.source === 'own' ? 'eigenes Schlagwort' : 'Vorschlag, bestätigt' }, t.label);
          if (proposed) {
            const ok = el('button.ph-tagchip__b', { type: 'button', 'aria-label': `${t.label} bestätigen`, title: 'bestätigen' }, '✓');
            ok.addEventListener('click', () => save({ tags: { confirm: [{ dim: t.dim, value: t.value }] } }));
            c.append(ok);
          }
          const no = el('button.ph-tagchip__b', { type: 'button', 'aria-label': `${t.label} entfernen`, title: proposed || t.source === 'llm' ? 'verwerfen' : 'entfernen' }, '✕');
          no.addEventListener('click', () => save({ tags: { reject: [{ dim: t.dim, value: t.value }] } }));
          c.append(no);
          return c;
        }))));
      const ann = d.annotation ?? {};
      const cl = CLEARED[d.cleared_for] ?? ['unknown', d.cleared_for];
      const rightsSeg = (opts, val, key, label) => segmented(opts, val, (v) => save({ [key]: v }, 'Gespeichert.'), label).el;
      const field = (key, label, multi) => {
        const f = el(multi ? 'textarea.input.ph-field' : 'input.input.ph-field', { 'aria-label': label, placeholder: label, rows: multi ? 3 : undefined });
        f.value = d[key] ?? '';
        f.addEventListener('change', () => save({ [key]: f.value }, `${label} gespeichert.`));
        return f;
      };
      const actions = el('div.row.fg-lb__links');
      const conf = el('button.btn.btn--primary', { type: 'button', title: 'Enter' }, review ? 'Geprüft – weiter ⏎' : d.status === 'confirmed' ? 'Geprüft ✓' : 'Als geprüft markieren ⏎');
      conf.disabled = d.status === 'new';
      conf.addEventListener('click', confirmAndNext);
      const again = el('button.btn', { type: 'button' }, 'Neu beschreiben lassen');
      again.addEventListener('click', () => save({ action: 'reannotate' }, 'In der Warteschlange – das Modell beschreibt das Foto neu.'));
      const hide = el('button.btn', { type: 'button' }, 'Ausblenden');
      hide.addEventListener('click', async () => {
        if (await confirmDialog({ title: 'Foto ausblenden?', text: 'Es verschwindet aus Suche und Raster; die Datei bleibt erhalten.', confirmLabel: 'Ausblenden' })) {
          await save({ action: 'remove' }, 'Ausgeblendet.'); dlg.close();
        }
      });
      actions.append(conf, again, el('a.btn', { href: d.original_url, download: '' }, 'Original'),
        d.media === 'video' ? el('a.btn', { href: d.video_url, download: '' }, 'MP4 (1080p)') : null, d.raw_url ? el('a.btn', { href: d.raw_url, download: '' }, 'RAW') : null, hide);
      const textBlock = (label, text, extra) => (text ? el('div.ph-text', el('span.label', label), el('p.small', text), extra ?? null) : null);
      side.replaceChildren(...kids(
        el('div.ph-titlebar', field('title', 'Titel'), el('span.small.muted', d.ref ?? `photo:${d.id}`)),
        el('p.small', mark(d.status === 'confirmed' ? 'ok' : d.status === 'failed' ? 'crit' : 'warn'), ` ${STATUS[d.status] ?? d.status}`,
          d.error ? el('span.muted', ` – ${d.error}`) : null),
        textBlock('Beschreibung', d.caption_de || d.caption_en),
        textBlock('Interpretation (Modell)', d.interpretation),
        el('div.ph-text', el('span.label', 'Eigene Notiz'), field('note', 'Kontext, Geschichte, was das Foto zeigt', true)),
        el('div.ph-tags', el('span.label', 'Schlagworte'), ...chips,
          tagInput((dm, v) => save({ tags: { add: [{ dim: dm, value: v }] } })),
          rejected.length ? el('details.small.muted', el('summary', `${rejected.length} verworfen`), rejected.map((t) => {
            const b = el('button.ph-chip', { type: 'button', title: 'wieder aufnehmen' }, `${t.label} ↺`);
            b.addEventListener('click', () => save({ tags: { confirm: [{ dim: t.dim, value: t.value }] } }));
            return b;
          })) : null),
        el('div.fg-lb__share.ph-rights', el('span.label', 'Rechte'),
          el('p.small', mark(cl[0]), ` ${cl[1]}`),
          el('span.small.muted', 'Einwilligung der gezeigten Personen'), rightsSeg(CONSENT, d.consent, 'consent', 'Einwilligung'),
          el('span.small.muted', 'Verwendung erlaubt bis'), rightsSeg(USAGE, d.usage, 'usage', 'Verwendung'),
          el('span.small.muted', `Erkennbare Gesichter${d.annotation?.faces_by === 'llm' && d.faces !== 'unknown' ? ' (vom Modell erkannt)' : ''}`), rightsSeg(FACES, d.faces, 'faces', 'Gesichter'),
          d.media === 'video' ? el('span.small.muted', 'Ton (Sprache erkennbarer Personen braucht die Einwilligung)') : null,
          d.media === 'video' ? rightsSeg(AUDIO, d.audio, 'audio', 'Ton') : null,
          d.media === 'video' && d.sound_cleared_for !== d.cleared_for ? el('p.small', mark('warn'), ` mit Ton nur ${CLEARED[d.sound_cleared_for]?.[1] ?? d.sound_cleared_for} – auf Folien ohne Ton verwenden`) : null),
        el('div.ph-groups', el('span.label', 'Gruppen'), el('div.ph-chips', arr(d.groups).map((g) => {
          const c = el('span.ph-tagchip.ph-tagchip--own', `${g.kind === 'survey' ? 'Erhebung' : 'Album'}: ${g.title}`);
          const x = el('button.ph-tagchip__b', { type: 'button', title: 'aus der Gruppe nehmen', 'aria-label': `aus ${g.title} nehmen` }, '✕');
          x.addEventListener('click', async () => { try { await api('/photos/bulk', { ids: [d.id], action: 'group_remove', group: g.id }); changed = true; show(); } catch (e) { ctx.toast(e.message, 5000); } });
          c.append(x);
          return c;
        }), groupPicker(d))),
        dl([['Aufgenommen', d.taken_at ? fmt.dateTime(d.taken_at.replace(' ', 'T')) : null], ['Ort', field('place', 'Ort')], ['Kamera', d.camera ?? null],
          ['Dauer', d.media === 'video' ? `${dur(d.duration)}${d.fps ? ` · ${d.fps} fps` : ''}` : null],
          ['Grösse', `${d.width} × ${d.height} px`], ['Platz für Text', ann.copy_space && ann.copy_space !== 'none' ? { left: 'links', right: 'rechts', top: 'oben', bottom: 'unten' }[ann.copy_space] : 'kaum'],
          ['Text im Bild', arr(ann.text_visible).join(' · ') || null], ['Bildnachweis', field('credit', 'Bildnachweis')],
          ['Datei', d.orig_name ?? null], ['Herkunft', d.source_path ?? null],
          ['Beschrieben', d.annotated_at ? `${fmt.dateTime(d.annotated_at.replace(' ', 'T'))} · ${ann.model ?? ''}` : null]]),
        arr(d.uses).length ? el('div.ph-text', el('span.label', 'Verwendet in'), el('ul.small', arr(d.uses).map((u) => el('li', u.title || u.ref || `Folie ${u.exhibit_id}`, u.project ? ` (${u.project})` : '', ` · ${fmt.date(u.used_at?.replace(' ', 'T'))}`)))) : null,
        arr(d.similar).length ? el('div.ph-text', el('span.label', 'Ähnliche Fotos (Serie, Duplikat)'), el('div.ph-similar', arr(d.similar).map((s) => {
          const b = el('button.ph-sim', { type: 'button', title: `photo:${s.id}` }, el('img', { src: s.thumb_url, alt: '' }));
          b.addEventListener('click', () => { const j = list.findIndex((y) => y.id === s.id); if (j >= 0) { i = j; show(); } else ctx.toast('Nicht in der aktuellen Auswahl.'); });
          return b;
        }))) : null,
        actions));
    }

    function groupPicker(d) {
      const have = new Set(arr(d.groups).map((g) => g.id));
      const sel = el('select.input.ph-gsel', { 'aria-label': 'Zu Gruppe hinzufügen' }, el('option', { value: '' }, '+ Gruppe'),
        arr(st.meta?.groups).filter((g) => !have.has(g.id)).map((g) => el('option', { value: String(g.id) }, `${g.kind === 'survey' ? 'Erhebung' : 'Album'}: ${g.title}`)),
        el('option', { value: 'new' }, '+ neues Album …'));
      sel.addEventListener('change', async () => {
        let group = sel.value === 'new' ? null : Number(sel.value);
        if (sel.value === 'new') { const t = prompt('Name des neuen Albums'); if (!t?.trim()) { sel.value = ''; return; } group = { kind: 'album', title: t.trim() }; }
        try { await api('/photos/bulk', { ids: [d.id], action: 'group_add', group }); changed = true; st.meta = await api('/photos/meta'); show(); } catch (e) { ctx.toast(e.message, 5000); }
      });
      return sel;
    }

    async function confirmAndNext() {
      const x = list[i];
      if (x.status === 'new') return;
      const d = await save({ action: 'confirm' });
      if (!d) return;
      if (review || i < list.length - 1) { if (i < list.length - 1) { i += 1; show(); } else { ctx.toast('Prüfliste durch.'); dlg.close(); } }
    }

    const go = (k) => { const j = i + k; if (j >= 0 && j < list.length) { i = j; show(); } };
    prev.addEventListener('click', () => go(-1));
    next.addEventListener('click', () => go(1));
    close.addEventListener('click', () => dlg.close());
    dlg.addEventListener('close', () => { vid.pause(); dlg.remove(); if (changed && alive) load(); });
    dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
    dialogKeys(dlg, (e) => {   // am Dokument: auch wenn Safari den Fokus aus dem Dialog nimmt
      if (e.target.closest?.('.seg, input, textarea, select, video, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === 'ArrowLeft') { e.preventDefault(); go(-1); }
      if (e.key === 'ArrowRight') { e.preventDefault(); go(1); }
      if (e.key === 'Enter' && !e.target.closest('button, a')) { e.preventDefault(); confirmAndNext(); }
    });
    let touch = null;
    frame.parentElement.addEventListener('touchstart', (e) => { touch = e.touches.length === 1 ? { x: e.touches[0].clientX, y: e.touches[0].clientY, t: Date.now() } : null; }, { passive: true });
    frame.parentElement.addEventListener('touchmove', (e) => { if (e.touches.length > 1) touch = null; }, { passive: true });
    frame.parentElement.addEventListener('touchend', (e) => {
      if (!touch || window.visualViewport?.scale > 1.05) { touch = null; return; }
      const t = e.changedTouches[0], dx = t.clientX - touch.x, dy = t.clientY - touch.y;
      if (Math.abs(dx) > 50 && Math.abs(dx) > 1.5 * Math.abs(dy) && Date.now() - touch.t < 800) go(dx < 0 ? 1 : -1);
      touch = null;
    }, { passive: true });
    document.body.append(dlg);
    dlg.showModal();
    show();
    close.focus();
  }

  // ---------------------------------------------------------------- Hochladen
  function uploadDialog() {
    const dlg = el('dialog.dialog.ph-upload');
    const files = el('input', { type: 'file', multiple: true, accept: 'image/*,video/*,.heic,.heif,.dng,.cr2,.cr3,.nef,.arw,.raf,.orf,.rw2,.pef,.srw,.mov,.mp4,.m4v,.mkv,.mts,.m2ts,.avi,.webm,.pptx,.docx,.key,.pdf' });
    const groups = arr(st.meta?.groups).filter((g) => g.kind === 'survey');
    const survey = el('input.input', { type: 'text', list: 'ph-surveys', placeholder: 'z. B. Feldstudie Spital Zürich 2019' });
    const sl = el('datalist', { id: 'ph-surveys' }, groups.map((g) => el('option', { value: g.title })));
    const project = el('input.input', { type: 'text', placeholder: 'Projekt (optional)' });
    const period = el('input.input', { type: 'text', placeholder: 'Zeitraum, z. B. 2019' });
    const place = el('input.input', { type: 'text', placeholder: 'Ort (grob)' });
    const note = el('textarea.input', { rows: 2, placeholder: 'Kontext für alle Fotos (hilft dem Modell)' });
    let consent = 'yes', usage = 'publication';   // Falk, 28.10.2026: alles Hochgeladene darf öffentlich verwendet werden
    const cSeg = segmented(CONSENT, consent, (v) => { consent = v; }, 'Einwilligung');
    const uSeg = segmented(USAGE, usage, (v) => { usage = v; }, 'Verwendung');
    const bar = el('progress.ph-progress', { max: 100, value: 0, hidden: true });
    const msg = el('p.small.muted', 'Fotos (JPEG, HEIC, RAW, PNG, TIFF) und Videos (MOV, MP4, MKV …) – auch PowerPoint, Word, Keynote und PDF: deren Bilder und Videos werden übernommen (Symbole und Logos nicht). Videos werden für Folien auf 1080p umgerechnet; das dauert bei langen Videos etwas. Gleiche Dateien werden nie doppelt gespeichert.');
    const ok = el('button.btn.btn--primary', { type: 'button' }, 'Hochladen');
    const cancel = el('button.btn', { type: 'button' }, 'Schliessen');
    const row = (label, ...c) => el('label.ph-uprow', el('span.label', label), ...c);
    dlg.append(el('div.dialog__body', el('h3', 'Fotos und Videos hochladen'), row('Dateien', files), row('Erhebung (Hauptgruppe)', survey, sl),
      el('div.ph-upgrid', row('Projekt', project), row('Zeitraum', period), row('Ort', place)), row('Notiz', note),
      row('Einwilligung (für alle)', cSeg.el), row('Verwendung erlaubt bis', uSeg.el), msg, bar, el('div.row.dialog__actions', cancel, ok)));
    cancel.addEventListener('click', () => { dlg.close(); dlg.remove(); });
    ok.addEventListener('click', () => {
      if (!files.files.length) { msg.textContent = 'Bitte Dateien wählen.'; return; }
      const fd = new FormData();
      for (const f of files.files) fd.append('files', f, f.name);
      for (const [k, v] of [['survey', survey.value], ['project', project.value], ['period', period.value], ['place', place.value], ['note', note.value], ['consent', consent], ['usage', usage]])
        if (v?.trim()) fd.append(k, v.trim());
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/photos/upload');
      bar.hidden = false; ok.disabled = true;
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) { bar.value = Math.round((e.loaded / e.total) * 100); msg.textContent = `Lade hoch … ${bar.value} %`; } };
      xhr.upload.onload = () => { msg.textContent = 'Hochgeladen – wird eingelesen (HEIC, RAW und Videos brauchen etwas) …'; };
      xhr.onload = () => {
        ok.disabled = false;
        let d = {};
        try { d = JSON.parse(xhr.responseText); } catch { /* leer */ }
        if (xhr.status !== 200) { msg.textContent = `Fehler: ${d.error ?? xhr.status}`; return; }
        msg.textContent = `${d.imported} neu, ${d.duplicates} schon vorhanden${d.failed?.length ? `, ${d.failed.length} nicht lesbar (${d.failed.map((f) => f.file.split('/').pop()).join(', ')})` : ''}.${d.imported ? ' Das Modell beschreibt die neuen Fotos jetzt im Hintergrund.' : ''}`;
        if (d.survey) st.group = String(d.survey);
        load();
      };
      xhr.onerror = () => { ok.disabled = false; msg.textContent = 'Hochladen fehlgeschlagen (Verbindung).'; };
      xhr.send(fd);
    });
    document.body.append(dlg);
    dlg.showModal();
  }

  load();
  // solange das Modell arbeitet: Stand alle 20 s nachladen (nur wenn keine Grossansicht offen ist)
  const timer = setInterval(() => {
    if (!alive || document.querySelector('dialog[open]') || document.hidden) return;
    if (st.meta?.worker?.pending) load();
  }, 20000);
  return () => { alive = false; clearInterval(timer); document.querySelector('dialog.fg-lightbox')?.remove(); };
}
