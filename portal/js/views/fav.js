/**
 * Favoriten (fundus/favorites.py): Stern an Kachel und Grossansicht, gemeinsam für #folien und #suche.
 * Sessions setzen ihn mit fundus_exhibit_favorite; die Liste aller Favoriten holt fundus_slides(['favorites']).
 * Daten: POST /api/favorites {refs, on}.
 */
import { el, icon } from '../lib/dom.js';
import { api } from './review-ui.js';

/** Favorit setzen oder entfernen; gibt den neuen Zustand zurück (wirft bei Fehler). */
export async function setFavorite(id, on) {
  await api('/api/favorites', { refs: [String(id)], on });
  return on;
}

/**
 * Stern-Knopf. x.favorite wird umgeschaltet; onChange(neuer Zustand) zeichnet neu.
 * variant 'tile' = rund über dem Vorschaubild, 'btn' = Knopf mit Text (Grossansicht).
 */
export function favButton(x, ctx, onChange, variant = 'tile') {
  const label = () => (x.favorite ? 'Favorit entfernen' : 'Als Favorit markieren');
  const b = el(variant === 'tile' ? 'button.fv-star' : 'button.btn.fv-btn', { type: 'button' });
  const draw = () => {
    b.classList.toggle('is-on', !!x.favorite);
    b.setAttribute('aria-pressed', String(!!x.favorite));
    b.setAttribute('aria-label', label());
    b.title = label();
    b.replaceChildren(icon('ic-star', 'fv-star__ic'), ...(variant === 'tile' ? [] : [x.favorite ? 'Favorit' : 'Favorit markieren']));
  };
  draw();
  b.addEventListener('click', async (e) => {
    e.stopPropagation();
    const on = !x.favorite;
    b.disabled = true;
    try {
      await setFavorite(x.id, on);
      x.favorite = on || undefined;
      draw();
      onChange?.(on);
    } catch (err) { ctx?.toast?.(`Nicht gespeichert: ${err.message}`, 5000); }
    b.disabled = false;
  });
  return b;
}
