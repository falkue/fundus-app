/**
 * Fundus Cloud im Portal (fundus/cloud.py): Folien Ende-zu-Ende-verschlüsselt an das Fundus einer anderen Person senden.
 * Firebase ist nur Postfach und Adressbuch; Google sieht keine Inhalte.
 *
 * openShareDialog: Umfang (Auswahl, Foliensatz, Projekt) → Empfänger (E-Mail, nur registrierte; POST /api/cloud/lookup) →
 * Prüfung auf interne Folien (POST /api/cloud/share {action: check}) mit ausdrücklicher Bestätigung → Senden als Auftrag
 * (POST /api/cloud/share {action: start}, Fortschritt über GET /api/cloud/share?job=…).
 */
import { el, headline } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';

const arr = (v) => (Array.isArray(v) ? v : []);

export async function cloudApi(sub, body) {
  const opt = body === undefined ? { cache: 'no-store' } : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch(`/api/cloud/${sub}`, opt);
  let d = null; try { d = await r.json(); } catch { /* kein JSON */ }
  if (!r.ok || d?.error) throw new Error(d?.error ?? `HTTP ${r.status}`);
  return d;
}

const PHASE = { start: 'Starte …', check: 'Prüfe Empfänger und Folien …', export: 'Packe die Folien …', encrypt: 'Verschlüssele …',
  upload: 'Lade verschlüsselt hoch …', record: 'Vermerke den Versand …', done: 'Fertig' };
const mb = (n) => (n >= 1e6 ? `${fmt.num1(n / 1e6)} MB` : `${fmt.int(Math.max(1, Math.round(n / 1e3)))} kB`);

/** Wörter des Fingerabdrucks als Chips (zum Vorlesen am Telefon). */
export function fpWords(fp) {
  return el('span.cl-fp', { title: 'Fingerabdruck – am Telefon vergleichen' }, arr(String(fp ?? '').split(' ')).filter(Boolean).map((w) => el('span.cl-fp__w', w)));
}

/**
 * opts: { project, projectName, set: {id, name} | null, selection: [ids], ctx }
 * Ergebnis: das Ergebnis des Versands oder null (abgebrochen).
 */
export function openShareDialog(opts) {
  return new Promise((resolve) => {
    const { ctx } = opts;
    const dlg = el('dialog.dialog.rv-dialog.cl-dialog', { 'aria-label': 'Teilen über Fundus Cloud' });
    let result = null;
    const finish = () => { clearInterval(timer); dlg.close(); dlg.remove(); resolve(result); };
    let timer = null;

    const scopes = [
      opts.selection.length ? { v: 'selection', l: opts.selection.length === 1 ? '1 markierte Folie' : `${fmt.int(opts.selection.length)} markierte Folien`,
        refs: opts.selection.map((id) => `exhibit:${id}`) } : null,
      opts.set ? { v: 'set', l: `Foliensatz „${opts.set.name}“`, refs: [`set:${opts.project}/${opts.set.id}`] } : null,
      { v: 'project', l: `ganzes Projekt „${opts.projectName}“`, refs: [`project:${opts.project}`] },
    ].filter(Boolean);
    let scope = scopes[0];
    const scopeBox = el('div.rv-radios', { role: 'radiogroup', 'aria-label': 'Umfang' }, scopes.map((s) => {
      const id = `cl-scope-${s.v}`;
      const r = el('input', { type: 'radio', name: 'cl-scope', id, value: s.v, checked: s === scope });
      r.addEventListener('change', () => { scope = s; resetCheck(); });
      return el('label.rv-radio', { for: id }, r, s.l);
    }));

    // Empfänger
    const recips = [];          // {email, fingerprint, confirmed, key_changed}
    const list = el('ul.rv-people.cl-recips');
    const who = el('input.input', { type: 'email', id: 'cl-who', placeholder: 'E-Mail der Person (mit Fundus-Cloud-Konto)', autocomplete: 'off' });
    const addBtn = el('button.btn.btn--small', { type: 'button' }, 'Hinzufügen');
    const drawRecips = () => list.replaceChildren(...recips.map((p, i) => {
      const rm = el('button.btn.btn--ghost.btn--small', { type: 'button', 'aria-label': `${p.email} entfernen` }, '✕');
      rm.addEventListener('click', () => { recips.splice(i, 1); drawRecips(); });
      return el('li.cl-recip', el('div.cl-recip__main', el('strong', p.email), fpWords(p.fingerprint)),
        el('span', { class: `badge ${p.key_changed ? 'badge--danger' : p.confirmed ? 'badge--ok' : ''}` },
          p.key_changed ? 'Schlüssel geändert' : p.confirmed ? 'bestätigt' : 'unbestätigt'), rm);
    }));
    const addRecip = async () => {
      const v = who.value.trim().toLowerCase();
      if (!v) return;
      if (recips.some((p) => p.email === v)) { who.value = ''; return; }
      addBtn.disabled = true; err.textContent = '';
      try {
        const d = await cloudApi('lookup', { emails: [v] });
        const r = arr(d.recipients)[0] ?? {};
        if (r.self) err.textContent = 'Das ist deine eigene Adresse.';
        else if (!r.registered) err.textContent = r.error ? r.error : `${v} hat kein Fundus-Cloud-Konto. Die Person muss sich zuerst in ihrem Fundus unter „Verwaltung“ verbinden.`;
        else if (r.key_changed) err.textContent = `Achtung: Der Schlüssel von ${v} hat sich seit dem ersten Kontakt geändert. Erst telefonisch prüfen und unter „Verwaltung“ annehmen.`;
        else { recips.push(r); who.value = ''; drawRecips(); }
      } catch (e) { err.textContent = `Nachschlagen fehlgeschlagen: ${e.message}`; }
      addBtn.disabled = false; who.focus();
    };
    addBtn.addEventListener('click', addRecip);
    who.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addRecip(); } });

    // Interne Folien
    const internalBox = el('div.cl-internal', { hidden: true });
    const confirmIn = el('input', { type: 'checkbox', id: 'cl-confirm' });
    let checked = null;   // {internal_count, internal}
    function resetCheck() { checked = null; internalBox.hidden = true; confirmIn.checked = false; go.textContent = 'Prüfen und senden'; }

    const prog = el('div.cl-progress', { hidden: true, role: 'status' });
    const bar = el('progress.cl-bar', { max: 1, value: 0 });
    const phase = el('span.small');
    prog.append(phase, bar);
    const err = el('p.small.rv-err', { role: 'alert' });
    const cancel = el('button.btn', { type: 'button' }, 'Abbrechen');
    const go = el('button.btn.btn--primary', { type: 'button' }, 'Prüfen und senden');
    cancel.addEventListener('click', finish);

    function showInternal(c) {
      const items = arr(c.internal);
      internalBox.replaceChildren(
        el('p.cl-internal__head', el('strong', `${fmt.int(c.internal_count)} ${c.internal_count === 1 ? 'Folie ist' : 'Folien sind'} intern`),
          ' – Kunden- oder eigene, nicht öffentliche Daten. Sie gehen nur nach ausdrücklicher Bestätigung mit:'),
        el('ul.cl-internal__list', items.slice(0, 40).map((x) => el('li', el('span', x.title || x.ref), el('span.small.muted', ` (${x.ref})`),
          arr(x.reasons).length ? el('span.small', ` – ${x.reasons.join('; ')}`) : null))),
        items.length > 40 ? el('p.small.muted', `… und ${fmt.int(items.length - 40)} weitere`) : null,
        el('label.rv-check.cl-confirm', { for: 'cl-confirm' }, confirmIn, 'Ich habe die Liste geprüft – die internen Folien dürfen an diese Empfänger gehen.'));
      internalBox.hidden = false;
    }

    async function poll(job) {
      try {
        const j = await cloudApi(`share?job=${encodeURIComponent(job)}`);
        phase.textContent = `${PHASE[j.phase] ?? j.phase}${j.phase === 'upload' && j.total ? ` ${mb(j.done)} von ${mb(j.total)}` : ''}`;
        bar.value = j.total ? j.done / j.total : (j.state === 'done' ? 1 : 0);
        if (j.state === 'running') return;
        clearInterval(timer);
        if (j.state === 'error') { err.textContent = `Nicht gesendet: ${j.error}`; go.disabled = false; cancel.disabled = false; return; }
        if (j.state === 'confirm') { checked = j.result; showInternal(checked); go.disabled = false; cancel.disabled = false; prog.hidden = true; return; }
        result = j.result;
        showDone(j.result);
      } catch (e) { clearInterval(timer); err.textContent = `Status unbekannt: ${e.message}`; go.disabled = false; cancel.disabled = false; }
    }

    function showDone(r) {
      const body = dlg.querySelector('.dialog__body');
      const close = el('button.btn.btn--primary', { type: 'button' }, 'Schliessen');
      close.addEventListener('click', finish);
      body.replaceChildren(headline('h3', 'Gesendet', 'Gesendet'),
        el('p', `„${r.title ?? 'Folienpaket'}“${Number.isFinite(r.slides) ? ` (${fmt.int(r.slides)} Folien)` : ''} liegt verschlüsselt im Postfach – ${mb(r.size ?? 0)}, abholbar bis ${fmt.date(r.expires)}.`),
        el('ul.rv-people.cl-recips', arr(r.recipients).map((p) => el('li.cl-recip', el('div.cl-recip__main', el('strong', p.email), fpWords(p.fingerprint)),
          el('span', { class: `badge ${p.confirmed ? 'badge--ok' : ''}` }, p.confirmed ? 'bestätigt' : 'unbestätigt')))),
        r.internal_count ? el('p.small', `Darunter ${fmt.int(r.internal_count)} interne Folien (bestätigt).`) : null,
        el('p.small.muted', 'Der Fundus der Empfänger holt das Paket innerhalb weniger Minuten ab; danach wird es aus der Cloud gelöscht (spätestens nach 30 Tagen).'),
        el('div.row.dialog__actions', close));
      ctx?.toast?.('Über Fundus Cloud gesendet.');
    }

    go.addEventListener('click', async () => {
      err.textContent = '';
      if (who.value.trim()) await addRecip();
      if (!recips.length) { err.textContent = 'Trage mindestens eine Person mit Fundus-Cloud-Konto ein.'; return; }
      go.disabled = true;
      try {
        if (!checked) {
          checked = await cloudApi('share', { action: 'check', refs: scope.refs });
          if (checked.internal_count) { showInternal(checked); go.disabled = false; go.textContent = 'Senden'; return; }
        }
        if (checked.internal_count && !confirmIn.checked) { err.textContent = 'Bestätige zuerst, dass die internen Folien mitgehen dürfen.'; go.disabled = false; return; }
        cancel.disabled = true; prog.hidden = false; phase.textContent = PHASE.start;
        const j = await cloudApi('share', { action: 'start', refs: scope.refs, recipients: recips.map((p) => p.email), confirm_internal: !!checked.internal_count && confirmIn.checked });
        timer = setInterval(() => poll(j.job), 700);
      } catch (e) { err.textContent = e.message; go.disabled = false; cancel.disabled = false; }
    });

    dlg.append(el('div.dialog__body',
      headline('h3', 'Teilen über Fundus Cloud', 'Fundus Cloud'),
      el('p.small.muted', 'Ein Paket je Versand, Ende-zu-Ende verschlüsselt für jede Person. Firebase dient nur als Postfach – Google sieht keine Inhalte.'),
      el('span.label', 'Umfang'), scopeBox,
      el('fieldset.rv-sec', el('legend', el('span.rv-check', 'Empfänger')),
        el('div.row.rv-who', who, addBtn), list,
        el('p.small.muted', 'Nur Personen mit Fundus-Cloud-Konto. „unbestätigt“: Fingerabdruck noch nicht telefonisch verglichen.')),
      internalBox, prog, err,
      el('div.row.dialog__actions', cancel, go)));
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); if (!cancel.disabled) finish(); });
    document.body.append(dlg);
    if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
    cloudApi('status').then((s) => {
      if (!s.connected) {
        err.textContent = 'Dieser Fundus ist nicht mit Fundus Cloud verbunden. Verbinden unter „Verwaltung“.';
        go.disabled = true; who.disabled = true; addBtn.disabled = true;
      }
    }).catch(() => {});
    who.focus();
  });
}
