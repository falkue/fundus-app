/**
 * Fundus Review – die öffentliche Prüfseite. Eine eingeladene Person öffnet ihren persönlichen Link /r/<token>; sie gibt
 * nichts ein. Ablauf: anonym bei Firebase anmelden → access/<sha256(token)> lesen (Name, Prüfung, gültig bis) →
 * claims/<uid> anlegen → Prüfung, Folien, Prüfende, Fortschritt und Beiträge live lesen (Firestore-Regeln prüfen jeden Zugriff).
 * Beiträge: Kommentar (optional mit markiertem Bereich auf der Folie), Urteil zu Quellen und Datenpunkten
 * (korrekt / fraglich / falsch, Notiz oder bessere Quelle), „Folie durchgesehen“. Die Spark holt sie ab (synced) und schreibt
 * Antwort und Status zurück. Sprachen: Englisch (Standard), Deutsch, Ukrainisch (i18n.js). Gestaltung: Circle-Design des Portals.
 */
import { firebaseConfig, SDK } from './config.js';
import { t, lang, locale, LANGS, setLang } from './i18n.js';
import { TOOLS, anchor, shapeNode, newSvg, makeShape, bigEnough, isShape } from './shapes.js';

// die drei SDK-Module parallel laden (index.html lädt sie per modulepreload schon vor)
const [{ initializeApp }, { getAuth, signInAnonymously, signOut, onAuthStateChanged }, fs] = await Promise.all([
  import(`${SDK}/firebase-app.js`), import(`${SDK}/firebase-auth.js`), import(`${SDK}/firebase-firestore.js`)]);
const { getFirestore, initializeFirestore, persistentLocalCache, persistentMultipleTabManager, doc, getDoc, setDoc, addDoc, deleteDoc,
  collection, onSnapshot, query, orderBy, serverTimestamp } = fs;

const app = initializeApp(firebaseConfig);
const auth = getAuth(app);
// Zwischenspeicher im Browser: beim zweiten Öffnen stehen die Folien sofort da, der Server liefert nur Änderungen nach
let db;
try { db = initializeFirestore(app, { localCache: persistentLocalCache({ tabManager: persistentMultipleTabManager() }) }); }
catch { db = getFirestore(app); }

/* ------------------------------------------------------------------ kleine DOM-Helfer */
function el(spec, attrs, ...kids) {
  if (attrs && (attrs instanceof Node || typeof attrs === 'string' || Array.isArray(attrs))) { kids.unshift(attrs); attrs = null; }
  const [tag, ...cls] = spec.split('.');
  const n = document.createElement(tag || 'div');
  cls.forEach((c) => n.classList.add(c));
  for (const [k, v] of Object.entries(attrs ?? {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'on') Object.entries(v).forEach(([e, f]) => n.addEventListener(e, f));
    else if (k === 'style') Object.assign(n.style, v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat(Infinity)) if (c !== null && c !== undefined && c !== false) n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
}
const $ = (id) => document.getElementById(id);
const arr = (v) => (Array.isArray(v) ? v : []);
let toastT;
function toast(msg, ms = 3200) { const x = $('toast'); x.textContent = msg; x.dataset.show = 'true'; clearTimeout(toastT); toastT = setTimeout(() => { x.dataset.show = 'false'; }, ms); }
const badge = (text, tone) => el(`span.badge${tone ? `.badge--${tone}` : ''}`, text);
const dfmt = new Intl.DateTimeFormat(locale, { day: '2-digit', month: '2-digit', year: 'numeric' });
const tfmt = new Intl.DateTimeFormat(locale, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
const asDate = (x) => (x?.toDate ? x.toDate() : x ? new Date(x) : null);

const VERDICT = { ok: 'success', doubt: 'warning', wrong: 'danger' };
const verdict = (v) => [t(`v_${v}`), VERDICT[v] ?? 'outline'];
const STATUS = { open: 'warning', in_progress: 'accent', incorporated: 'success', answered: 'outline', rejected: 'outline', decision: 'outline', recorded: 'outline' };
const STAGE = { unchecked: 'outline', verified: 'success', confirmed: 'success', disputed: 'danger' };
const NUM = { sourced: 'success', derived: 'outline', wrong: 'danger', unverified: 'danger' };

/* ------------------------------------------------------------------ Sprache */
document.documentElement.lang = lang;
$('who').textContent = t('checking_link');
$('title').textContent = t('review_slides');
const picker = el('select.input.rva-lang', { 'aria-label': t('language') }, LANGS.map((l) => el('option', { value: l.id, selected: l.id === lang }, l.label)));
picker.addEventListener('change', () => { setLang(picker.value); location.reload(); });
document.querySelector('.rva-head').append(picker);

/** Ladeanzeige mit Text, bis die erste Folie steht (sonst sieht es aus, als lade nichts). */
function phase(text) {
  const m = $('main');
  if (!m.querySelector('.rva-stage')) m.replaceChildren(el('div.rva-loading', el('span.rva-spinner', { 'aria-hidden': 'true' }), el('p.small.muted', text)));
}

function fail(title, text) {
  $('who').textContent = t('no_access');
  $('title').textContent = title;
  $('main').replaceChildren(el('div.card.rva-msg', el('p', text)));
}

async function sha256(s) {
  const b = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
  return [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, '0')).join('');
}

/* ------------------------------------------------------------------ Zugang */
const token = (location.pathname.match(/^\/r\/([A-Za-z0-9_-]{20,})/) ?? [])[1];
if (!token) fail('Fundus Review', t('open_via_link'));
else start().catch((e) => fail(t('not_loadable'), t('not_loadable_text', { err: e.code ?? e.message })));

async function start() {
  phase(t('checking_link'));
  const [hash] = await Promise.all([sha256(token), signInAnonymously(auth)]);
  await new Promise((res) => { const off = onAuthStateChanged(auth, (u) => { if (u) { off(); res(); } }); });
  const cref = doc(db, 'claims', auth.currentUser.uid);
  // Link und eigene Anmeldung gleichzeitig lesen (eine Runde zum Server statt zwei)
  const [acc, have] = await Promise.all([getDoc(doc(db, 'access', hash)), getDoc(cref).catch(() => null)]);
  if (!acc.exists()) { fail(t('link_invalid'), t('link_invalid_text')); return; }
  const a = acc.data();
  if (!a.active || asDate(a.expires) < new Date()) { fail(t('link_expired'), t('link_expired_text')); return; }
  // Eine anonyme Anmeldung gehört zu genau einem Link (Regeln: claims nur anlegen, nie ändern). Wurde in diesem Browser
  // schon ein anderer Link geöffnet, zeigt die gespeicherte Anmeldung noch auf jene Prüfung: neu anonym anmelden.
  if (have?.exists() && have.data().hash !== hash) {
    await signOut(auth);
    await signInAnonymously(auth);
    await setDoc(doc(db, 'claims', auth.currentUser.uid), { hash, review: a.review, reviewer: a.reviewer });
  } else if (!have?.exists()) await setDoc(cref, { hash, review: a.review, reviewer: a.reviewer });
  phase(t('loading_slides'));
  const me = { id: a.reviewer, name: a.name };
  $('who').textContent = t('signed_in_as', { name: me.name });
  run(a.review, me, a, hash);
}

/* ------------------------------------------------------------------ Prüfung */
function run(rid, me, access, hash) {
  const S = { review: null, slides: [], people: new Map(), items: [], pos: 1, tab: 'comments', tool: null, rect: null,
    done: new Set(), progress: new Map(), finished: !!access.finished };
  const main = $('main');
  const img = el('img.rva-img', { alt: '' });
  const layer = newSvg();
  const marks = el('div.rva-marks');
  const frame = el('div.rva-frame', img, layer, marks);
  const prev = el('button.btn', { type: 'button', 'aria-label': t('prev_slide') }, '←');
  const next = el('button.btn', { type: 'button', 'aria-label': t('next_slide') }, '→');
  const posEl = el('span.small.rva-pos');
  const toolBtns = TOOLS.map((tool) => {
    const b = el('button.btn.rva-tool', { type: 'button', 'aria-pressed': 'false', 'data-tool': tool, title: t(`tool_${tool}_title`) },
      el('span.rva-tool__ic', { 'aria-hidden': 'true', 'data-tool': tool }), t(`tool_${tool}`));
    b.addEventListener('click', () => setDraw(S.tool === tool ? null : tool));
    return b;
  });
  const drawBtn = el('div.rva-tools', { role: 'group', 'aria-label': t('mark_area') }, toolBtns);
  const doneBtn = el('button.btn.rva-done', { type: 'button', 'aria-pressed': 'false', title: t('slide_done_title') }, t('slide_done'));
  const mine = el('span.small.rva-mine');
  doneBtn.addEventListener('click', async () => {
    if (S.finished) return;
    const p = S.pos;
    const nextSet = new Set(S.done);
    if (nextSet.has(p)) nextSet.delete(p); else nextSet.add(p);
    // the last slide finishes the review: from then on the link is read-only
    if (S.slides.every((x) => nextSet.has(x.position)) && !window.confirm(t('finish_confirm'))) return;
    doneBtn.disabled = true;
    try {
      await setDoc(doc(db, 'reviews', rid, 'progress', me.id), { slides: [...nextSet].sort((a, b) => a - b), updated: serverTimestamp() });
      toast(nextSet.has(p) ? t('marked_done') : t('unmarked'));
      if (nextSet.has(p)) { const i = S.slides.findIndex((s) => s.position === p); const nx = S.slides.slice(i + 1).find((s) => !nextSet.has(s.position)); if (nx) go(nx.position); }
    } catch (e) { toast(t('not_saved', { err: e.code ?? e.message }), 5000); }
    doneBtn.disabled = false;
  });
  const strip = el('div.rva-strip', { role: 'list' });
  const side = el('aside.rva-side');
  // Vollbild: die Folie mit Leiste (blättern, zeichnen, durchgesehen) füllt den Bildschirm; Knopf, Symbol auf der Folie, Taste F
  const fullBtn = el('button.btn.rva-full', { type: 'button', 'aria-pressed': 'false', title: t('full_title') });
  const corner = el('button.rva-corner', { type: 'button' });
  frame.append(corner);
  const stage = el('section.rva-stage', el('div.rva-bar', prev, posEl, next, el('span.rva-grow'), mine, drawBtn, doneBtn, fullBtn), frame, strip);
  main.replaceChildren(stage, side);
  const isFull = () => document.fullscreenElement ?? document.webkitFullscreenElement ?? null;
  const showFull = (on) => {
    stage.classList.toggle('rva-stage--full', on);
    document.body.classList.toggle('rva-noscroll', on);
    fullBtn.textContent = on ? t('full_off') : t('full_on');
    fullBtn.setAttribute('aria-pressed', String(on));
    corner.textContent = on ? '⤡' : '⤢';
    corner.title = on ? t('full_off').replace(/^\S+\s/, '') : t('full_on').replace(/^\S+\s/, '');
    corner.setAttribute('aria-label', corner.title);
  };
  const toggleFull = async () => {
    const on = !stage.classList.contains('rva-stage--full');
    showFull(on);
    try {
      if (on && !isFull()) await (stage.requestFullscreen?.() ?? stage.webkitRequestFullscreen?.());
      else if (!on && isFull()) await (document.exitFullscreen?.() ?? document.webkitExitFullscreen?.());
    } catch { /* nur Fenster füllen */ }
  };
  const leftFull = () => { if (!isFull() && stage.classList.contains('rva-stage--full')) showFull(false); };
  document.addEventListener('fullscreenchange', leftFull);
  document.addEventListener('webkitfullscreenchange', leftFull);
  fullBtn.addEventListener('click', toggleFull);
  corner.addEventListener('click', (e) => { e.stopPropagation(); toggleFull(); });
  corner.addEventListener('pointerdown', (e) => e.stopPropagation());   // kein Zeichnen beim Tippen auf das Symbol
  img.addEventListener('dblclick', () => { if (!S.tool) toggleFull(); });
  showFull(false);

  prev.addEventListener('click', () => go(S.pos - 1));
  next.addEventListener('click', () => go(S.pos + 1));
  document.addEventListener('keydown', (e) => {
    if (e.target.closest('input, textarea, select')) return;
    if (e.key === 'ArrowLeft') go(S.pos - 1);
    if (e.key === 'ArrowRight') go(S.pos + 1);
    if ((e.key === 'f' || e.key === 'F') && !e.metaKey && !e.ctrlKey) { e.preventDefault(); toggleFull(); }
    if (e.key === 'Escape' && stage.classList.contains('rva-stage--full') && !isFull()) showFull(false);
  });
  function setDraw(tool) {
    S.tool = tool;
    toolBtns.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.tool === tool)));
    frame.classList.toggle('rva-frame--draw', !!tool);
    if (tool) { S.tab = 'comments'; drawSide(); toast(t(`tool_${tool}_hint`)); }
  }
  // Zeichnen mit Pointer Events (Maus, Trackpad, Stift, Finger): Rahmen/Kreis aufziehen, Pfeil ziehen, Freihand mit dem Stift
  let start = null, path = [];
  let draft = null;
  const at = (e) => { const r = img.getBoundingClientRect(); return { x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)) }; };
  function drawDraft(shape) {
    draft?.remove(); draft = null;
    if (shape) { draft = shapeNode(shape, 'shape shape--draft'); layer.append(draft); }
  }
  frame.addEventListener('pointerdown', (e) => {
    if (!S.tool) return;
    start = at(e); path = [start];
    frame.setPointerCapture(e.pointerId); e.preventDefault();
  });
  frame.addEventListener('pointermove', (e) => {
    if (!start) return;
    const p = at(e);
    if (S.tool === 'pen') path.push(p);
    S.rect = makeShape(S.tool, start, p, path);
    drawDraft(S.rect);
  });
  frame.addEventListener('pointerup', () => {
    if (!start) return;
    start = null;
    if (!S.rect || !bigEnough(S.rect)) { S.rect = null; drawDraft(null); return; }
    setDraw(null); drawSide();
    side.querySelector('textarea')?.focus();
  });

  onSnapshot(doc(db, 'access', hash), (d) => {
    const f = !!d.data()?.finished;
    if (f !== S.finished) { S.finished = f; S.tool = null; drawAll(); }
  }, () => {});
  onSnapshot(doc(db, 'reviews', rid), (d) => {
    S.review = d.data();
    $('title').textContent = S.review?.title ?? t('review');
    document.title = `${S.review?.title ?? t('review')} · Fundus Review`;
    $('meta').textContent = [S.review?.project, S.review?.round > 1 ? t('round', { n: S.review.round }) : null,
      access.expires ? t('valid_until', { date: dfmt.format(asDate(access.expires)) }) : null].filter(Boolean).join(' · ');
  }, () => fail(t('no_access'), t('not_shared')));
  // erste Folie einzeln holen: die Liste aller Folien trägt Quellen und Daten mit (bei 70 Folien rund 2 MB) und kommt später
  getDoc(doc(db, 'reviews', rid, 'slides', String(S.pos))).then((d) => { if (d.exists() && !S.slides.length) { S.first = d.data(); drawAll(); } }).catch(() => {});
  onSnapshot(query(collection(db, 'reviews', rid, 'slides'), orderBy('position')), (q) => {
    S.slides = q.docs.map((d) => d.data());
    if (!S.slides.some((s) => s.position === S.pos) && S.slides.length) S.pos = S.slides[0].position;
    drawAll();
  });
  onSnapshot(collection(db, 'reviews', rid, 'reviewers'), (q) => { q.docs.forEach((d) => S.people.set(d.id, d.data())); drawAll(); });
  onSnapshot(collection(db, 'reviews', rid, 'progress'), (q) => {
    S.progress = new Map(q.docs.map((d) => [d.id, new Set(d.data().slides ?? [])]));
    S.done = S.progress.get(me.id) ?? new Set();
    drawAll();
  });
  onSnapshot(query(collection(db, 'reviews', rid, 'items'), orderBy('created')), (q) => {
    S.items = q.docs.map((d) => ({ id: d.id, ...d.data() }));
    drawAll();
  });

  const slide = () => S.slides.find((s) => s.position === S.pos) ?? (S.slides.length ? null : S.first?.position === S.pos ? S.first : null);
  const itemsOf = (s) => S.items.filter((i) => i.slide === s.position);
  function go(p) {
    if (!S.slides.some((s) => s.position === p)) return;
    S.pos = p; S.rect = null; S.showAll = null; drawDraft(null); setDraw(null); drawAll();
  }

  function drawAll() {
    const s = slide();
    if (!s) { side.replaceChildren(el('p.muted', t('loading_slides'))); return; }
    const list = S.slides.length ? S.slides : [s];
    const i = list.indexOf(s);
    const total = S.slides.length || S.review?.slideCount || 1;
    posEl.textContent = t('slide_n_of', { n: i + 1, total });
    const isDone = S.done.has(s.position);
    doneBtn.setAttribute('aria-pressed', String(isDone));
    doneBtn.textContent = S.finished ? t('done_locked') : isDone ? t('slide_done_on') : t('slide_done');
    doneBtn.disabled = S.finished;
    drawBtn.hidden = S.finished;
    const nDone = S.slides.filter((x) => S.done.has(x.position)).length;
    mine.textContent = S.slides.length && nDone === S.slides.length ? t('progress_all', { total }) : t('progress_mine', { n: nDone, total });
    prev.disabled = i === 0; next.disabled = i === list.length - 1;
    showImage(s);
    // Nachbarfolien vorladen: Blättern zeigt sofort das scharfe Bild
    [list[i - 1], list[i + 1]].forEach((x) => { if (x?.image && !preloaded.has(x.image)) { preloaded.add(x.image); new Image().src = x.image; } });
    const shapes = itemsOf(s).filter((x) => isShape(x.shape) && x.version === s.version);
    [...layer.querySelectorAll('g:not(.shape--draft)')].forEach((g) => g.remove());
    shapes.forEach((x) => layer.insertBefore(shapeNode(x.shape, `shape${x.author?.startsWith('ai:') ? ' shape--ai' : ''}${x.author === me.id ? ' shape--mine' : ''}`), draft));
    marks.replaceChildren(...shapes.map((x, k) => {
      const p = anchor(x.shape);
      return el('span.rva-pin', { title: `${x.authorName}: ${x.text}`, class: `rva-pin${x.author?.startsWith('ai:') ? ' rva-pin--ai' : ''}`,
        style: { left: `${p.x * 100}%`, top: `${p.y * 100}%` } }, String(k + 1));
    }));
    drawStrip();
    drawSide();
  }

  /** Folienbild: zuerst die kleine Vorschau (meist schon geladen), dann das scharfe Bild, sobald es da ist. */
  const preloaded = new Set();
  function showImage(s) {
    if (img.dataset.src === s.image) return;
    img.dataset.src = s.image; img.alt = s.title ?? '';
    const full = new Image();
    full.src = s.image;
    if (full.complete) { img.src = s.image; frame.classList.remove('rva-frame--loading'); return; }
    if (s.preview && s.preview !== s.image) img.src = s.preview; else img.removeAttribute('src');
    frame.classList.add('rva-frame--loading');
    const done = () => { if (img.dataset.src !== s.image) return; img.src = s.image; frame.classList.remove('rva-frame--loading'); };
    full.onload = done; full.onerror = done;
  }

  /** Vorschauleiste nur neu bauen, wenn sich Folien, Beiträge oder „durchgesehen“ ändern; beim Blättern nur markieren. */
  let stripKey = '';
  function drawStrip() {
    const key = JSON.stringify([S.slides.map((x) => [x.position, x.preview ?? x.image, x.title]), S.items.map((y) => [y.slide, y.kind]), [...S.done]]);
    if (key !== stripKey) {
      stripKey = key;
      buildStrip();
    }
    strip.querySelectorAll('.rva-thumb').forEach((b) => {
      const on = b.dataset.pos === String(S.pos);
      b.setAttribute('aria-current', on ? 'true' : 'false');
      if (on && b.offsetParent) { const l = b.offsetLeft - strip.offsetLeft; if (l < strip.scrollLeft || l + b.offsetWidth > strip.scrollLeft + strip.clientWidth) strip.scrollLeft = l - strip.clientWidth / 2 + b.offsetWidth / 2; }
    });
  }
  function buildStrip() {
    strip.replaceChildren(...S.slides.map((x, k) => {
      const n = S.items.filter((y) => y.slide === x.position && y.kind !== 'check').length;
      const b = el('button.rva-thumb', { type: 'button', role: 'listitem', 'data-pos': String(x.position), title: x.title ?? '' },
        el('img', { src: x.preview ?? x.image, alt: '', loading: 'lazy' }), el('span.rva-thumb__n', String(k + 1)), n ? el('span.rva-thumb__c', String(n)) : null,
        S.done.has(x.position) ? el('span.rva-thumb__done', { title: t('reviewed') }, '✓') : null);
      b.addEventListener('click', () => go(x.position));
      return b;
    }));
  }

  function drawSide() {
    const s = slide();
    if (!s) return;
    const its = itemsOf(s);
    const shapes = its.filter((x) => isShape(x.shape) && x.version === s.version);
    const comments = its.filter((x) => x.kind !== 'check');
    const tabs = [['comments', `${t('tab_comments')} ${comments.length || ''}`.trim()], ['sources', `${t('tab_sources')} ${arr(s.sources).length}`],
      ['data', `${t('tab_data')} ${arr(s.data).length + arr(s.numbers).length}`], ['refs', t('tab_refs')]];
    const seg = el('div.seg.rva-tabs', { role: 'tablist' }, tabs.map(([k, l]) => {
      const b = el('button.seg__btn', { type: 'button', role: 'tab', 'aria-selected': String(S.tab === k), 'aria-checked': String(S.tab === k) }, l);
      b.addEventListener('click', () => { S.tab = k; drawSide(); });
      return b;
    }));
    const head = el('div.rva-side__head', el('span.label', s.eyebrow ?? t('slide_n', { n: S.slides.indexOf(s) + 1 })), el('h2.rva-side__title', s.title ?? ''), seg);
    let body;
    if (S.tab === 'comments') body = commentsTab(s, comments, shapes);
    else if (S.tab === 'sources') body = checksTab(s, 'sources');
    else if (S.tab === 'refs') body = refsTab();
    else body = checksTab(s, 'data');
    side.replaceChildren(...[S.finished ? el('p.small.rva-finished', t('finished_banner')) : null, head, body].filter(Boolean));
  }

  function itemCard(x, num) {
    const own = x.author === me.id;
    const stKey = x.status ?? (x.synced ? 'open' : '');
    const del = own && !x.synced ? el('button.btn.btn--ghost.btn--small', { type: 'button' }, t('delete')) : null;
    del?.addEventListener('click', async () => { await deleteDoc(doc(db, 'reviews', rid, 'items', x.id)); toast(t('deleted')); });
    return el('li.rva-item', { class: `rva-item${x.author?.startsWith('ai:') ? ' rva-item--ai' : ''}` },
      el('div.rva-item__head', num ? el('span.rva-num', String(num)) : null, el('strong', own ? `${x.authorName} (${t('you')})` : x.authorName),
        el('span.small.muted', x.created ? tfmt.format(asDate(x.created)) : t('sending')), el('span.rva-grow'),
        x.verdict ? badge(...verdict(x.verdict)) : null,
        x.weight === 'suggestion' ? badge(t('suggestion'), 'outline') : null,
        STATUS[stKey] ? badge(t(`s_${stKey}`), STATUS[stKey]) : x.synced === false ? badge(t('sent_badge'), 'outline') : null),
      x.text ? el('p.rva-item__text', x.text) : null,
      x.note ? el('p.small.muted', x.note) : null,
      x.reply ? el('p.small.rva-reply', el('strong', `${x.resolvedVersion ? t('reply_version', { v: x.resolvedVersion }) : t('reply')}: `), x.reply) : null,
      del);
  }

  function commentsTab(s, comments, shapes) {
    const list = comments.length ? el('ul.rva-items', comments.map((x) => itemCard(x, shapes.indexOf(x) >= 0 ? shapes.indexOf(x) + 1 : null)))
      : el('p.small.muted', t('no_comments'));
    const ta = el('textarea.input.rva-ta', { rows: 3, placeholder: S.rect ? t('comment_marked_placeholder') : t('comment_placeholder'), 'aria-label': t('comment_label'), maxlength: 4000 });
    const send = el('button.btn.btn--primary', { type: 'button' }, t('send'));
    const clearRect = S.rect ? el('button.btn.btn--ghost.btn--small', { type: 'button' }, t('remove_mark')) : null;
    clearRect?.addEventListener('click', () => { S.rect = null; drawDraft(null); drawSide(); });
    send.addEventListener('click', async () => {
      const text = ta.value.trim();
      if (!text) { ta.focus(); return; }
      send.disabled = true;
      try {
        await addDoc(collection(db, 'reviews', rid, 'items'), {
          slide: s.position, exhibit: s.exhibit, version: s.version, kind: S.rect ? 'shape' : 'comment', text,
          author: me.id, authorName: me.name, created: serverTimestamp(), synced: false, ...(S.rect ? { shape: S.rect } : {}),
        });
        S.rect = null; drawDraft(null); ta.value = ''; toast(t('comment_sent'));
      } catch (e) { toast(t('not_sent', { err: e.code ?? e.message }), 5000); }
      send.disabled = false;
    });
    if (S.finished) return el('div.rva-tab', list);
    return el('div.rva-tab', list, el('div.rva-form', S.rect ? el('p.small', el('span.rva-num.rva-num--new', '+'), t('mark_attached'), clearRect) : null,
      ta, el('div.rva-form__row', el('span.small.muted', t('mark_hint')), send)));
  }

  /** Zahlen der Folie, wie Gemini sie geprüft hat (nur lesend) – unter „Daten“. */
  function numbersList(s) {
    const ns = arr(s.numbers);
    if (!ns.length) return null;
    return el('div.rva-nums', el('span.label', t('numbers_of_slide', { by: ns[0].by ?? 'Gemini' })), el('p.small.muted', t('numbers_hint')),
      el('ul.rva-checks', ns.map((x) => {
        const target = { kind: 'number', id: String(x.id) };
        const latest = new Map();
        S.items.filter((y) => y.kind === 'check' && y.target?.kind === 'number' && String(y.target?.id) === target.id).forEach((y) => latest.set(y.author, y));
        return el('li.rva-check',
          el('div.rva-check__head', el('span', el('strong', x.number), x.claim ? ` – ${x.claim}` : ''), badge(t(`n_${x.verdict}`), NUM[x.verdict] ?? 'outline')),
          x.note && x.verdict !== 'sourced' ? el('span.small.muted', x.note) : null,
          x.evidence ? el('blockquote.small.rva-quote', `“${x.evidence}”`) : null,
          x.deep_url ? el('a.small.rva-deep', { href: x.deep_url, target: '_blank', rel: 'noopener noreferrer' }, t('open_place')) : null,
          latest.size ? votes(latest) : null,
          x.id ? verdictButtons(s, target, latest.get(me.id), { choices: ['ok', 'wrong'], labels: { ok: t('num_ok'), wrong: t('num_wrong') } }) : null);
      })));
  }

  function checksTab(s, which) {
    const tab = checksTabInner(s, which);
    if (which === 'data') { const nl = numbersList(s); if (nl) tab.append(nl); }
    return tab;
  }

  function checksTabInner(s, which) {
    const rows = arr(s[which]);
    if (!rows.length) return el('div.rva-tab', el('p.small.muted', which === 'sources' ? t('no_sources') : t('no_data')),
      s.sources_text && which === 'sources' ? el('p.small', el('span.label', t('source_line')), s.sources_text) : null);
    const list = el('ul.rva-checks', rows.slice(0, S.showAll?.[which] ? rows.length : 8).map((r) => checkRow(s, which, r)));
    const more = rows.length > 8 && !S.showAll?.[which] ? el('button.btn.btn--small', { type: 'button' }, t('show_all', { n: rows.length })) : null;
    more?.addEventListener('click', () => { S.showAll = { ...(S.showAll ?? {}), [which]: true }; drawSide(); });
    return el('div.rva-tab', el('p.small.rva-optional', t('optional_hint')), el('p.small.muted', which === 'sources' ? t('ask_source') : t('ask_data')),
      list, more);
  }

  /** Quellenverzeichnis über alle Folien: jede Quelle einmal, mit den Folien, die sie nutzen, Fundstelle und Urteil. */
  function refsTab() {
    const refs = new Map();
    for (const x of S.slides) {
      for (const r of arr(x.sources)) {
        const k = String(r.id);
        if (!refs.has(k)) refs.set(k, { r, slides: [] });
        refs.get(k).slides.push(x);
      }
    }
    const rows = [...refs.values()];
    const noRef = S.slides.filter((x) => !arr(x.sources).length);
    return el('div.rva-tab',
      el('p.small.rva-optional', t('optional_hint')),
      el('p.small.muted', t('refs_intro', { n: rows.length, total: S.slides.length })),
      rows.length ? el('ol.rva-checks.rva-refs', rows.map(({ r, slides }) => {
        const li = checkRow(slides[0], 'sources', r);
        li.prepend(el('div.small.rva-used', t('used_on'), slides.map((x, k) => {
          const a = el('button.rva-link', { type: 'button' }, String(S.slides.indexOf(x) + 1));
          a.addEventListener('click', () => { S.tab = 'sources'; go(x.position); });
          return [k ? ', ' : '', a];
        })));
        return li;
      })) : el('p.small.muted', t('refs_none')),
      noRef.length ? el('div.small', el('span.label', t('only_source_line')),
        el('ul.rva-votes', noRef.map((x) => el('li', el('strong', `${t('slide_n', { n: S.slides.indexOf(x) + 1 })}: `), x.sources_text ?? t('no_source_given'))))) : null);
  }

  /** Urteile der Prüfenden (letztes je Person). */
  function votes(latest) {
    return el('ul.rva-votes', [...latest.values()].map((x) => el('li.small', badge(...verdict(x.verdict)), ` ${x.author === me.id ? t('you') : x.authorName}`, x.note ? ` – ${x.note}` : '')));
  }

  /** Urteilsknöpfe (freiwillig). aspect 'usage' = belegt diese Folie, 'source' = die Quelle selbst ist echt. */
  function verdictButtons(s, target, myLast, { aspect = 'usage', labels = {}, choices = ['ok', 'doubt', 'wrong'] } = {}) {
    if (S.finished) return null;
    const note = el('input.input', { type: 'text', placeholder: t('note_placeholder'), maxlength: 2000, 'aria-label': t('note') });
    const btns = choices.map((v) => {
      const l = labels[v] ?? verdict(v)[0];
      const b = el('button.btn.btn--small.rva-verdict', { type: 'button', 'data-v': v, 'aria-pressed': String(myLast?.verdict === v) }, l);
      b.addEventListener('click', async () => {
        if (v !== 'ok' && !note.value.trim()) { note.focus(); toast(t('explain_please')); return; }
        btns.forEach((x) => { x.disabled = true; });
        try {
          await addDoc(collection(db, 'reviews', rid, 'items'), {
            slide: s.position, exhibit: s.exhibit, version: s.version, kind: 'check', verdict: v, target, text: '', aspect,
            author: me.id, authorName: me.name, created: serverTimestamp(), synced: false, ...(note.value.trim() ? { note: note.value.trim() } : {}),
          });
          note.value = ''; toast(t('recorded_as', { v: l }));
        } catch (e) { toast(t('not_sent', { err: e.code ?? e.message }), 5000); }
        btns.forEach((x) => { x.disabled = false; });
      });
      return b;
    });
    return el('div.rva-verdicts', el('div.rva-check__act', btns), note);
  }

  function checkRow(s, which, r) {
    const target = which === 'sources' ? { kind: 'document', id: String(r.id) } : { kind: r.kind, id: String(r.id) };
    const mineOf = (asp) => {
      const latest = new Map();
      S.items.filter((x) => x.kind === 'check' && x.target?.kind === target.kind && String(x.target?.id) === target.id
        && (asp === 'source' ? x.aspect === 'source' : x.aspect !== 'source' && x.slide === s.position)).forEach((x) => latest.set(x.author, x));
      return latest;
    };
    const latest = mineOf('usage');
    const stage = r.stage && STAGE[r.stage] ? r.stage : 'unchecked';
    const genuine = which === 'sources' ? mineOf('source') : null;
    const gStage = r.source_stage && STAGE[r.source_stage] ? r.source_stage : 'unchecked';
    const title = which === 'sources'
      ? (r.url ? el('a', { href: r.url, target: '_blank', rel: 'noopener noreferrer' }, r.title || r.url) : el('span', r.title ?? `${t('source')} ${r.id}`))
      : el('span', `${r.label ?? ''}${r.value !== undefined && r.value !== null ? ` · ${r.value}${r.unit ? ` ${r.unit}` : ''}` : ''}${r.period ? ` (${r.period})` : ''}`);
    const link = r.deep_url || (which === 'sources' ? r.url : r.source_url);
    return el('li.rva-check',
      el('div.rva-check__head', title, badge(`${t(`g_${stage}`)}${r.confirmations > 1 ? ` (${r.confirmations})` : ''}`, STAGE[stage])),
      el('span.small.muted', which === 'sources' ? [r.publisher, r.published].filter(Boolean).join(' · ')
        : r.source_url ? el('a', { href: r.source_url, target: '_blank', rel: 'noopener noreferrer' }, r.source_title ?? t('source')) : r.source_title ?? ''),
      which === 'sources' && r.link_ok === false ? el('span.small.rva-bad', t('link_unreachable')) : null,
      r.quote || r.locator ? el('blockquote.small.rva-quote', r.locator ? el('strong', `${r.locator}: `) : null, r.quote ?? '') : null,
      link ? el('a.small.rva-deep', { href: link, target: '_blank', rel: 'noopener noreferrer' }, r.deep_url ? t('open_place') : t('open_source')) : null,
      el('span.label.rva-q', which === 'sources' ? t('q_supports') : t('q_value')),
      latest.size ? votes(latest) : null,
      verdictButtons(s, target, latest.get(me.id)),
      which === 'sources' ? el('details.rva-genuine', el('summary.small', t('q_genuine'), ' ', badge(t(`g_${gStage}`), STAGE[gStage])),
        el('p.small.muted', t('q_genuine_help')), genuine.size ? votes(genuine) : null,
        verdictButtons(s, target, genuine.get(me.id), { aspect: 'source', choices: ['ok', 'wrong'], labels: { ok: t('genuine_ok'), wrong: t('genuine_wrong') } })) : null);
  }
}
