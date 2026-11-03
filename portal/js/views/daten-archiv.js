/**
 * Datensätze › Archiv (#daten?tab=archiv): Bestandserhaltung – Prüfsummen (in Ordnung / fehlerhaft / ungeprüft, älteste
 * Prüfung, Fehlerliste), Lebenszyklus (ersetzt → Nachfolger, zurückgezogen, ruhend), Referenz-Datensätze (Rohdaten bleiben
 * an ihrem Ort: Pfad, Grösse, Zustand, letzte Prüfung), Speicher (grösste Datensätze, Anteil, Original und Parquet) und
 * „Referenz registrieren …“ (POST /api/datasets/reference).
 */
import { el, svgEl, card, emptyNote, segmented, badge } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { arr, kids, api, loadCatalog, dropCatalog, sqlDate, VIS, FREQ, tip, hideTip, put } from './daten-lib.js';

const LOC = { ok: ['ok', 'in Ordnung'], missing: ['danger', 'fehlt'], changed: ['warn', 'verändert'] };

export function render(root, ctx, alive) {
  const box = el('div.dk-arch', el('div.dk-skel', el('div.dk-skel__card'), el('div.dk-skel__card')));
  root.append(box);
  loadCatalog().then((cat) => {
    if (!alive()) return;
    const ds = arr(cat.datasets);
    const legacy = cat.legacy ? el('div.alert', 'Der Fundus-Server liefert noch keine Archivangaben (Prüfsummen, Lebenszyklus, Referenzen) – sie erscheinen nach dem nächsten Neustart des Servers. Der Speicher unten stammt aus der alten Liste.') : null;
    put(box, ...kids(legacy, el('div.dk-arch__two', fixityCard(cat), lifecycleCard(cat, ds)), referencesCard(ctx, cat, ds, alive), storageCard(cat, ds), orphansCard(cat)));
  }).catch((e) => put(box, el('div.alert.alert--error', `Katalog nicht ladbar: ${e.message}`)));
}

const dlink = (d, label) => el('a', { href: `#daten?d=${d?.slug ?? d?.id}` }, label ?? d?.title ?? '?');

// ---------------------------------------------------------------- Prüfsummen
function fixityCard(cat) {
  const f = cat.stats?.fixity;
  if (!f) return card({ title: 'Prüfsummen', accent: 'Prüfsummen' }, emptyNote('Noch keine Prüfung gelaufen.'));
  const ok = f.ok ?? 0, bad = f.failed ?? 0, un = f.unchecked ?? 0;
  const tot = Math.max(1, ok + bad + un);
  const W = 600, H = 16;
  const seg = (x, w, cls, label) => (w > 0 ? svgEl('rect', { x, y: 0, width: Math.max(2, w), height: H, class: cls }, svgEl('title', {}, label)) : null);
  const wOk = (ok / tot) * W, wBad = (bad / tot) * W;
  const bar = svgEl('svg', { class: 'dk-fix__bar', viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', role: 'img', 'aria-label': `${ok} in Ordnung, ${bad} fehlerhaft, ${un} ungeprüft` },
    seg(0, wOk, 'dk-fix--ok', `${fmt.int(ok)} in Ordnung`), seg(wOk, wBad, 'dk-fix--bad', `${fmt.int(bad)} fehlerhaft`), seg(wOk + wBad, W - wOk - wBad, 'dk-fix--un', `${fmt.int(un)} noch ungeprüft`));
  const fails = arr(f.failures);
  const byId = cat.byId;
  return card({ title: 'Prüfsummen der Dateien', accent: 'Prüfsummen',
    sub: 'Fundus rechnet nachts die Prüfsummen der gespeicherten Originale nach (zuerst die am längsten nicht geprüften, bis zu einer Datenmenge je Nacht) und prüft, ob die Parquet-Kopien vorhanden und lesbar sind.' },
  el('div.dk-fix__nums',
    el('span', el('span.dk-dot.dk-dot--ok'), el('strong', fmt.int(ok)), ' in Ordnung'),
    el('span', el('span.dk-dot.dk-dot--bad'), el('strong', fmt.int(bad)), ' fehlerhaft'),
    el('span', el('span.dk-dot.dk-dot--un'), el('strong', fmt.int(un)), ' noch ungeprüft')),
  bar,
  el('p.small.muted', f.oldest_verified ? `Älteste Prüfung: ${fmt.date(sqlDate(f.oldest_verified))} (${fmt.ago(sqlDate(f.oldest_verified))})` : 'Noch keine Datei geprüft.'),
  bad ? (fails.length ? el('ul.dk-fix__fails', fails.map((x) => {
    const id = Number(String(x.dataset ?? x.dataset_id ?? '').replace(/^dataset:/, ''));
    const ds = byId?.get(id);
    return el('li', badge('fehlerhaft', 'danger'), ' ', dlink(ds ?? { id }, ds?.title ?? x.title ?? `dataset:${id}`),
      el('span.small.muted', ` · ${kids(x.file ?? x.name, x.version ? `Version ${x.version}` : null, x.error, x.verified_at ? `geprüft ${fmt.dateTime(sqlDate(x.verified_at))}` : null).join(' · ')}`));
  }))
    : el('p.small', `${fmt.int(bad)} Dateien fehlerhaft – im Steckbrief des Datensatzes unter „Dateien“ zu sehen.`)) : ok ? el('p.small', badge('alles in Ordnung', 'ok'), ' Keine fehlerhafte Datei.') : null);
}

// ---------------------------------------------------------------- Lebenszyklus
function lifecycleCard(cat, ds) {
  const sup = ds.filter((d) => d.lifecycle === 'superseded');
  const wd = ds.filter((d) => d.lifecycle === 'withdrawn');
  const dor = ds.filter((d) => d.dormant).sort((a, b) => (b.bytes ?? 0) - (a.bytes ?? 0));
  const list = (items, row, empty) => (items.length ? el('ul.dk-life__list', items.map(row)) : el('p.small.muted', empty));
  const succ = (d) => cat.byId?.get(d.superseded_by);
  return card({ title: 'Lebenszyklus', accent: 'Lebenszyklus',
    sub: 'Ersetzte Datensätze bleiben zitierbar, Sessions bekommen den Nachfolger genannt; zurückgezogene erscheinen nicht mehr in der Suche der Sessions. Ruhend heisst: 90 Tage von niemandem genutzt.' },
  el('div.dk-life',
    el('section', el('h4', 'Ersetzt ', el('span.muted', String(sup.length))),
      list(sup, (d) => el('li', dlink(d), el('div.small.muted', '→ ', succ(d) ? dlink(succ(d), `${succ(d).ref} ${succ(d).title}`) : `dataset:${d.superseded_by ?? '?'}`)), 'Keiner.')),
    el('section', el('h4', 'Zurückgezogen ', el('span.muted', String(wd.length))),
      list(wd, (d) => el('li', dlink(d), d.lifecycle_note ? el('div.small.muted', d.lifecycle_note) : null), 'Keiner.')),
    el('section', el('h4', 'Ruhend ', el('span.muted', String(dor.length))),
      list(dor.slice(0, 8), (d) => el('li', dlink(d), el('div.small.muted', kids(fmt.bytes(d.bytes), d.last_used ? `zuletzt genutzt ${fmt.ago(sqlDate(d.last_used))}` : 'nie genutzt').join(' · '))), 'Keiner.'),
      dor.length > 8 ? el('a.small', { href: '#daten?state=dormant' }, `Alle ${dor.length} im Katalog`) : null)));
}

// ---------------------------------------------------------------- Referenzen
function referencesCard(ctx, cat, ds, alive) {
  const refs = ds.filter((d) => d.holding === 'reference');
  const add = el('button.btn', { type: 'button' }, 'Referenz registrieren …');
  add.addEventListener('click', () => referenceDialog(ctx));
  const body = el('div');
  const c = card({ title: 'Referenzen auf Rohdaten', accent: 'Referenzen', tools: [add],
    sub: 'Rohdaten, die an ihrem Ort bleiben (Ordner oder Datei auf dem Spark): Fundus kopiert, profiliert und liest sie nicht, beschreibt sie nur aus README und Dateiliste und prüft nachts, ob sie noch da und unverändert sind.' }, body);
  if (!refs.length) { body.append(emptyNote('Noch keine Referenz registriert.')); return c; }
  const row = (d, loc) => {
    const [tone, word] = LOC[loc?.status] ?? ['outline', loc ? (loc.status ?? 'unbekannt') : 'lädt …'];
    return el('tr', el('td', dlink(d), el('div.small.muted', d.ref)),
      el('td.dk-path', { title: loc?.path ?? '' }, loc?.path ?? '–'),
      el('td.num', fmt.bytes(loc?.bytes ?? d.bytes_reference ?? d.bytes)), el('td.num', loc?.files != null ? fmt.int(loc.files) : '–'),
      el('td', badge(word, tone)), el('td.small', loc?.checked_at ? fmt.ago(sqlDate(loc.checked_at)) : '–'));
  };
  const tbody = el('tbody', refs.map((d) => row(d, d.location)));
  body.append(el('div.table-wrap', el('table.table.table--compact.dk-reftable',
    el('thead', el('tr', ['Datensatz', 'Pfad', 'Grösse', 'Dateien', 'Zustand', 'Geprüft'].map((h, i) => el('th', { class: i === 2 || i === 3 ? 'num' : null }, h)))), tbody)));
  // Pfad und Zustand stehen im Steckbrief (location); der Katalog nennt sie nicht immer → nachladen (wenige Referenzen)
  const missing = refs.filter((d) => !d.location).slice(0, 40);
  if (missing.length) {
    Promise.all(missing.map((d) => api(`/${d.id}`).then((x) => [d.id, x.location ?? null]).catch(() => [d.id, null]))).then((locs) => {
      if (!alive()) return;
      const m = new Map(locs);
      put(tbody, ...refs.map((d) => row(d, d.location ?? m.get(d.id) ?? { status: null })));
    });
  }
  return c;
}

function referenceDialog(ctx) {
  const dlg = el('dialog.dialog.dk-refdlg');
  const inp = (ph, type = 'text') => el('input.input', { type, placeholder: ph });
  const f = {
    path: inp('/home/user/… (Ordner oder Datei auf dem Spark)'), title: inp('Titel'), publisher: inp('Herausgeber, z. B. Eurostat'),
    source_title: inp('Titel der Quelle'), url: inp('https://…', 'url'), license: inp('Lizenz'), published: inp('z. B. 2026-10-30'),
    project: inp('Projekt (optional)'), update_hint: inp('Wo und wie neue Ausgaben holen (URL, Skript)'), retrieved_at: inp('', 'date'), script: inp('Skript, das die Daten geholt hat (Pfad)'),
  };
  const descr = el('textarea.input', { rows: 3, placeholder: 'Was liegt dort, woher, wie erhoben? (sonst aus README und Dateiliste)' });
  let visibility = 'public';
  const vis = segmented(VIS, visibility, (v) => { visibility = v; }, 'Sichtbarkeit');
  const freq = el('select.input', { 'aria-label': 'Rhythmus' }, FREQ.map(([v, l]) => el('option', { value: v }, l)));
  const msg = el('p.small.muted', 'Nur Pfade auf dem Spark unter dem eigenen Home-Verzeichnis. Fundus kopiert nichts und schaut nicht in die Daten – beschrieben wird aus README-Dateien und der Dateiliste.');
  const ok = el('button.btn.btn--primary', { type: 'button' }, 'Registrieren');
  const cancel = el('button.btn', { type: 'button' }, 'Abbrechen');
  const row = (label, c) => el('label.ph-uprow', el('span.label', label), c);
  dlg.append(el('div.dialog__body', el('h3', 'Referenz ', el('em', 'registrieren')),
    row('Pfad', f.path), row('Titel', f.title), row('Beschreibung', descr),
    el('div.dk-refgrid', row('Herausgeber', f.publisher), row('Titel der Quelle', f.source_title), row('URL', f.url), row('Lizenz', f.license),
      row('Veröffentlicht', f.published), row('Abgerufen am', f.retrieved_at), row('Projekt', f.project), row('Neue Ausgaben', freq)),
    row('Skript', f.script), row('Hinweis für neue Ausgaben', f.update_hint), row('Sichtbarkeit', vis.el), msg, el('div.row.dialog__actions', cancel, ok)));
  const close = () => { dlg.close(); dlg.remove(); };
  cancel.addEventListener('click', close);
  ok.addEventListener('click', async () => {
    const path = f.path.value.trim();
    if (!path.startsWith('/') && !path.startsWith('~')) { msg.textContent = 'Bitte einen absoluten Pfad auf dem Spark angeben (beginnt mit / oder ~).'; f.path.focus(); return; }
    if (!f.title.value.trim()) { msg.textContent = 'Bitte einen Titel angeben.'; f.title.focus(); return; }
    const body = { path, description: descr.value.trim() || undefined, visibility, frequency: freq.value || undefined };
    for (const [k, i] of Object.entries(f)) if (k !== 'path' && i.value.trim()) body[k] = i.value.trim();
    ok.disabled = true; msg.textContent = 'Registriere …';
    try {
      const r = await api('/reference', body);
      dropCatalog();
      ctx.toast('Referenz registriert – das lokale Modell beschreibt sie aus README und Dateiliste.', 5000);
      close();
      location.hash = `#daten?d=${r.slug ?? r.id}`;
    } catch (e) { ok.disabled = false; msg.textContent = `Nicht registriert: ${e.message}`; }
  });
  document.body.append(dlg);
  dlg.showModal();
  f.path.focus();
}

// ---------------------------------------------------------------- Speicher
function storageCard(cat, ds) {
  const disk = (d) => d.bytes_disk ?? d.bytes ?? 0;
  const stored = ds.filter((d) => d.holding !== 'reference');
  const total = stored.reduce((a, d) => a + disk(d), 0);
  const top = [...stored].sort((a, b) => disk(b) - disk(a)).slice(0, 15);
  const topSum = top.reduce((a, d) => a + disk(d), 0);
  const s = cat.stats ?? {};
  if (!total) return card({ title: 'Speicher', accent: 'Speicher' }, emptyNote('Nichts gespeichert.'));
  const max = disk(top[0]) || 1;
  const rows = top.map((d) => {
    const orig = Math.min(d.bytes ?? 0, disk(d));
    const pq = Math.max(0, disk(d) - orig);
    const W = 100;
    const bar = svgEl('svg', { class: 'dk-st__bar', viewBox: `0 0 ${W} 10`, preserveAspectRatio: 'none', 'aria-hidden': 'true' },
      svgEl('rect', { x: 0, y: 0, width: (orig / max) * W, height: 10, class: 'dk-st--orig' }),
      pq ? svgEl('rect', { x: (orig / max) * W, y: 0, width: (pq / max) * W, height: 10, class: 'dk-st--pq' }) : null);
    const r = el('a.dk-st__row', { href: `#daten?d=${d.slug}` },
      el('span.dk-st__title', { title: d.title }, d.title), el('span.dk-st__track', bar),
      el('span.dk-st__val', fmt.bytes(disk(d))), el('span.dk-st__pct', fmt.pct((disk(d) / total) * 100, 1)));
    r.addEventListener('mousemove', (e) => tip(e, [d.title, kids(`Original ${fmt.bytes(orig)}`, pq ? `Parquet ${fmt.bytes(pq)}` : null).join(' · '), `${fmt.pct((disk(d) / total) * 100, 1)} des Speichers`]));
    r.addEventListener('mouseleave', hideTip);
    return r;
  });
  const rest = stored.length - top.length;
  return card({ title: 'Speicher je Datensatz', accent: 'Speicher',
    sub: `${fmt.bytes(total)} auf dem Spark${s.bytes_parquet != null ? ` (Original ${fmt.bytes(s.bytes_original)}, Parquet-Kopien ${fmt.bytes(s.bytes_parquet)})` : ''}${s.bytes_reference ? `; Referenzen zusätzlich ${fmt.bytes(s.bytes_reference)} an ihrem Ort` : ''}. Die ${top.length} grössten Datensätze belegen ${fmt.pct((topSum / total) * 100)} davon.` },
  el('div.dk-st__legend.small', el('span', el('span.dk-sw.dk-st--orig'), 'Original'), el('span', el('span.dk-sw.dk-st--pq'), 'Parquet-Kopie'), el('span.muted', 'Anteil am Speicher')),
  el('div.dk-st', rows),
  rest > 0 ? el('p.small.muted', `Übrige ${fmt.int(rest)} Datensätze zusammen ${fmt.bytes(total - topSum)} (${fmt.pct(((total - topSum) / total) * 100, 1)}).`) : null);
}

// ---------------------------------------------------------------- Verwaiste Dateien (nur Anzeige; Löschen nur im Terminal)
const ORPHAN = { file: 'Datei ohne Datensatz', tmp: 'angefangene Kopie (tmp)', incoming: 'liegengebliebener Import' };
function orphansCard(cat) {
  const o = cat.stats?.orphans;
  if (!o) return null;
  const list = arr(o.list);
  return card({ title: 'Verwaiste Dateien', accent: 'Verwaiste',
    sub: 'Dateien im Datenordner, auf die kein Datensatz mehr verweist (mindestens 24 Stunden unberührt) – gefunden beim nächtlichen Prüflauf. Entfernen nur auf Falks Anweisung im Terminal: fundus datasets orphans --purge.' },
  o.checked_at == null ? el('p.small.muted', 'Noch nicht geprüft – der nächtliche Prüflauf (oder fundus datasets orphans) zählt sie.')
    : el('p', el('strong', fmt.int(o.files)), ` ${o.files === 1 ? 'Datei' : 'Dateien'} · ${fmt.bytes(o.bytes)}`, el('span.small.muted', ` · geprüft ${fmt.ago(sqlDate(o.checked_at))}`)),
  list.length ? el('div.table-wrap.ds-scroll', el('table.table.table--compact', el('thead', el('tr', el('th', 'Pfad'), el('th', 'Art'), el('th.num', 'Grösse'), el('th', 'geändert'))),
    el('tbody', list.map((x) => el('tr', el('td.dk-path', x.path), el('td.small', ORPHAN[x.kind] ?? x.kind), el('td.num', fmt.bytes(x.bytes)),
      el('td.small', x.mtime ? fmt.date(typeof x.mtime === 'number' ? x.mtime * 1000 : sqlDate(x.mtime)) : '–')))))) : null);
}
