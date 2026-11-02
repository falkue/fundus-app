/**
 * Schlichte Grossansicht für Folienbilder ausserhalb der Projektansicht (z. B. Prüfungen): blättern (Knöpfe, Pfeiltasten,
 * Wischen), Vollbild über den Knopf in der Leiste, das Symbol oben rechts auf der Folie oder Doppelklick.
 * openViewer(list, start) – list: [{ png, preview, title, sub, href, side }]; side (optional): () => Node für eine Spalte
 * rechts (z. B. Prüfstand, Quellen und Skript einer erhaltenen Folie).
 */
import { el, fullscreenToggle, dialogKeys } from '../lib/dom.js';

export function openViewer(list, start = 0) {
  document.querySelector('dialog.vw-lightbox')?.remove();
  if (!list.length) return;
  let i = Math.min(Math.max(0, start), list.length - 1);
  const dlg = el('dialog.fg-lightbox.vw-lightbox', { 'aria-label': 'Folie gross' });
  const img = el('img.fg-lb__img', { alt: '' });
  const prev = el('button.btn.fg-lb__nav', { type: 'button', 'aria-label': 'Vorherige Folie' }, '←');
  const next = el('button.btn.fg-lb__nav', { type: 'button', 'aria-label': 'Nächste Folie' }, '→');
  const close = el('button.btn.fg-lb__close', { type: 'button', 'aria-label': 'Schliessen' }, '✕');
  const pos = el('span.small.muted');
  const cap = el('span.small.vw-cap');
  const frame = el('div.fg-lb__frame', img);
  const stage = el('div.fg-lb__stage', frame);
  const fs = fullscreenToggle(dlg, 'fg-lightbox--full');
  fs.corner(frame, img);
  const withSide = list.some((s) => typeof s.side === 'function');
  const side = withSide ? el('aside.fg-lb__side') : null;
  dlg.append(el('div.fg-lb', el('div.fg-lb__bar', prev, pos, next, cap, el('span.fg-tools__grow'), fs, close),
    withSide ? el('div.fg-lb__main', stage, side) : el('div.fg-lb__main.vw-main', stage)));
  const show = () => {
    const s = list[i];
    pos.textContent = `${i + 1} / ${list.length}`;
    prev.disabled = i === 0; next.disabled = i === list.length - 1;
    img.src = s.png ?? s.preview ?? ''; img.alt = s.title ?? '';
    cap.replaceChildren(el('strong', s.title ?? ''), s.sub ? ` · ${s.sub}` : '', s.href ? el('a', { href: s.href, style: { marginLeft: '8px' } }, 'Im Projekt öffnen') : '');
    cap.querySelector('a')?.addEventListener('click', () => dlg.close());
    if (side) side.replaceChildren(typeof s.side === 'function' ? s.side() : '');
  };
  const go = (d) => { const j = i + d; if (j >= 0 && j < list.length) { i = j; show(); } };
  prev.addEventListener('click', () => go(-1));
  next.addEventListener('click', () => go(1));
  close.addEventListener('click', () => dlg.close());
  dialogKeys(dlg, (e) => {   // am Dokument: auch wenn Safari den Fokus aus dem Dialog nimmt
    if (e.key === 'ArrowLeft') { e.preventDefault(); go(-1); }
    if (e.key === 'ArrowRight') { e.preventDefault(); go(1); }
  });
  let x0 = null;
  stage.addEventListener('touchstart', (e) => { x0 = e.touches[0].clientX; }, { passive: true });
  stage.addEventListener('touchend', (e) => {
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
