"""Journal (container title) and publishing house of a DOI from Crossref – the publisher name alone cannot tell which
journal of a big house (Elsevier, SAGE, INFORMS …) an article appeared in."""
from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request

API = "https://api.crossref.org/works/"
CONTACT = os.environ.get("FUNDUS_CONTACT_EMAIL", "")   # Crossref "polite pool": a contact address
UA = "Fundus/1.0 (evidence archive" + (f"; mailto:{CONTACT}" if CONTACT else "") + ")"


def norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", str(doi).strip(), flags=re.I)
    return d if d.startswith("10.") else None


def lookup(doi: str | None, timeout: float = 6.0) -> dict | None:
    """{'journal', 'publisher'} for a DOI, None if unknown or unreachable (never raises).
    FUNDUS_NO_NET=1 switches the lookup off (tests)."""
    d = norm_doi(doi)
    if not d or os.environ.get("FUNDUS_NO_NET") == "1":
        return None
    try:
        req = urllib.request.Request(API + urllib.parse.quote(d, safe="/"), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            m = json.loads(r.read()).get("message") or {}
    except Exception:
        return None
    titles = m.get("container-title") or []
    clean = lambda v: html.unescape(re.sub(r"\s+", " ", v or "")).strip() or None   # Crossref sends "&amp;" etc.
    out = {"journal": clean(titles[0]) if titles else None, "publisher": clean(m.get("publisher"))}
    return out if any(out.values()) else None
