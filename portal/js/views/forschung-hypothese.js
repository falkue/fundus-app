/**
 * Forschung › Hypothese (#forschung?h=<id>): one readable document, no tabs.
 * Head: back link, code and title, the statement, one muted meta line and – when needed – one muted note line
 * (newer data, incomplete plan …). Sections: Ergebnis · Interpretation · Plan vor der Auswertung (with deviations) ·
 * Daten · Verwandt · Technische Details (collapsed: method, scripts, checksum, source, result versions, change log).
 * The frozen plan has no edit controls; changes are recorded as deviations only.
 * Data: GET /api/research/hypotheses/<id>; POST {action: 'interpret' | 'deviate'}.
 */
import { el } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { arr, kids, api, put, VERDICT, STATUS, KIND, DIRECTION, PREREG_FIELDS, TIMING, md, linkify, refHref, when, day, num,
  seriesLabel, findHref, normHyp } from './forschung-lib.js';
import { intervalPlot } from './forschung-svg.js';
import { interpretationSection, relatedList, logList, defs } from './forschung-teile.js';
import { backLink } from './forschung-finding.js';

export function render(root, ctx, id, alive) {
  const doc = el('article.fo-doc');
  root.append(doc);
  let h = null;

  async function load() {
    put(doc, el('p.fo-muted', 'Lade …'));
    try {
      h = normHyp(await api(`/hypotheses/${encodeURIComponent(id)}`));
      if (!alive()) return;
      draw();
      document.title = `${h.code ?? 'Hypothese'} · Forschung · Fundus-Portal`;
    } catch (e) { put(doc, backLink(), el('p', e.status === 404 ? `Hypothese ${id} gibt es nicht.` : `Nicht ladbar: ${e.message}`)); }
  }

  function draw() {
    const notes = noteLine(h);
    put(doc,
      backLink(),
      el('header.fo-doc__head',
        el('p.fo-doc__code', h.code ?? 'Hypothese'),
        el('h1', h.title || h.statement || h.code),
        h.title && h.statement && h.statement !== h.title ? el('p.fo-doc__lead', ...linkify(h.statement)) : null,
        el('p.fo-muted', kids(h.series ? seriesLabel(h.series) : 'ohne Serie', KIND[h.kind], h.visibility === 'internal' ? 'intern' : h.visibility === 'public' ? 'öffentlich' : null,
          h.status && h.status !== 'result' ? STATUS[h.status] : null, h.created_at ? `erfasst ${day(h.created_at)}` : null).join(' · ')),
        notes ? el('p.fo-note', notes) : null,
        h.derived_from_finding ? el('p.fo-muted', 'Entstanden aus ', el('a', { href: findHref(h.derived_from_finding.id ?? h.derived_from_finding) }, h.derived_from_finding.code ?? 'einem Finding'), '.') : null),
      section('Ergebnis', resultBody()),
      section('Interpretation', h.result ? interpretationSection({ interp: h.interpretation, ctx, verdict: h.result.verdict, readOnly: h.status === 'withdrawn',
        save: async (sections) => {
          const r = await api(`/hypotheses/${h.id}`, { action: 'interpret', interpretation: sections, result_id: h.result.id ?? null, author: 'Falk (Portal)' });
          if (r?.id) { h = normHyp(r); return h.interpretation; }
          return null;
        } }) : el('p.fo-muted', 'Folgt, sobald ein Ergebnis vorliegt.')),
      section('Plan vor der Auswertung', planBody()),
      section('Daten', dataBody()),
      section('Verwandt', relatedList({ links: h.links, theories: h.theories, extra: slides() })),
      technical());
  }
  const section = (title, body) => el('section.fo-sec', el('h2', title), body);

  // ---------------------------------------------------------------- Ergebnis
  function resultBody() {
    const r = h.result;
    if (!r) return el('p.fo-muted', h.status === 'withdrawn' ? `Zurückgezogen, bevor es ein Ergebnis gab${h.withdrawn_reason ? `: ${h.withdrawn_reason}` : '.'}` : 'Noch kein Ergebnis.');
    const sampleOnly = r.sample_run || r.sample;
    const hasCI = Number.isFinite(r.estimate) && Number.isFinite(r.ci_low) && Number.isFinite(r.ci_high);
    const rows = arr(r.estimates).length ? arr(r.estimates) : hasCI ? [{ label: 'Schätzung', estimate: r.estimate, ci_low: r.ci_low, ci_high: r.ci_high }] : [];
    return el('div.fo-result',
      el('p.fo-result__verdict', el('strong', capital(VERDICT[r.verdict] ?? r.verdict)), sampleOnly ? ' (bisher nur ein Probelauf)' : '', '.'),
      r.effect ? el('p', ...linkify(r.effect)) : null,
      defs([['Stichprobe', r.n_text ?? (r.n ? fmt.int(r.n) : null)],
        ['Intervall', hasCI ? `${num(r.ci_low)} bis ${num(r.ci_high)} (95 %)${r.unit ? `, ${r.unit}` : ''}` : r.ci_text ?? null],
        ['Vorbehalte', r.caveats ?? null], ['Ergebnis vom', r.at ? day(r.at) : null]]),
      rows.length ? intervalPlot(rows) : null);
  }

  // ---------------------------------------------------------------- Plan vor der Auswertung
  function planBody() {
    const pre = h.prereg;
    if (!pre) return el('p.fo-muted', h.result ? 'Kein Plan vor der Auswertung festgehalten – das Ergebnis wurde ohne Plan abgelegt.' : 'Noch kein Plan festgelegt.');
    const val = (k) => {
      const v = pre[k];
      if (k === 'direction') return DIRECTION[v] ?? v;
      if (k === 'data' && Array.isArray(v)) return v.length ? v.map((x) => (typeof x === 'string' ? x : x.ref)).join(', ') : null;
      return v;
    };
    const rows = PREREG_FIELDS.filter(([k]) => k !== 'statement' && k !== 'data').map(([k, label]) => [label, val(k)]);
    const devs = arr(h.deviations).slice().sort((a, b) => String(a.at).localeCompare(String(b.at)));
    const label = (k) => PREREG_FIELDS.find(([f]) => f === k)?.[1] ?? k;
    const devBox = el('div.fo-devs',
      devs.length ? el('h3', 'Abweichungen') : null,
      devs.length ? el('ul.fo-list', devs.map((d) => el('li', el('span.fo-muted', `${day(d.at)} · ${label(d.field)} · ${TIMING[d.timing] ?? ''} `), el('br'),
        el('span', d.after ?? ''), d.reason ? el('span.fo-muted', ` – ${d.reason}`) : null))) : null);
    const frozen = !!pre.frozen_at;
    const addB = frozen && h.status !== 'withdrawn' ? el('button.btn', { type: 'button' }, 'Abweichung festhalten') : null;
    addB?.addEventListener('click', () => { addB.replaceWith(deviationForm(() => draw())); });
    return el('div',
      defs(rows),
      el('p.fo-muted', frozen ? `Festgelegt am ${when(pre.frozen_at)} – danach nicht mehr änderbar.` : 'Entwurf – noch nicht festgelegt.'),
      devBox, addB);
  }

  function deviationForm(done) {
    const pre = h.prereg ?? {};
    const fields = PREREG_FIELDS.filter(([k]) => k !== 'statement');
    const sel = el('select.input', { 'aria-label': 'Was ändert sich?' }, fields.map(([k, l]) => el('option', { value: k }, l)));
    sel.value = 'measures';
    const latest = (k) => { const ds = arr(h.deviations).filter((d) => d.field === k); return ds.length ? ds[ds.length - 1].after : (k === 'data' ? arr(pre.data).map((x) => x.ref ?? x).join(', ') : pre[k]); };
    const after = el('textarea.input', { rows: 2, 'aria-label': 'Neu', placeholder: 'Was gilt jetzt?' });
    const reason = el('textarea.input', { rows: 2, 'aria-label': 'Grund', placeholder: 'Warum?' });
    const timing = el('select.input', { 'aria-label': 'Wann?' }, Object.entries(TIMING).map(([v, l]) => el('option', { value: v }, l)));
    const msg = el('p.fo-muted', 'Der Plan selbst bleibt unverändert; die Abweichung wird mit Datum angehängt.');
    const ok = el('button.btn.btn--primary', { type: 'button' }, 'Festhalten');
    const cancel = el('button.btn', { type: 'button', on: { click: done } }, 'Abbrechen');
    ok.addEventListener('click', async () => {
      if (!after.value.trim() || !reason.value.trim()) { msg.textContent = 'Bitte „Neu“ und „Grund“ ausfüllen.'; return; }
      ok.disabled = true;
      try {
        const r = await api(`/hypotheses/${h.id}`, { action: 'deviate', field: sel.value, before: String(latest(sel.value) ?? ''), after: after.value.trim(), reason: reason.value.trim(), timing: timing.value, author: 'Falk (Portal)' });
        ctx.toast('Abweichung festgehalten.');
        if (r?.id) h = normHyp(r); else { await load(); return; }
        done();
      } catch (e) { ok.disabled = false; msg.textContent = `Nicht gespeichert: ${e.message}`; }
    });
    const row = (l, c) => el('label.fo-form__row', el('span', l), c);
    return el('div.fo-form', row('Was ändert sich?', sel), row('Neu', after), row('Grund', reason), row('Wann?', timing), msg, el('div.fo-form__bar', ok, cancel));
  }

  // ---------------------------------------------------------------- Daten, Folien
  function dataBody() {
    const inputs = arr(h.result?.inputs ?? h.prereg?.data).map((x) => (typeof x === 'string' ? { ref: x } : x)).filter((x) => x.ref);
    if (!inputs.length) return el('p.fo-muted', typeof h.prereg?.data === 'string' ? h.prereg.data : 'Keine Datensätze angegeben.');
    return el('ul.fo-list', inputs.map((x) => el('li', refHref(x.ref) ? el('a', { href: refHref(x.ref) }, x.title ?? x.ref) : el('span', x.title ?? x.ref),
      x.version || x.stale ? el('span.fo-muted', ` · ${kids(x.version ? `verwendet: Version ${x.version}` : null, x.stale ? `inzwischen Version ${x.current_version}` : null).join(', ')}`) : null)));
  }
  function slides() {
    return arr(h.result?.outputs).filter((o) => o.kind === 'exhibit').map((o) => el('li', el('span.fo-muted', 'Folie '),
      el('a', { href: `#folien?p=${encodeURIComponent(o.project ?? String(o.ref).split('/')[0])}` }, o.title ?? o.ref)));
  }

  // ---------------------------------------------------------------- Technische Details
  function technical() {
    const r = h.result;
    const pre = h.prereg;
    const rs = arr(h.results);
    return el('details.fo-tech', el('summary', 'Technische Details'),
      el('div.fo-tech__body',
        r?.method_doc ? el('div', el('h3', 'Methode'), md(r.method_doc)) : pre?.method ? el('div', el('h3', 'Methode'), el('p', pre.method)) : null,
        r?.robustness ? el('div', el('h3', 'Robustheit'), md(r.robustness)) : null,
        defs([
          ['Skripte', arr(r?.scripts).length ? el('ul.fo-list', arr(r.scripts).map((s) => el('li', el('code', String(s.path ?? s).split('/').pop()), s.sha256 ? el('span.fo-muted', ` · SHA-256 ${s.sha256}`) : null))) : null],
          ['Laufprotokoll', r?.run_log ?? null], ['Umgebung', r?.environment ?? null],
          ['Prüfsumme des Plans', pre?.sha256 ? `SHA-256 ${pre.sha256}${pre.sha_ok === false ? ' (stimmt nicht mit der gespeicherten Fassung überein)' : ''}` : null],
          ['Quelle', kids(h.source, pre?.source && pre.source !== h.source ? pre.source : null).join(' · ') || null],
          ['Ergebnisse', rs.length > 1 ? el('ul.fo-list', rs.map((x) => el('li', `${day(x.at)} · ${VERDICT[x.verdict] ?? x.verdict}`, x.sample_run || x.sample ? el('span.fo-muted', ' · Probelauf') : null,
            x === h.result ? el('span.fo-muted', ' · gilt') : null))) : null],
          ['Kennung', h.ref ?? `hypothesis:${h.id}`]]),
        el('div', el('h3', 'Verlauf'), logList(h.log))));
  }

  load();
}

const capital = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

/** One muted line with the integrity notes in plain words (no boxes). */
function noteLine(h) {
  const out = [];
  for (const f of arr(h.flags)) {
    if (f.flag === 'data_newer_than_result') {
      const d = arr(f.detail);
      out.push(d.length === 1 ? `Neuere Daten verfügbar – Ergebnis stammt von Version ${d[0].used} (${d[0].ref})` : d.length ? `Neuere Daten verfügbar für ${d.length} Datensätze` : 'Neuere Daten verfügbar');
    } else if (f.flag === 'prereg_incomplete') {
      const d = arr(f.detail).map((k) => PREREG_FIELDS.find(([x]) => x === k)?.[1] ?? k);
      out.push(`Plan unvollständig${d.length ? ` (${d.join(', ')} fehlt)` : ''}`);
    } else if (f.flag === 'prereg_missing') out.push('Ohne Plan vor der Auswertung abgelegt');
    else if (f.flag === 'sample_only') out.push('Bisher nur ein Probelauf');
    else if (f.flag === 'prereg_hash_mismatch') out.push('Plan weicht von der festgelegten Fassung ab');
    else if (f.flag === 'deviation_after_data') out.push('Abweichung nach Blick in die Daten');
    else if (f.flag === 'prereg_after_result') out.push('Plan ist später datiert als das Ergebnis');
    else if (f.flag === 'prereg_time_unknown') out.push('Zeitpunkt des Plans unbekannt');
  }
  return out.join(' · ');
}
