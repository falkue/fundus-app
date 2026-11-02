/**
 * Ansicht „Verwaltung“ (#verwaltung): Einstellungen dieses Fundus. Erstes Feld: Fundus Cloud (fundus/cloud.py).
 * - Verbinden mit E-Mail und Passwort (gibt es das Konto, wird angemeldet, sonst angelegt). Die E-Mail wird nicht
 *   geprüft; das Passwort verlässt diesen Fundus nur als abgeleiteter Schlüssel (Argon2id), Google sieht es nie.
 * - Bei der Registrierung einmalig der Wiederherstellungscode; danach Fingerabdruck (6 Wörter) zum Vergleichen am Telefon.
 * - Kontakte mit Fingerabdruck und Schalter „bestätigt“; geänderte Schlüssel deutlich gewarnt (annehmen erst nach Prüfung).
 * - Pakete: gesendet und erhalten (lokales Protokoll), „Jetzt abholen“, Trennen.
 * Daten: /api/cloud/status | connect | disconnect | contacts | poll.
 */
import { el, pageHead, card, emptyNote, badge, confirmDialog } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { cloudApi, fpWords } from './cloud-ui.js';

const arr = (v) => (Array.isArray(v) ? v : []);
/** Zeiten vom Server: „YYYY-MM-DD HH:MM:SS“ in UTC oder ISO mit Zone. */
const when = (s) => { if (!s) return '–'; const t = String(s); return fmt.dateTime(t.includes(' ') ? `${t.replace(' ', 'T')}Z` : t); };
const STATE = { uploading: ['wird hochgeladen', ''], sent: ['im Postfach', ''], delivered: ['abgeholt', 'ok'], expired: ['abgelaufen', 'warn'],
  failed: ['fehlgeschlagen', 'danger'], received: ['erhalten', 'ok'], held_key: ['zurückgehalten: Schlüssel geändert', 'danger'], pending: ['wartet', ''] };

export function render(root, ctx) {
  let alive = true;
  const statusCard = el('div');
  const contactsCard = el('div');
  const packagesCard = el('div');
  root.append(pageHead({ title: 'Verwaltung', accent: 'Verwaltung',
    lede: 'Einstellungen dieses Fundus. Fundus Cloud: Folien Ende-zu-Ende verschlüsselt mit anderen Fundus-Installationen austauschen – ein Paket je Versand; Firebase ist nur Postfach und Adressbuch, Google sieht keine Inhalte.' }),
  el('div.cl-grid', statusCard, contactsCard), packagesCard);

  let st = null;
  async function load() {
    try {
      st = await cloudApi('status');
      if (!alive) return;
      drawStatus();
      if (st.connected) { loadContacts(); loadPackages(); } else { contactsCard.replaceChildren(); packagesCard.replaceChildren(); }
    } catch (e) { statusCard.replaceChildren(el('div.alert.alert--error', `Status nicht ladbar: ${e.message}`)); }
  }

  function drawStatus() {
    if (!st.connected) { statusCard.replaceChildren(connectForm()); return; }
    const pollBtn = el('button.btn', { type: 'button' }, 'Jetzt abholen');
    const off = el('button.btn', { type: 'button' }, 'Trennen');
    pollBtn.addEventListener('click', async () => {
      pollBtn.disabled = true;
      try {
        const r = await cloudApi('poll', {});
        const n = arr(r.received).length, h = arr(r.held).length, e = arr(r.errors).length;
        ctx.toast(r.busy ? 'Abholung läuft gerade schon.' : `${fmt.int(n)} ${n === 1 ? 'Paket' : 'Pakete'} erhalten${h ? `, ${h} zurückgehalten (Schlüssel geändert)` : ''}${e ? `, ${e} Fehler` : ''}.`, 5000);
        load();
      } catch (e) { ctx.toast(`Abholen fehlgeschlagen: ${e.message}`, 6000); }
      pollBtn.disabled = false;
    });
    off.addEventListener('click', async () => {
      if (!await confirmDialog({ title: 'Von Fundus Cloud trennen?', text: 'Dieser Fundus vergisst die Anmeldung und die lokale Kopie deiner Schlüssel. Kontakte und Protokoll bleiben. Mit E-Mail und Passwort verbindest du dich jederzeit wieder – mit derselben Identität.', confirmLabel: 'Trennen' })) return;
      try { await cloudApi('disconnect', {}); ctx.toast('Getrennt.'); load(); } catch (e) { ctx.toast(`Nicht getrennt: ${e.message}`, 6000); }
    });
    const p = st.packages ?? {};
    const inN = (p.in_received ?? 0), outN = (p.out_sent ?? 0) + (p.out_delivered ?? 0);
    statusCard.replaceChildren(card({ title: 'Fundus Cloud', accent: 'Cloud', tools: [badge('verbunden', 'ok')] },
      el('dl.cl-dl',
        el('dt', 'Konto'), el('dd', el('strong', st.email)),
        el('dt', 'Fingerabdruck'), el('dd', fpWords(st.fingerprint), el('p.small.muted', 'Diese sechs Wörter liest du deinen Kontakten am Telefon vor; sie vergleichen sie mit ihrer Anzeige.')),
        el('dt', 'Postfach'), el('dd', `${fmt.int(inN)} erhalten · ${fmt.int(outN)} gesendet · abgeholt wird alle ${fmt.int(Math.round((st.poll_seconds ?? 180) / 60))} Minuten`,
          el('br'), el('span.small.muted', `zuletzt ${st.last_poll ? when(st.last_poll) : 'noch nie'}`)),
        st.last_error ? el('dt', 'Fehler') : null, st.last_error ? el('dd.cl-err', st.last_error) : null,
        st.key_changes ? el('dt', 'Warnung') : null,
        st.key_changes ? el('dd.cl-err', `${fmt.int(st.key_changes)} ${st.key_changes === 1 ? 'Kontakt hat' : 'Kontakte haben'} einen neuen Schlüssel – siehe Kontakte.`) : null),
      el('div.row.cl-actions', pollBtn, off)));
  }

  function connectForm() {
    const email = el('input.input', { type: 'email', id: 'cl-email', autocomplete: 'username', placeholder: 'name@beispiel.ch' });
    const pw = el('input.input', { type: 'password', id: 'cl-pw', autocomplete: 'current-password', placeholder: 'mindestens 10 Zeichen' });
    const err = el('p.small.rv-err', { role: 'alert' });
    const go = el('button.btn.btn--primary', { type: 'submit' }, 'Verbinden');
    const form = el('form.cl-form', el('label.cl-field', { for: 'cl-email' }, el('span.label', 'E-Mail'), email),
      el('label.cl-field', { for: 'cl-pw' }, el('span.label', 'Passwort'), pw), err, el('div.row.cl-actions', go));
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      err.textContent = '';
      if (!email.value.includes('@')) { err.textContent = 'Bitte eine E-Mail-Adresse eingeben.'; return; }
      if (pw.value.length < 10) { err.textContent = 'Das Passwort braucht mindestens 10 Zeichen.'; return; }
      go.disabled = true; go.textContent = 'Verbinde …';
      try {
        const r = await cloudApi('connect', { email: email.value.trim(), password: pw.value });
        pw.value = '';
        if (r.recovery_code) await showRecovery(r);
        ctx.toast(r.created ? 'Konto angelegt und verbunden.' : 'Verbunden.');
        load();
      } catch (e2) { err.textContent = e2.message; go.disabled = false; go.textContent = 'Verbinden'; }
    });
    return card({ title: 'Fundus Cloud', accent: 'Cloud', tools: [badge('nicht verbunden')] },
      el('p.small', 'Mit E-Mail und Passwort verbinden. Gibt es noch kein Konto, wird es angelegt. Die E-Mail wird nicht bestätigt; sie ist nur deine Adresse im Adressbuch. Das Passwort bleibt hier: Firebase erhält nur einen daraus abgeleiteten Schlüssel.'),
      el('p.small.muted', 'Auf einem zweiten Rechner mit derselben E-Mail und demselben Passwort bekommst du dieselbe Identität (gleicher Fingerabdruck).'),
      form);
  }

  function showRecovery(r) {
    return new Promise((resolve) => {
      const dlg = el('dialog.dialog.cl-recovery', { 'aria-label': 'Wiederherstellungscode' });
      const ok = el('input', { type: 'checkbox', id: 'cl-rc-ok' });
      const done = el('button.btn.btn--primary', { type: 'button', disabled: true }, 'Fertig');
      const copy = el('button.btn', { type: 'button' }, 'Kopieren');
      ok.addEventListener('change', () => { done.disabled = !ok.checked; });
      copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText(r.recovery_code); ctx.toast('Kopiert.'); } catch { ctx.toast('Kopieren nicht möglich – bitte abschreiben.'); } });
      done.addEventListener('click', () => { dlg.close(); dlg.remove(); resolve(); });
      dlg.addEventListener('cancel', (e) => e.preventDefault());
      dlg.append(el('div.dialog__body',
        el('h3', 'Wiederherstellungscode'),
        el('p.small', 'Wird nur jetzt angezeigt. Bewahre ihn sicher auf (Passwortmanager, Papier). Er entschlüsselt deine Schlüssel, falls das Passwort verloren geht.'),
        el('code.cl-code', r.recovery_code), el('div.row', copy),
        el('p.small', 'Dein Fingerabdruck: ', fpWords(r.fingerprint)),
        el('label.rv-check', { for: 'cl-rc-ok' }, ok, 'Ich habe den Code gesichert.'),
        el('div.row.dialog__actions', done)));
      document.body.append(dlg);
      if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
    });
  }

  async function loadContacts() {
    try {
      const d = await cloudApi('contacts');
      if (!alive) return;
      const rows = arr(d.contacts);
      contactsCard.replaceChildren(card({ title: 'Kontakte', accent: 'Kontakte', sub: 'Kommen dazu, sobald du an jemanden sendest oder etwas erhältst. „bestätigt“ erst nach dem Wortvergleich am Telefon.' },
        rows.length ? el('ul.cl-contacts', rows.map(contactRow)) : emptyNote('Noch keine Kontakte.')));
    } catch (e) { contactsCard.replaceChildren(el('div.alert.alert--error', `Kontakte nicht ladbar: ${e.message}`)); }
  }

  function contactRow(c) {
    const id = `cl-c-${c.email.replace(/[^a-z0-9]/gi, '-')}`;
    const tog = el('input', { type: 'checkbox', id, checked: c.confirmed, disabled: c.key_changed, role: 'switch' });
    tog.addEventListener('change', async () => {
      try { await cloudApi('contacts', { email: c.email, action: tog.checked ? 'confirm' : 'unconfirm' }); ctx.toast(tog.checked ? `${c.email} bestätigt.` : `Bestätigung für ${c.email} entfernt.`); loadContacts(); }
      catch (e) { tog.checked = !tog.checked; ctx.toast(e.message, 6000); }
    });
    const warn = c.key_changed ? (() => {
      const acc = el('button.btn.btn--small', { type: 'button' }, 'Neuen Schlüssel annehmen …');
      acc.addEventListener('click', async () => {
        if (!await confirmDialog({ title: 'Neuen Schlüssel annehmen?', text: `Nur, wenn ${c.email} dir am Telefon bestätigt hat, dass ihr Fundus neu eingerichtet wurde und der neue Fingerabdruck „${c.new_fingerprint}“ lautet. Sonst könnte jemand anderes Pakete unter diesem Namen senden.`, confirmLabel: 'Annehmen' })) return;
        try { await cloudApi('contacts', { email: c.email, action: 'accept_key' }); ctx.toast('Neuer Schlüssel angenommen – Kontakt ist wieder unbestätigt.'); load(); }
        catch (e) { ctx.toast(e.message, 6000); }
      });
      return el('div.alert.alert--error.cl-keywarn', el('div', el('strong', 'Schlüssel geändert! '),
        `Seit dem ersten Kontakt zeigt das Adressbuch einen anderen Schlüssel. Pakete von ${c.email} werden zurückgehalten, Senden ist gesperrt.`,
        el('div.small', 'neu: ', fpWords(c.new_fingerprint)), acc));
    })() : null;
    return el('li.cl-contact', el('div.cl-contact__main', el('strong', c.email), fpWords(c.fingerprint),
      el('span.small.muted', `seit ${when(c.first_seen)}${c.confirmed_at ? ` · bestätigt ${when(c.confirmed_at)}` : ''}`), warn),
    el('label.cl-switch', { for: id }, tog, el('span', 'bestätigt')));
  }

  async function loadPackages() {
    try {
      const d = await cloudApi('poll');
      if (!alive) return;
      const rows = arr(d.packages);
      packagesCard.replaceChildren(card({ title: 'Pakete', accent: 'Pakete', sub: 'Lokales Protokoll dieses Fundus. Erhaltene Foliensätze erscheinen in Fundus wie andere Folien; unbestätigte Absender sind markiert.' },
        rows.length ? el('ul.cl-packages', rows.map((p) => {
          const [label, tone] = STATE[p.state] ?? [p.state, ''];
          const peer = p.direction === 'out' ? `an ${arr(p.peer).join(', ') || '–'}` : `von ${p.peer}`;
          return el('li.cl-pkg', el('span.cl-pkg__dir', { 'aria-label': p.direction === 'out' ? 'gesendet' : 'erhalten' }, p.direction === 'out' ? '↑' : '↓'),
            el('div.cl-pkg__main', el('strong', p.title || 'Folienpaket'), el('span.small', ` ${peer}`),
              el('div.small.muted', `${when(p.updated)}${p.size ? ` · ${fmt.bytes(p.size)}` : ''}${Number.isFinite(p.slides) ? ` · ${fmt.int(p.slides)} Folien` : ''}`),
              p.error ? el('div.small.cl-err', p.error) : null),
            p.direction === 'in' && p.state === 'received' && p.confirmed === false ? badge('Absender unbestätigt', 'warn') : null,
            badge(label, tone || undefined));
        })) : emptyNote('Noch nichts gesendet oder erhalten.')));
    } catch (e) { packagesCard.replaceChildren(el('div.alert.alert--error', `Pakete nicht ladbar: ${e.message}`)); }
  }

  load();
  return () => { alive = false; };
}
