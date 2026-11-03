/**
 * Forschung › Hypothese (#forschung?h=<id>&t=<reiter>): header (code, title, statement, kind, status, visibility, series,
 * verdict, frozen badge, integrity flags) and tabs: Präregistrierung (frozen, all fields, deviations as a timeline,
 * append-only form) · Ergebnis (verdict, forest plot, n, robustness, caveats, earlier versions) · Interpretation
 * (seven-section editor) · Methode · Daten & Herkunft (graph) · Verbindungen (links, theory cards) · Verlauf.
 * The frozen pre-registration has no edit controls anywhere – changes are deviations only.
 * Data: GET /api/research/hypotheses/<id>; POST {action: 'interpret' | 'deviate'}.
 */
import { el, svgEl, card, emptyNote, segmented, badge, dl } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { arr, kids, hp, api, put, VERDICT, DIRECTION, PREREG_FIELDS, TIMING, FLAG, interpState, vpill, statusChip, kindChip,
  visChip, interpChip, md, linkify, refLink, refHref, shortHash, when, day, num, seriesLabel, findHref, normHyp, flagText } from './forschung-lib.js';
import { forest, provenance } from './forschung-svg.js';
import { interpretationEditor, linksCard, theoryCards, logList, evidenceList, facts } from './forschung-teile.js';
import { copyText } from './daten-lib.js';

const TABS = [
  { value: 'prereg', label: 'Präregistrierung' }, { value: 'ergebnis', label: 'Ergebnis' }, { value: 'interpretation', label: 'Interpretation' },
  { value: 'methode', label: 'Methode' }, { value: 'herkunft', label: 'Daten & Herkunft' }, { value: 'verbindungen', label: 'Verbindungen' },
  { value: 'verlauf', label: 'Verlauf' }];

export function render(root, ctx, id, alive) {
  const back = el('a.dk-back', { href: '#forschung?tab=hypothesen' }, '← Hypothesen');
  const head = el('header.fo-head');
  const tabsBox = el('div.dk-dtabs');
  const body = el('div.fo-tabbody');
  root.append(el('p', back), head, tabsBox, body);
  let h = null;
  let tab = hp().get('t');
  let resultIdx = 0;          // which result version the Ergebnis tab shows
  const built = new Map();

  async function load() {
    put(head, el('div.dk-skel', el('div.dk-skel__card')));
    try {
      h = await api(`/hypotheses/${encodeURIComponent(id)}`);
      if (!alive()) return;
      normHyp(h);
      resultIdx = Math.max(0, h.results.indexOf(h.result));
      if (!TABS.some((t) => t.value === tab)) tab = h.result ? 'ergebnis' : 'prereg';
      built.clear();
      draw();
      document.title = `${h.code ?? 'Hypothese'} · Forschung · Fundus-Portal`;
    } catch (e) { put(head, el('div.alert.alert--error', e.status === 404 ? `Hypothese ${id} gibt es nicht.` : `Nicht ladbar: ${e.message}`)); }
  }

  function draw() {
    drawHead();
    const ist = interpState(h.interpretation);
    const seg = segmented(TABS.map((t) => ({ ...t, count: t.value === 'interpretation' && h.result && !ist.complete ? (ist.filled ? `${ist.done}/${ist.need}` : 'fehlt')
      : t.value === 'prereg' && arr(h.deviations).length ? String(arr(h.deviations).length) : undefined })), tab, showTab, 'Reiter');
    seg.el.classList.add('dk-dtabs__seg');
    seg.el.querySelectorAll('.seg__btn').forEach((b, i) => { if (TABS[i].value === 'prereg' && arr(h.deviations).length) b.title = `${arr(h.deviations).length} Abweichungen`; });
    put(tabsBox, seg.el);
    showTab(tab);
  }

  function showTab(v) {
    tab = v;
    const p = hp(); p.set('h', id);
    if (v === (h.result ? 'ergebnis' : 'prereg')) p.delete('t'); else p.set('t', v);
    const s = `#forschung?${p}`;
    if (location.hash !== s) history.replaceState(null, '', s);
    if (!built.has(v)) {
      try {
        built.set(v, { prereg: preregTab, ergebnis: resultTab, interpretation: interpTab, methode: methodTab, herkunft: provenanceTab,
          verbindungen: linksTab, verlauf: logTab }[v]());
      } catch (e) { built.set(v, el('div.alert.alert--error', `Reiter nicht darstellbar: ${e.message}`)); }
    }
    put(body, built.get(v));
  }

  // ---------------------------------------------------------------- header
  function drawHead() {
    const r = h.result;
    const pre = h.prereg;
    const flags = arr(h.flags).filter((f) => f.flag !== 'interpretation_missing');
    const ist = interpState(h.interpretation);
    put(head,
      el('div.fo-head__top', el('span.label', kids('Hypothese', h.code, seriesLabel(h.series)).join(' · ')),
        el('div.row.fo-head__chips', kindChip(h.kind), statusChip(h.status), visChip(h.visibility))),
      el('h1.fo-head__title', h.code ? el('span.fo-head__code', h.code) : null, h.title),
      h.statement && h.statement !== h.title ? el('p.fo-head__stmt', h.statement) : null,
      el('div.fo-head__row',
        r ? vpill(r.verdict, { big: true }) : vpill(null, { none: h.status === 'withdrawn' ? 'zurückgezogen, ohne Ergebnis' : 'noch kein Ergebnis' }),
        pre?.frozen_at ? frozenBadge(pre) : h.status !== 'withdrawn' ? el('span.fo-unfrozen', 'noch nicht präregistriert') : null,
        r ? el('button.fo-ipbtn', { type: 'button', on: { click: () => showTabAndSeg('interpretation') } }, interpChip(ist.state)) : null),
      h.status === 'withdrawn' ? el('div.alert.fo-alert', el('strong', 'Zurückgezogen'), el('span', h.withdrawn_reason ? ` – ${h.withdrawn_reason}` : '', ' Die Hypothese bleibt mit allen Angaben sichtbar.')) : null,
      h.derived_from_finding ? el('p.small.fo-head__from', 'Entstanden aus ', refLink(`finding:${h.derived_from_finding.id ?? h.derived_from_finding}`, h.derived_from_finding.code ? `Finding ${h.derived_from_finding.code}` : `Finding ${h.derived_from_finding}`),
        ' – das Finding bleibt explorativ, diese Hypothese prüft es.') : null,
      flags.length ? el('div.fo-flags', flags.map((f) => el('div.alert.fo-alert', el('strong', f.label ?? FLAG[f.flag] ?? f.flag), flagText(f) ? el('span', ' – ', ...linkify(flagText(f))) : null,
        f.flag === 'data_newer_than_result' ? el('span', ' Ein neuer Lauf würde ein neues Ergebnis als weitere Version anlegen; das bisherige bleibt.') : null))) : null,
      el('dl.fo-facts.fo-head__facts', kids(
        ['Richtung', DIRECTION[h.direction] ?? h.direction ?? '–'],
        ['Serie', h.series ? el('a', { href: `#forschung?tab=hypothesen&series=${encodeURIComponent(h.series.slug ?? '')}` }, h.series.title ?? seriesLabel(h.series)) : 'ohne Serie'],
        ['Projekt', h.project || '–'],
        ['Erfasst', `${day(h.created_at)}${h.created_by ? ` · ${h.created_by}` : ''}`],
        r ? ['Ergebnis vom', `${day(r.at)}${arr(h.results).length > 1 ? ` · Version ${r.version ?? arr(h.results).length} von ${arr(h.results).length}` : ''}`] : null,
        r?.n ? ['Fallzahl', fmt.int(r.n)] : null).map(([k, v]) => el('div', el('dt', k), el('dd', v)))));
  }
  function showTabAndSeg(v) { tab = v; draw(); body.scrollIntoView({ block: 'start', behavior: 'smooth' }); }

  function frozenBadge(pre) {
    const b = el('button.fo-frozen', { type: 'button', title: `Prüfsumme (SHA-256): ${pre.sha256 ?? '–'} – Klick kopiert sie` },
      lockIcon(),
      el('span', `eingefroren am ${when(pre.frozen_at)}`), el('span.fo-frozen__hash', `Prüfsumme ${shortHash(pre.sha256)}`));
    b.addEventListener('click', async () => { if (pre.sha256) ctx.toast(await copyText(pre.sha256) ? 'Prüfsumme kopiert.' : pre.sha256); });
    return b;
  }

  // ---------------------------------------------------------------- Präregistrierung
  function preregTab() {
    const pre = h.prereg;
    if (!pre) {
      return el('div.fo-stack', card({ title: 'Noch nicht präregistriert', accent: 'präregistriert' },
        h.result ? el('p', el('strong', 'Ohne Präregistrierung abgelegt. '), 'Das Ergebnis wurde importiert, ohne dass eine Präregistrierung vorlag – es zählt deshalb nicht als präregistrierter Test.') : null,
        el('p', 'Diese Hypothese ist erfasst, aber noch nicht eingefroren. Präregistriert wird aus einer Claude-Session mit ', el('code', 'fundus_hypothesis(action="prereg")'),
          ' – vor jeder Schätzung oder Analyse. Danach sind Aussage, Richtung, Messung, Methode und Entscheidungsregel unveränderlich.'),
        h.status === 'idea' ? el('p.small.muted', `Idee seit ${day(h.created_at)}${ageDays(h.created_at) > 14 ? ` – seit ${ageDays(h.created_at)} Tagen ohne Präregistrierung (Arbeitsliste)` : ''}.`) : null));
    }
    const src = String(pre.source ?? '');
    const imported = src.startsWith('imported');
    const fields = PREREG_FIELDS.map(([k, label]) => {
      let v = pre[k];
      if (k === 'direction') v = DIRECTION[v] ?? v;
      if (k === 'data' && typeof v !== 'string') v = arr(v).length ? el('ul.fo-plain', arr(v).map((x) => el('li', typeof x === 'string' ? refLink(x) : refLink(x.ref, `${x.ref}${x.version ? ` v${x.version}` : ''}`), x.title ? ` ${x.title}` : '', x.note ? el('span.small.muted', ` – ${x.note}`) : null))) : null;
      const devs = arr(h.deviations).filter((d) => d.field === k);
      return el('div.fo-pre__f', { class: `fo-pre__f${devs.length ? ' has-dev' : ''}` }, el('dt', label, devs.length ? el('span.fo-pre__devn', { title: 'Für dieses Feld gibt es Abweichungen (siehe unten)' }, `${devs.length} Abweichung${devs.length > 1 ? 'en' : ''}`) : null),
        el('dd', v === null || v === undefined || v === '' ? el('span.muted', '–') : (typeof v === 'string' ? el('span', ...linkify(v)) : v)));
    });
    const timeNote = { stamp: 'Zeitpunkt aus der Stempeldatei übernommen.', heading: 'Zeitpunkt aus der Überschrift der Präregistrierung übernommen.', unknown: 'Zeitpunkt unbekannt.' }[pre.time_source] ?? '';
    const frozen = !!pre.frozen_at;
    const sheet = el('section.fo-pre', { class: `fo-pre${frozen ? '' : ' is-draft'}`, 'aria-label': frozen ? 'Eingefrorene Präregistrierung' : 'Entwurf der Präregistrierung' },
      frozen ? el('div.fo-pre__seal', lockIcon(), el('div', el('strong', `Eingefroren am ${when(pre.frozen_at)}`),
        el('div.small', 'Prüfsumme (SHA-256) über die kanonische Fassung: ', el('code.fo-pre__hash', pre.sha256 ?? '–'),
          pre.sha_ok === false ? el('strong.fo-pre__bad', ' – stimmt nicht mit der gespeicherten Fassung überein') : pre.sha_ok === true ? el('span.muted', ' (nachgerechnet, stimmt)') : null),
        el('div.small.muted', imported ? `Importiert aus ${src.replace(/^imported:/, '')}. ${timeNote}` : 'In Fundus registriert.',
          ' Unveränderlich; Änderungen nur als Abweichung.')))
        : el('div.fo-pre__seal.fo-pre__seal--draft', el('div', el('strong', 'Entwurf – noch nicht eingefroren'), el('div.small.muted', 'Eingefroren wird aus einer Claude-Session mit fundus_hypothesis(action="prereg"), vor jeder Analyse.'))),
      arr(pre.missing).length ? el('p.fo-pre__missing.small', el('strong', 'Unvollständig: '), arr(pre.missing).map((k) => PREREG_FIELDS.find(([f]) => f === k)?.[1] ?? k).join(', '), ' fehlt in der importierten Fassung.') : null,
      pre.theory ? el('p.fo-pre__theory', el('span.label', 'Theorieanker'), ' ', ...linkify(pre.theory)) : null,
      el('dl.fo-pre__fields', fields));
    return el('div.fo-stack', sheet, deviationsCard());
  }

  function deviationsCard() {
    const devs = arr(h.deviations).slice().sort((a, b) => String(a.at).localeCompare(String(b.at)));
    const label = (k) => PREREG_FIELDS.find(([f]) => f === k)?.[1] ?? k;
    const resultAt = h.result?.at;
    const timeline = devs.length ? el('ol.fo-dev', devs.map((d) => el('li', { class: `fo-dev__item fo-dev--${d.timing ?? 'unknown'}` },
      el('div.fo-dev__head', el('span.fo-dev__ts', when(d.at)), el('strong', label(d.field)),
        el('span.fo-dev__timing', { class: `fo-dev__timing fo-dev__timing--${d.timing}` }, TIMING[d.timing] ?? 'Zeitpunkt unbekannt'),
        resultAt && String(d.at) > String(resultAt) ? el('span.fo-dev__after', 'nach dem Ergebnis') : null, el('span.small.muted', d.by ?? '')),
      el('div.fo-dev__diff', el('div.fo-dev__before', el('span.label', 'vorher'), el('p', d.before || '–')), el('div.fo-dev__after-v', el('span.label', 'nachher'), el('p', d.after || '–'))),
      d.reason ? el('p.fo-dev__why', el('span.label', 'Grund'), ' ', ...linkify(d.reason)) : null)))
      : el('p.small.muted', 'Keine Abweichungen vom Plan.');
    const addB = h.status !== 'withdrawn' ? el('button.btn', { type: 'button' }, 'Abweichung erfassen …') : null;
    const form = el('div.fo-devform', { hidden: true });
    addB?.addEventListener('click', () => { form.hidden = !form.hidden; if (!form.hidden) { put(form, deviationForm(() => { form.hidden = true; })); form.querySelector('select')?.focus(); } });
    return card({ title: 'Abweichungen vom Plan', accent: 'Abweichungen', tools: addB,
      sub: 'Wird nur angehängt, nie überschrieben: was, warum, wann – und ob es vor der Analyse oder nach Sicht auf die Daten geschah.' }, timeline, form);
  }

  function deviationForm(done) {
    const pre = h.prereg ?? {};
    const sel = el('select.input', { 'aria-label': 'Feld' }, PREREG_FIELDS.filter(([k]) => k !== 'statement' || true).map(([k, l]) => el('option', { value: k }, l)));
    sel.value = 'measures';
    const before = el('textarea.input', { rows: 2, readonly: true, 'aria-label': 'Bisher (aus der Präregistrierung)' });
    const latest = (k) => { const ds = arr(h.deviations).filter((d) => d.field === k); return ds.length ? ds[ds.length - 1].after : (k === 'data' ? arr(pre.data).map((x) => x.ref ?? x).join(', ') : pre[k]); };
    const setBefore = () => { before.value = String(latest(sel.value) ?? ''); };
    setBefore();
    sel.addEventListener('change', setBefore);
    const after = el('textarea.input', { rows: 2, 'aria-label': 'Neu', placeholder: 'Was gilt jetzt?' });
    const reason = el('textarea.input', { rows: 2, 'aria-label': 'Grund', placeholder: 'Warum? (z. B. „Variable fehlt in der neuen Ausgabe“)' });
    let timing = 'before_analysis';
    const tseg = segmented([{ value: 'before_analysis', label: 'vor der Analyse' }, { value: 'after_data_seen', label: 'nach Sicht auf die Daten' }], timing, (v) => { timing = v; }, 'Zeitpunkt');
    const msg = el('p.small.muted', 'Die Präregistrierung selbst bleibt unverändert; die Abweichung wird mit Zeitpunkt angehängt und lässt sich nicht löschen.');
    const ok = el('button.btn.btn--primary', { type: 'button' }, 'Abweichung anhängen');
    const cancel = el('button.btn', { type: 'button', on: { click: done } }, 'Abbrechen');
    ok.addEventListener('click', async () => {
      if (!after.value.trim() || !reason.value.trim()) { msg.textContent = 'Bitte „Neu“ und „Grund“ ausfüllen.'; return; }
      ok.disabled = true;
      try {
        const r = await api(`/hypotheses/${h.id}`, { action: 'deviate', field: sel.value, before: before.value, after: after.value.trim(), reason: reason.value.trim(), timing });
        ctx.toast('Abweichung angehängt.');
        if (r && r.id) { h = normHyp(r); } else await load();
        built.clear(); draw();
      } catch (e) { ok.disabled = false; msg.textContent = `Nicht gespeichert: ${e.message}`; }
    });
    return el('div.fo-devform__grid',
      el('label.ds-row', el('span.label', 'Feld'), sel),
      el('div.ds-row', el('span.label', 'Zeitpunkt'), tseg.el),
      el('label.ds-row', el('span.label', 'Bisher'), before),
      el('label.ds-row', el('span.label', 'Neu'), after),
      el('label.ds-row.fo-devform__wide', el('span.label', 'Grund'), reason),
      el('div.fo-devform__wide', msg, el('div.row', cancel, ok)));
  }

  // ---------------------------------------------------------------- Ergebnis
  function resultTab() {
    const rs = arr(h.results);
    if (!rs.length) return el('div.fo-stack', emptyNote(h.status === 'withdrawn' ? 'Zurückgezogen, bevor es ein Ergebnis gab.' : 'Noch kein Ergebnis. Es wird aus der Session mit fundus_hypothesis(action="result") abgelegt – jedes Urteil, auch nicht gestützt oder unentschieden.'));
    const wrap = el('div.fo-stack');
    const drawRes = () => {
      const r = rs[resultIdx];
      const rows = arr(r.estimates).length ? arr(r.estimates) : (Number.isFinite(r.estimate) ? [{ label: 'Hauptmodell', estimate: r.estimate, ci_low: r.ci_low, ci_high: r.ci_high }] : []);
      const verdictCard = el('section.card.fo-result', { class: `card fo-result fo-v--${r.verdict}` },
        el('div.fo-result__top', el('div', el('span.label', resultIdx === 0 ? 'Aktuelles Ergebnis' : `Frühere Version ${r.version ?? ''}`), vpill(r.verdict, { big: true })),
          el('div.fo-result__meta', el('span', when(r.at)), el('span', r.by ?? ''), r.source === 'imported' ? badge('importiert', 'outline') : null, r.sample ? badge('Stichprobenlauf', 'outline') : null)),
        r.effect ? el('p.fo-result__effect', ...linkify(r.effect)) : null,
        h.prereg?.decision_rule ? el('div.fo-result__rule', el('span.label', 'Entscheidungsregel (präregistriert)'), el('p', ...linkify(h.prereg.decision_rule))) : null,
        rows.length ? el('div.fo-result__plot', forest(rows, { verdict: r.verdict, direction: h.prereg?.direction ?? h.direction, unit: r.unit })) : el('p.small.muted', 'Keine Grafik: Schätzer und Intervall sind nicht als Zahlen abgelegt, nur im Text des Effekts.'),
        facts([
          ['Fallzahl', r.n ? fmt.int(r.n) : null], ['Stichprobe', r.n_text ?? null], ['Schätzer', Number.isFinite(r.estimate) ? `${num(r.estimate)}${r.unit ? ` ${r.unit}` : ''}` : null],
          ['95-%-Intervall', Number.isFinite(r.ci_low) ? `${num(r.ci_low)} bis ${num(r.ci_high)}` : null]].filter(([, v]) => v)));
      const more = el('div.fo-two',
        card({ title: 'Robustheit', accent: 'Robustheit' }, r.robustness ? md(r.robustness) : el('p.small.muted', 'Keine Angaben.')),
        card({ title: 'Vorbehalte', accent: 'Vorbehalte', sub: 'Identifikation, Daten, Messung' }, r.caveats ? md(r.caveats) : el('p.small.muted', 'Keine Angaben.')));
      const mEntries = Object.entries(r.measures ?? {}).filter(([, v]) => v !== null && v !== '' && !(typeof v === 'object' && !Object.keys(v).length));
      const measures = mEntries.length ? card({ title: 'Kennzahlen', accent: 'Kennzahlen' },
        dl(mEntries.map(([k, v]) => [k, typeof v === 'number' ? num(v) : typeof v === 'object' ? JSON.stringify(v) : String(v)]))) : null;
      const versions = rs.length > 1 ? card({ title: 'Versionen des Ergebnisses', accent: 'Versionen', sub: 'Ein neuer Lauf legt eine neue Version an; die neueste gilt. Alle bleiben sichtbar.' },
        el('ol.fo-rvers', rs.map((x, i) => el('li', { class: i === resultIdx ? 'is-on' : null },
          el('button.btn.btn--ghost.btn--small', { type: 'button', 'aria-pressed': String(i === resultIdx), on: { click: () => { resultIdx = i; drawRes(); } } }, `Version ${x.version ?? rs.length - i}`),
          vpill(x.verdict), el('span.small.muted', ` ${when(x.at)}${x.sample ? ' · Stichprobenlauf' : ''}${i === 0 ? ' · aktuell' : ''}`))))) : null;
      put(wrap, verdictCard, more, measures, versions);
    };
    drawRes();
    return wrap;
  }

  // ---------------------------------------------------------------- Interpretation
  function interpTab() {
    if (!h.result) {
      return el('div.fo-stack', el('div.alert.alert--info', 'Die Interpretation gehört zu einem Ergebnis. Sobald ein Ergebnis vorliegt, steht sie hier – mit sieben Teilen, vier davon Pflicht.'));
    }
    return el('div.fo-stack', interpretationEditor({ interp: h.interpretation, history: h.interpretations ?? (h.interpretation ? [h.interpretation] : []), verdict: h.result.verdict, ctx,
      readOnly: h.status === 'withdrawn',
      save: async (sections) => {
        const r = await api(`/hypotheses/${h.id}`, { action: 'interpret', interpretation: sections, sections, result_id: h.result.id ?? null, author: 'Falk (Portal)' });
        if (r && r.id) { h = normHyp(r); resultIdx = Math.max(0, h.results.indexOf(h.result)); built.delete('verlauf'); built.delete('ergebnis'); drawHead(); drawTabsOnly(); return h.interpretation; }
        return null;
      } }));
  }
  function drawTabsOnly() {
    const ist = interpState(h.interpretation);
    const btn = tabsBox.querySelectorAll('.seg__btn')[2];
    const c = btn?.querySelector('.count');
    if (c) { if (ist.complete) c.remove(); else c.textContent = ist.filled ? `${ist.done}/${ist.need}` : 'fehlt'; }
  }

  // ---------------------------------------------------------------- Methode
  function methodTab() {
    const r = h.result;
    if (!r) return el('div.fo-stack', card({ title: 'Geplante Methode', accent: 'Methode' }, h.prereg?.method ? el('p', ...linkify(h.prereg.method)) : el('p.small.muted', 'Noch keine Methode.')));
    return el('div.fo-stack',
      card({ title: 'Methode', accent: 'Methode', sub: 'Modellgleichung, Spezifikation, Schätzung, Diagnostik' }, r.method_doc ? md(r.method_doc) : el('p.small.muted', 'Keine Methodenbeschreibung abgelegt.')),
      card({ title: 'Skripte', accent: 'Skripte', sub: 'Mit Prüfsumme der Fassung, die das Ergebnis erzeugt hat.' },
        arr(r.scripts).length ? el('ul.fo-scripts', arr(r.scripts).map((s) => el('li', el('code.dk-path', s.path ?? s), s.sha256 ? hashChip(s.sha256) : null))) : el('p.small.muted', 'Keine Skripte angegeben.')),
      card({ title: 'Lauf', accent: 'Lauf' }, facts([['Laufprotokoll', r.run_log ? el('code.dk-path', r.run_log) : null], ['Umgebung', r.environment ?? null], ['Erzeugt von', r.by ?? null], ['Zeitpunkt', when(r.at)]])));
  }
  function hashChip(sha) {
    const b = el('button.fo-hash', { type: 'button', title: `SHA-256 ${sha} – Klick kopiert` }, shortHash(sha));
    b.addEventListener('click', async () => ctx.toast(await copyText(sha) ? 'Prüfsumme kopiert.' : sha));
    return b;
  }

  // ---------------------------------------------------------------- Daten & Herkunft
  function provenanceTab() {
    const r = h.result;
    const inputs = arr(r?.inputs ?? h.prereg?.data).map((x) => (typeof x === 'string' ? { ref: x } : x));
    const staleRefs = new Set(arr(h.flags).filter((f) => f.flag === 'data_newer_than_result').flatMap((f) => flagText(f).match(/dataset:\d+/g) ?? []));
    const cols = {
      inputs: inputs.map((x) => ({ label: x.title ?? x.ref, sub: kids(x.title ? x.ref : null, x.version ? `Version ${x.version}` : null, x.current_version && x.current_version > x.version ? `neu: ${x.current_version}` : null).join(' · '),
        href: refHref(x.ref), stale: staleRefs.has(x.ref) || (x.current_version && x.current_version > x.version) })),
      scripts: arr(r?.scripts).map((s) => ({ label: String(s.path ?? s).split('/').pop(), sub: s.sha256 ? `SHA-256 ${shortHash(s.sha256)}` : null })),
      result: r ? { label: `${h.code ?? 'Ergebnis'}: ${VERDICT[r.verdict] ?? r.verdict}`, sub: `${when(r.at)}${r.n ? ` · Fallzahl ${fmt.int(r.n)}` : ''}`, verdict: r.verdict } : null,
      outputs: arr(r?.outputs).map((o) => ({ label: o.title ?? String(o.ref).split('/').pop(), sub: kids(o.kind === 'exhibit' ? 'Folie' : o.kind === 'dataset' ? 'Datensatz' : 'Datei', o.title ? o.ref : null).join(' · '),
        href: o.kind === 'exhibit' ? `#folien?p=${encodeURIComponent(o.project ?? String(o.ref).split('/')[0])}` : refHref(o.ref) })),
    };
    const list = card({ title: 'Eingangsdaten', accent: 'Eingangsdaten', sub: 'Welche Version jedes Datensatzes das Ergebnis verwendet hat.' },
      inputs.length ? el('ul.fo-plain.fo-inputs', inputs.map((x) => el('li', refLink(x.ref, x.title ? `${x.ref} ${x.title}` : x.ref), x.version ? el('span.small.muted', ` · verwendet: Version ${x.version}`) : null,
        staleRefs.has(x.ref) || (x.current_version > x.version) ? el('span', ' ', badge('neuere Version vorhanden', 'warn')) : null))) : el('p.small.muted', 'Keine Datensätze angegeben.'));
    return el('div.fo-stack',
      card({ title: 'Herkunft des Ergebnisses', accent: 'Herkunft', sub: 'Datensätze → Skripte → Ergebnis → Folien, Datensätze und Dateien. Knoten führen in Datensätze und Folien.' },
        r || inputs.length ? provenance(cols) : el('p.small.muted', 'Noch nichts zu zeigen.')),
      list,
      r?.outputs?.length ? card({ title: 'Ausgaben', accent: 'Ausgaben' }, evidenceList(arr(r.outputs).map((o) => ({ kind: o.kind, ref: o.ref, title: o.title, note: o.note })), { plain: true })) : null);
  }

  // ---------------------------------------------------------------- Verbindungen, Verlauf
  function linksTab() {
    const from = h.derived_from_finding ? el('div.alert.alert--info', el('span', 'Entstanden aus ', el('a', { href: findHref(h.derived_from_finding.id ?? h.derived_from_finding) }, h.derived_from_finding.code ? `Finding ${h.derived_from_finding.code}` : `Finding ${h.derived_from_finding}`),
      '. Das Finding bleibt explorativ – erst diese Hypothese prüft es.')) : null;
    return el('div.fo-stack', from, linksCard(h.links), el('h3.fo-subhead', 'Theorie'), theoryCards(h.theories));
  }
  function logTab() { return el('div.fo-stack', card({ title: 'Verlauf', accent: 'Verlauf', sub: 'Alle Änderungen, neueste zuerst.' }, logList(h.log))); }

  load();
}

function ageDays(v) { const d = fmt.parseDate(String(v ?? '').replace(' ', 'T')); return d ? Math.floor((Date.now() - d.getTime()) / 864e5) : 0; }


export function lockIcon() {
  return svgEl('svg', { class: 'fo-lock', viewBox: '0 0 16 16', 'aria-hidden': 'true', focusable: 'false' },
    svgEl('path', { d: 'M4.5 7V5.2a3.5 3.5 0 0 1 7 0V7M3.5 7h9v6.5h-9z', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.5', 'stroke-linejoin': 'round' }));
}
