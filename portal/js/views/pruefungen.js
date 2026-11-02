/**
 * Bereich „Prüfungen“ des Fundus-Portals: alle freigegebenen Prüfungen (Personen per Link, Gemini) mit Fortschritt,
 * die Punkte, die auf deine Entscheidung warten, und je Prüfung Personen (Links kopieren, entziehen, einladen), Folien
 * und das Feedback mit seinem Status. Hash: #pruefungen (Übersicht), #pruefungen?r=<id> (eine Prüfung).
 * Daten: GET /api/reviews, GET /api/reviews/<id>, POST /api/reviews/<id> {action}, GET /api/feedback?review|status.
 */
import { el, pageHead, card, emptyNote, dataTable, badge, confirmDialog } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { api, feedbackItem, REVIEW_STATUS, GEMINI, linkField, progressRow } from './review-ui.js';
import { openViewer } from './viewer.js';

const fullPng = (u) => (u ? u.replace(/\.preview\.png(\?|$)/, '.png$1') : u);
/** Vorschaubild, das per Klick die Grossansicht öffnet (blättern über alle Bilder der Gruppe). */
const zoomImg = (list, k, w, h) => {
  const s = list[k];
  const img = el('img.rv-zoom', { src: s.preview, alt: '', loading: 'lazy', width: w, height: h, title: 'Gross ansehen', tabindex: '0', role: 'button' });
  const open = () => openViewer(list.map((x) => ({ png: fullPng(x.preview), preview: x.preview, title: x.title, sub: x.sub, href: x.href })), k);
  img.addEventListener('click', open);
  img.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  return img;
};

const arr = (v) => (Array.isArray(v) ? v : []);
const kids = (...a) => a.filter((x) => x !== null && x !== undefined && x !== false);
const SCOPE = { selection: 'markierte Folien', set: 'Foliensatz', deck: 'Deck' };
const GEMINI_SHORT = { queued: 'vorgemerkt', running: 'prüft', done: 'fertig', failed: 'fehlgeschlagen' };
const ts = (v) => (v ? fmt.date(String(v).replace(' ', 'T')) : '–');
const LOOP_STATE = { queued: ['vorgemerkt', 'outline'], checking: ['Gemini prüft', 'info'], waiting: ['Session ist dran', 'warning'],
  done: ['fertig', 'success'], stopped: ['gestoppt', 'outline'] };
const seq = (a) => (arr(a).length ? arr(a).join(' → ') : '–');

/** Prüfschleifen aus Sessions: Runde, Zustand und die Kennzahl, wie stark der Folienumfang je Runde schrumpft. */
function loopsCard(loops) {
  if (!arr(loops).length) return null;
  const cols = [
    { key: 'project', label: 'Projekt', render: (l) => `${l.project} · #${l.loop}` },
    { key: 'round', label: 'Runde', num: true, render: (l) => `${l.round}/${l.max_rounds}` },
    { key: 'state', label: 'Zustand', render: (l) => {
      const [t, tone] = LOOP_STATE[l.state] ?? [l.state, 'outline'];
      return el('span', badge(l.state === 'checking' && l.running ? `${t} ${l.running.done ?? 0}/${l.running.total ?? '?'}` : t, tone),
        l.state === 'waiting' && l.open_errors ? el('span.small.muted', ` ${fmt.int(l.open_errors)} Fehler offen`) : null);
    } },
    { key: 'vol', label: 'Folien je Runde', render: (l) => seq(l.kpi?.volume_per_round) },
    { key: 'err', label: 'mit Fehlern', render: (l) => seq(l.kpi?.errors_per_round) },
    { key: 'kpi', label: 'Konvergenz', num: true, render: (l) => {
      const k = l.kpi ?? {};
      if (k.convergence_pct == null) return '–';
      return el('span', { class: k.shrinking ? 'rv-ok' : 'rv-err', title: `Je Runde kleiner um: ${arr(k.shrink_pct).map((x) => `${x} %`).join(', ')}` },
        `${fmt.num1(k.convergence_pct)} %`);
    } },
    { key: 'first', label: 'Runde 1 fehlerfrei', num: true, render: (l) => (l.kpi?.first_pass_clean_pct == null ? '–' : `${l.kpi.first_pass_clean_pct} %`) },
    { key: 'reason', label: 'Ergebnis', render: (l) => l.reason ?? (l.state === 'waiting' ? 'wartet auf die Session' : '–') },
  ];
  return card({ title: 'Prüfschleifen', accent: 'Gemini',
    sub: 'Aus einer Session gestartet: Gemini prüft, die Session arbeitet ein, Gemini prüft die überarbeiteten Folien – bis nichts mehr zu ändern ist (höchstens die angegebenen Runden). Konvergenz = wie viel kleiner der Folienumfang im Schnitt je Runde wird.' },
    dataTable(cols, loops));
}

export function render(root, ctx) {
  let alive = true;
  let timer = null;
  const rid = new URLSearchParams(location.hash.split('?')[1] ?? '').get('r');
  const head = pageHead({ title: 'Prüfungen', accent: 'Prüfungen', lede: 'Lade Prüfungen …' });
  const body = el('div.rv-page', { 'aria-live': 'polite' });
  root.append(head, body);
  const fail = (e) => { if (alive) body.replaceChildren(el('div.alert.alert--error', el('strong', 'Nicht ladbar. '), e.message)); };
  const reopen = async (d, instruction) => {
    try { await api('/api/feedback', { id: d.id, action: 'reopen', instruction }); ctx.toast('Punkt wieder offen – die nächste Session arbeitet ihn ein.'); }
    catch (e) { ctx.toast(`Nicht gespeichert: ${e.message}`, 5000); return; }
    load(); ctx.refreshNav?.();
  };
  const slideGroups = (slides) => {
    const pics = arr(slides).filter((s) => s.preview).map((s) => ({ preview: s.preview, title: s.title || s.ref, sub: `${s.ref} · v${s.current_version}`,
      href: `#folien?p=${encodeURIComponent(s.ref.split('/')[0])}` }));
    return el('div.rv-groups', arr(slides).map((s) => el('section.rv-group',
    el('div.rv-group__head',
      s.preview ? zoomImg(pics, pics.findIndex((x) => x.preview === s.preview), 160, 90) : null,
      el('div', el('strong', s.title || s.ref), el('p.small.muted', `${s.ref} · aktuell v${s.current_version}`),
        el('a.small', { href: `#folien?p=${encodeURIComponent(s.ref.split('/')[0])}` }, 'Im Projekt öffnen'))),
    el('ul.rv-fbs', arr(s.items).map((d) => feedbackItem(d, { onReopen: reopen }))))));
  };

  function overview() {
    Promise.all([api('/api/reviews'), api('/api/feedback?status=decision')]).then(([d, dec]) => {
      const loops = arr(d.loops);
      if (!alive) return;
      const rs = arr(d.reviews);
      head.querySelector('.lede').textContent = `${fmt.plural(rs.length, 'Prüfung', 'Prüfungen')}. Freigeben kannst du im Bereich Folien: Folien markieren, dann „Mit Gemini prüfen“ oder „Personen einladen“.`;
      const cols = [
        { key: 'title', label: 'Prüfung', render: (r) => el('a', { href: `#pruefungen?r=${r.id}` }, r.title) },
        { key: 'project', label: 'Projekt' },
        { key: 'slides', label: 'Folien', num: true, render: (r) => `${fmt.int(r.slides)} · ${SCOPE[r.scope] ?? r.scope}` },
        { key: 'who', label: 'Prüft', render: (r) => kids(r.people ? `${fmt.plural(r.people, 'Person', 'Personen')}` : null,
          r.gemini ? `Gemini (${GEMINI_SHORT[r.gemini] ?? r.gemini}${r.gemini === 'running' && r.gemini_progress ? ` ${r.gemini_progress.done ?? 0}/${r.gemini_progress.total ?? r.slides}` : ''})` : null).join(' · ') || '–' },
        { key: 'progress', label: 'Fortschritt', render: (r) => arr(r.progress).length
          ? el('div.rv-pmini', arr(r.progress).map((p) => el('span.small', { title: `${p.name}: ${p.done} von ${p.total} Folien durchgesehen` },
            `${p.name.split(' ')[0]} ${p.percent} %`))) : '–' },
        { key: 'status', label: 'Status', render: (r) => {
          const [t, tone] = r.status === 'closed' && !r.people && r.gemini === 'done' ? ['fertig', 'success']
            : r.completed_at && r.status !== 'closed' ? REVIEW_STATUS.completed : REVIEW_STATUS[r.status] ?? [r.status, 'outline'];
          return badge(t, tone);
        } },
        { key: 'fb', label: 'Feedback', render: (r) => `${fmt.int(r.feedback.open)} Fehler offen · ${fmt.int(r.feedback.suggestions ?? 0)} Anregungen · ${fmt.int(r.feedback.done)} verarbeitet${r.feedback.decision ? ` · ${r.feedback.decision} Entscheidung` : ''}` },
        { key: 'created_at', label: 'Angelegt', render: (r) => ts(r.created_at) },
      ];
      const decSlides = arr(dec.slides);
      // laufend: offen, nicht abgeschlossen, Links nicht abgelaufen – oder Gemini prüft gerade; neueste zuerst
      const now = new Date().toISOString().slice(0, 19).replace('T', ' ');
      const running = (r) => ['queued', 'running'].includes(r.gemini)
        || (['draft', 'open'].includes(r.status) && !r.completed_at && !(r.expires_at && r.expires_at < now));
      const newest = (a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? ''));
      const live = rs.filter(running).sort(newest);
      const done = rs.filter((r) => !running(r)).sort(newest);
      const table = (list) => dataTable(cols, list, { onRow: (r) => { location.hash = `#pruefungen?r=${r.id}`; } });
      body.replaceChildren(...kids(
        card({ title: 'Laufende Prüfungen', accent: 'Laufende', sub: live.length ? 'Offen, Links gültig oder Gemini prüft gerade – neueste zuerst.' : null },
          live.length ? table(live) : emptyNote(rs.length ? 'Gerade läuft keine Prüfung.' : 'Noch keine Prüfung angelegt.')),
        decSlides.length ? card({ title: 'Wartet auf deine Entscheidung', accent: 'Entscheidung',
          sub: 'Sessions haben diese Punkte an dich übergeben. Schreib deine Vorgabe und klicke „Entscheiden“: der Punkt geht mit deiner Vorgabe zurück an die nächste Session.' }, slideGroups(decSlides)) : null,
        loopsCard(loops),
        done.length ? card({ title: 'Abgeschlossen und abgelaufen', accent: 'Abgeschlossen', sub: 'Abgeschlossene Prüfungen und solche, deren Links abgelaufen sind – neueste zuerst.' }, table(done)) : null));
      ctx.setStatus('ok', 'verbunden');
      poll(rs.some((r) => ['queued', 'running'].includes(r.gemini)) || loops.some((l) => ['queued', 'checking', 'waiting'].includes(l.state)),
        rs.some((r) => r.status !== 'closed' && r.people));
    }).catch(fail);
  }

  function detail() {
    Promise.all([api(`/api/reviews/${rid}`), api(`/api/feedback?review=${rid}&status=all`), api('/api/reviewers')]).then(([r, fb, rvs]) => {
      if (!alive) return;
      head.querySelector('h1').textContent = r.title;
      const [st, tone] = r.status === 'closed' && !arr(r.reviewers).length && r.gemini === 'done' ? ['fertig', 'success']
        : r.completed_at && r.status !== 'closed' ? REVIEW_STATUS.completed : REVIEW_STATUS[r.status] ?? [r.status, 'outline'];
      head.querySelector('.lede').replaceChildren(badge(st, tone), ' ', r.gemini ? badge(`${GEMINI[r.gemini] ?? r.gemini} · ${r.gemini_model ?? ''}`, 'accent') : '',
        ` Projekt ${r.project} · ${fmt.plural(r.slides, 'Folie', 'Folien')} (${SCOPE[r.scope] ?? r.scope}) · angelegt ${ts(r.created_at)}${r.expires_at ? ` · Links gültig bis ${ts(r.expires_at)}` : ''}`);
      document.title = `${r.title} · Prüfungen · Fundus-Portal`;
      const people = arr(r.reviewers);
      const act = async (payload, msg) => {
        try { await api(`/api/reviews/${r.id}`, payload); ctx.toast(msg); load(); }
        catch (e) { ctx.toast(`Nicht gespeichert: ${e.message}`, 5000); }
      };
      const who = el('input.input', { type: 'text', list: 'rv-known-d', placeholder: 'Name, Vorname', 'aria-label': 'Person einladen' });
      const invite = el('button.btn.btn--small', { type: 'button' }, 'Einladen');
      invite.addEventListener('click', () => { const v = who.value.trim(); if (v) act({ action: 'invite', people: [v] }, `${v} eingeladen.`); });
      who.addEventListener('keydown', (e) => { if (e.key === 'Enter') invite.click(); });
      const closeBtn = el('button.btn', { type: 'button', disabled: r.status === 'closed' }, 'Prüfung abschliessen');
      closeBtn.addEventListener('click', async () => {
        if (await confirmDialog({ title: 'Prüfung abschliessen?', text: 'Die Links bleiben bis zum Ablauf zum Ansehen offen, ändern kann niemand mehr etwas. Das Feedback bleibt in Fundus.', confirmLabel: 'Abschliessen' })) act({ action: 'close' }, 'Prüfung abgeschlossen – Links nur noch lesbar.');
      });
      const purgeBtn = el('button.btn.btn--ghost', { type: 'button', disabled: !r.published }, 'Löschen');
      purgeBtn.addEventListener('click', async () => {
        if (await confirmDialog({ title: 'Prüfung aus der Review-App löschen?', text: 'Alle Links werden sofort ungültig, Folien und Beiträge werden aus Firebase entfernt. Das Feedback bleibt in Fundus.', confirmLabel: 'Löschen' })) act({ action: 'purge' }, 'Prüfung gelöscht – Links ungültig.');
      });
      const items = arr(fb.slides);
      body.replaceChildren(...kids(
        el('p', el('a', { href: '#pruefungen' }, '← Alle Prüfungen')),
        r.gemini ? geminiCard(r) : null,
        card({ title: 'Personen', accent: 'Personen', tools: el('div.row', closeBtn, purgeBtn),
          sub: people.length ? `Jede Person hat ihren eigenen Link; ihr Name steht an allen ihren Beiträgen.${r.published ? ' Die Prüfung ist veröffentlicht; Beiträge erscheinen hier nach spätestens einer Minute.' : ' Die Links gehen in Betrieb, sobald die Prüfung veröffentlicht ist (nach einer Gemini-Vorprüfung wartet sie auf deren Ende).'}` : 'Diese Prüfung läuft ohne Personen, nur mit Gemini.' },
        people.length ? el('ul.rv-people', people.map((p) => {
          const rev = el('button.btn.btn--ghost.btn--small', { type: 'button', disabled: !p.link || r.status === 'closed' }, 'Entziehen');
          rev.addEventListener('click', () => act({ action: 'revoke', person: p.label }, `Link von ${p.name} entzogen.`));
          return el('li.rv-people__row', el('div.rv-people__head', el('strong', p.name), el('span.small.muted', p.link ? `eingeladen ${ts(p.invited_at)}` : `entzogen ${ts(p.revoked_at)}`),
            el('span.fg-tools__grow'), rev), p.link ? linkField(p.link, ctx.toast) : null);
        })) : null,
        r.status !== 'closed' ? el('div.row.rv-who', who, invite, el('datalist', { id: 'rv-known-d' }, arr(rvs.reviewers).map((x) => el('option', { value: x.label })))) : null),
        arr(r.progress).length ? card({ title: 'Fortschritt der Personen', accent: 'Fortschritt',
          sub: '„Durchgesehen“ heisst: Die Person ist mit der Folie durch – nicht, dass alles stimmt. Beiträge und Urteile stehen unten im Feedback.' },
        el('div.rv-progress-list', arr(r.progress).map((p) => progressRow(p)))) : null,
        card({ title: 'Folien', accent: 'Folien' }, el('div.rv-thumbs', arr(r.slides_list).map((s, k, all) => el('figure.rv-thumb',
          s.preview ? zoomImg(all.filter((x) => x.preview).map((x) => ({ preview: x.preview, title: `${x.position}. ${x.title || x.slug}`, sub: `v${x.version}`,
            href: `#folien?p=${encodeURIComponent(r.project)}` })), all.filter((x) => x.preview).indexOf(s), 320, 180) : null,
          el('figcaption.small', `${s.position}. ${s.title || s.slug}`, el('br'), el('span.muted', `geprüft v${s.version}${s.current > s.version ? ` · jetzt v${s.current}` : ''} · ${s.feedback.open} offen`),
            arr(s.reviewed_by).length ? el('span.rv-thumb__done', { title: `durchgesehen von ${s.reviewed_by.join(', ')}` }, `✓ ${s.reviewed_by.join(', ')}`) : null))))),
        card({ title: 'Feedback', accent: 'Feedback', sub: `${fmt.int(r.feedback.open)} Fehler offen, ${fmt.int(r.feedback.suggestions ?? 0)} Anregungen (blockieren nicht), ${fmt.int(r.feedback.decision)} für deine Entscheidung, ${fmt.int(r.feedback.done)} verarbeitet. In einer Claude-Session genügt: „Schau dir das Feedback zu den Folien an und verarbeite es.“` },
          items.length ? slideGroups(items) : emptyNote('Noch kein Feedback.'))));
      ctx.setStatus('ok', 'verbunden');
      poll(['queued', 'running'].includes(r.gemini), r.status !== 'closed' && arr(r.reviewers).length > 0);
    }).catch(fail);
  }
  /** Stand und Befund der Gemini-Prüfung; lädt sich neu, solange sie läuft. */
  function geminiCard(r) {
    const p = r.gemini_progress ?? {};
    const rep = r.gemini_report ?? {};
    const running = ['queued', 'running'].includes(r.gemini);
    const total = p.total ?? r.slides;
    const rerun = el('button.btn', { type: 'button', disabled: running }, 'Fehlende Folien erneut prüfen');
    rerun.addEventListener('click', async () => {
      rerun.disabled = true;
      try { await api(`/api/reviews/${r.id}`, { action: 'rerun' }); ctx.toast('Gemini prüft die fehlenden Folien erneut.'); load(); }
      catch (e) { ctx.toast(`Nicht gestartet: ${e.message}`, 5000); rerun.disabled = false; }
    });
    const tok = p.tokens ? `${fmt.int((p.tokens.prompt ?? 0) + (p.tokens.output ?? 0))} Tokens` : null;
    const slides = arr(rep.slides);
    return card({ title: 'Prüfung durch Gemini', accent: 'Gemini', tools: (p.error || r.gemini === 'failed') ? rerun : null,
      sub: kids(`Modell ${r.gemini_model ?? '–'}`, running ? `prüft: ${fmt.int(p.done ?? 0)} von ${fmt.int(total)} Folien` : `${fmt.int(p.done ?? 0)} von ${fmt.int(total)} Folien geprüft`,
        tok, rep.searches ? `${fmt.int(rep.searches)} Websuchen` : null, p.finished_at ? `fertig ${ts(p.finished_at)}` : null).join(' · ') },
      ...kids(
        running ? el('div.rv-progress', { role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': total, 'aria-valuenow': p.done ?? 0 },
          el('span', { style: { width: `${Math.round(100 * (p.done ?? 0) / Math.max(1, total))}%` } })) : null,
        p.error ? el('p.small.rv-err', p.error) : null,
        rep.deck?.summary ? el('p', el('strong', r.scope === 'deck' ? 'Roter Faden: ' : 'Zusammenhang: '), rep.deck.summary) : null,
        rep.deck?.error ? el('p.small.rv-err', `Deck-Prüfung: ${rep.deck.error}`) : null,
        slides.length ? el('ul.rv-gem', slides.map((s) => el('li',
          el('span.rv-gem__pos', String(s.position ?? '')),
          el('div', s.error ? el('span.rv-err', `nicht geprüft: ${s.error}`) : s.summary ?? '',
            s.skipped ? el('span.small.muted', ` · ${s.skipped}, nicht erneut geprüft`) : null,
            s.mode === 'followup' ? el('span.small', { class: s.clean ? 'small rv-ok' : 'small rv-err' },
              ` · Nachprüfung seit v${s.since_version}: ${fmt.int(s.fixed ?? 0)} behoben, ${fmt.int(s.not_fixed ?? 0)} nicht behoben, ${fmt.int(s.new_errors ?? 0)} neue Fehler`) : null,
            s.verified_before || s.known_numbers ? el('span.small.muted', ` · übernommen: ${fmt.int(s.verified_before ?? 0)} schon verifizierte Quellen/Datenpunkte, ${fmt.int(s.known_numbers ?? 0)} schon belegte Zahlen`) : null,
            !s.error ? el('span.small.muted', ` · ${fmt.int(s.comments ?? 0)} Hinweise, ${fmt.int(s.checks ?? 0)} Quellen-/Datenprüfungen${arr(s.unreachable_sources).length ? ` · ${s.unreachable_sources.length} Quelle(n) nicht abrufbar` : ''}`) : null,
            !s.error && s.numbers && s.mode !== 'followup' ? numbersLine(s) : null)))) : null));
  }

  /** Bilanz der Zahlen einer Folie: belegt, abgeleitet, falsch, nicht belegbar – und fehlende Quellen. */
  function numbersLine(s) {
    const n = s.numbers;
    const total = (n.sourced ?? 0) + (n.derived ?? 0) + (n.wrong ?? 0) + (n.unverified ?? 0);
    const bad = (n.wrong ?? 0) + (n.unverified ?? 0);
    return el('p.small.rv-nums', badge(bad ? `${bad} Zahl${bad === 1 ? '' : 'en'} ohne Beleg oder falsch` : 'alle Zahlen belegt', bad ? 'danger' : 'success'),
      ` ${fmt.int(total)} Zahlen: ${fmt.int(n.sourced ?? 0)} belegt, ${fmt.int(n.derived ?? 0)} abgeleitet, ${fmt.int(n.wrong ?? 0)} falsch, ${fmt.int(n.unverified ?? 0)} nicht belegbar`,
      s.missing_sources ? ` · ${fmt.int(s.missing_sources)} Quelle${s.missing_sources === 1 ? '' : 'n'} fehlen` : '',
      s.claims && (s.claims.doubt || s.claims.wrong) ? ` · Aussagen: ${fmt.int(s.claims.wrong ?? 0)} falsch, ${fmt.int(s.claims.doubt ?? 0)} fraglich` : '');
  }

  /** Solange Gemini prüft, alle 5 s neu laden. */
  // Gemini prüft: alle 5 s; offene Prüfung mit Personen: alle 30 s (Fortschritt und Beiträge kommen über den Sync)
  function poll(running, people = false) { clearTimeout(timer); if (alive && (running || people)) timer = setTimeout(() => refresh(running ? 5000 : 30000), running ? 5000 : 30000); }
  // Nicht neu zeichnen, solange jemand in ein Feld schreibt (sonst verliert das Feld den Fokus und der Text geht verloren)
  function editing() {
    const a = document.activeElement;
    if (a && body.contains(a) && a.matches('input, textarea, select')) return true;
    return [...body.querySelectorAll('input[type=text], textarea')].some((x) => x.value.trim());
  }
  function refresh(ms) {
    if (!alive) return;
    if (editing()) { clearTimeout(timer); timer = setTimeout(() => refresh(ms), ms); return; }
    load();
  }
  function load() {
    clearTimeout(timer);
    if (rid) detail(); else overview();
  }
  load();
  return () => { alive = false; clearTimeout(timer); };
}
