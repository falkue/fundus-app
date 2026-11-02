"""Entity layer ("graph-lite"): organisations, people and products, their aliases, and links to items.

Embeddings blur names and know nothing about identity ('Unispital Zürich' = 'University Hospital Zurich' = 'USZ').
This module keeps that identity explicitly:

  * `entity` (organisation | person | product, optional parent), `entity_alias` (every spelling, normalised
    case- and diacritic-insensitively in `alias_norm`), `entity_link` (entity ↔ item with a role:
    publisher | organisation | speaker | subject | mentioned | via).
  * Seeds by rule (`seed`): every publisher, quote/practice organisations, organisation/tool facts, quote speakers,
    and acronym definitions found in item text ("University Hospital Zurich (USZ)"; these stay pending until the
    LLM confirms them).
  * Alias enrichment and clean-up with the local LLM (`enrich`, Ollama gemma4:26b, JSON mode, batches of 20):
    canonical name, de/en/fr variants, established abbreviations, parent organisation, splitting of co-publications.
    Results are cached in a JSONL file, so a rebuild costs no LLM time. Duplicates are then merged conservatively
    (same kind and same canonical name, or one entity's name is another one's alias); every merge is logged in the
    changelog and can be undone (`unmerge`).
  * Linking (`link`): publisher via document/series, organisation/speaker/forecaster fields, subject for
    organisation/tool facts, `via` from sourcing notes ("(via X)", "as cited by X", "Underlying source: X") and
    `mentioned` from a gazetteer over the item text (token n-gram lookup, longest match first; abbreviations only in
    upper case; ambiguous or generic aliases are never used for mentions).
  * Incremental (`update`): items changed since the last run (changelog marker) and items containing aliases added
    since then (found through the full-text index) are relinked; new entities from new field values are seeded
    by rule. LLM enrichment only runs in `fundus entities build` (resumable).

    python -m fundus.cli entities build [--no-llm] [--limit N] | link [--full] | merge SRC DST | unmerge ID
                                      | status | show NAME
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

from fundus.store import ITEM_KINDS, Store

KINDS = ("organisation", "person", "product")
ROLES = ("subject", "organisation", "speaker", "publisher", "via", "mentioned")
ROLE_RANK = {r: i for i, r in enumerate(ROLES)}          # lower = stronger
LINK_KINDS = ITEM_KINDS + ("series",)
ACTOR = "entities"

LLM_MODEL = os.environ.get("FUNDUS_ENTITY_MODEL", "gemma4:26b")
LLM_KEEP_ALIVE = os.environ.get("FUNDUS_ENTITY_KEEP_ALIVE", "24h")   # do not shorten the keep-alive of a shared model
LLM_NUM_CTX = int(os.environ.get("FUNDUS_LLM_NUM_CTX", "262144"))   # = keep-gemma4-warm, TaskMe, OpenClaw
LLM_BATCH = 20
LLM_TIMEOUT = 900
# Longest answer we accept. In JSON mode with temperature 0 gemma4 can fall into a loop and write until the context is
# full (28 Oct 2026: 47'500 tokens in 15 min for a slide search, GPU at 100 %, other sessions queued) – a cap ends it.
LLM_MAX_TOKENS = int(os.environ.get("FUNDUS_LLM_MAX_TOKENS", "8192"))

# Alias sources that only serve exact field lookups (raw field values, slug variants, co-publication names) and are
# never used for gazetteer mentions or query detection.
LOOKUP_ONLY = {"raw", "compound", "publisher_alias", "ambiguous", "common_word"}
# 'ambiguous' (flagged by the LLM: also a surname, place, common word …) is still used to recognise entities in search
# queries, where a wrong guess only adds a ranking; 'common_word' (mostly written in lower case in the archive) is not.
QUERY_OK = {"ambiguous"}
# Text fields scanned for mentions, per kind.
MENTION_FIELDS = {
    "observation": ("metric", "category", "population", "quote_context", "notes"),
    "forecast": ("statement", "original", "metric", "scenario", "notes"),
    "study": ("finding", "intervention", "outcome", "sector", "moderators", "notes"),
    "quote": ("text", "original", "role", "context", "notes"),
    "practice": ("what", "segment", "result", "people", "quote", "notes"),
    "fact": ("title", "what", "implication", "attributes", "notes"),
    "series": ("name", "population", "method", "notes"),
}

# ------------------------------------------------------------------ normalisation
_TRANS = str.maketrans({"ß": "ss", "ẞ": "SS", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O", "œ": "oe", "Œ": "OE",
                        "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ı": "i", "’": "'", "‘": "'"})
_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"})
_TOK = re.compile(r"[^\W_]+|&")


def fold(text: str | None) -> str:
    """Strip diacritics (keeping case): 'Zürich' → 'Zurich', 'Straße' → 'Strasse'."""
    t = unicodedata.normalize("NFKD", (text or "").translate(_TRANS))
    return "".join(c for c in t if not unicodedata.combining(c))


def tokens(text: str | None) -> list[str]:
    """Word tokens with original case, diacritics stripped; '&' becomes 'and'."""
    return ["and" if t == "&" else t for t in _TOK.findall(fold(text))]


def norm(text: str | None) -> str:
    """Comparable form of a name: case- and diacritic-insensitive, punctuation-free, single spaces."""
    return " ".join(t.casefold() for t in tokens(text))


def umlaut_variant(alias: str) -> str | None:
    """'Universitätsspital Zürich' → 'Universitaetsspital Zuerich' (the transliteration sources use)."""
    v = alias.translate(_UMLAUT)
    return v if v != alias else None


_UNI = re.compile(r"\bUniversit(?:ä|ae|a)ts(?=[a-zäöü])|\bUniversit(?:ä|ae|a)t(?= )")


def short_variant(alias: str) -> str | None:
    """German colloquial short form: 'Universitätsspital Basel' → 'Unispital Basel', 'Universitätsklinikum Leipzig' →
    'Uniklinikum Leipzig', 'Universität Zürich' → 'Uni Zürich'."""
    v = _UNI.sub("Uni", alias)
    return v if v != alias and len(v.split()) >= 2 else None


def is_abbrev(alias: str) -> bool:
    """An abbreviation: one token, upper case (digits allowed), 2–8 characters ('USZ', 'HSG', '3M')."""
    a = alias.strip()
    return bool(re.fullmatch(r"[A-Z0-9&]{2,8}", a)) and any(c.isalpha() for c in a)


def clean_name(raw: str | None, cut_title: bool = False) -> str | None:
    """Field value → name: comments in brackets removed ('AJMC (reporting …)' → 'AJMC'); for fact titles also the
    part after ': ' or ' – ' ('Pinewood.AI: AI products …' → 'Pinewood.AI')."""
    if not raw:
        return None
    n = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", " ", raw)
    n = re.sub(r"\s*[\(\[].*$", "", n)                      # unclosed bracket
    if cut_title:
        n = re.split(r":\s|\s[–—-]\s|;\s", n, maxsplit=1)[0]
    n = re.sub(r"\s+", " ", n).strip(" ,;:/|-–")
    return n or None


def initials_match(abbr: str, long_form: str) -> bool:
    """Schwartz–Hearst style check: the abbreviation's letters occur in order in the long form, the first one
    starting a word ('USZ' ~ 'Universitätsspital Zürich', 'TPMG' !~ 'Kaiser Permanente')."""
    letters = [c.lower() for c in fold(abbr) if c.isalnum()]
    s = fold(long_form).lower()
    if not letters or len(s) <= len(abbr) or not s.startswith(letters[0]):
        return False
    j = 0
    for c in letters:
        j = s.find(c, j)
        if j < 0:
            return False
        j += 1
    return True


def paren_abbrevs(raw: str | None) -> list[str]:
    """Abbreviations given in brackets after a name: 'Universitätsspital Basel (USB)' → ['USB']."""
    if not raw:
        return []
    base = clean_name(raw) or ""
    return [m for m in re.findall(r"\(([A-Z][A-Za-z0-9&]{1,9})\)", raw) if is_abbrev(m) and initials_match(m, base)]


# Single words that are never used as a mention/query alias on their own (places, generic words).
_PLACES = {"zurich", "geneva", "genf", "geneve", "basel", "bern", "berne", "lausanne", "lugano", "st gallen", "luzern",
           "lucerne", "winterthur", "aarau", "zug", "berlin", "munich", "munchen", "hamburg", "frankfurt", "cologne",
           "koln", "stuttgart", "dusseldorf", "vienna", "wien", "paris", "london", "new york", "boston", "chicago",
           "seattle", "san francisco", "los angeles", "washington", "texas", "california", "florida", "georgia",
           "tokyo", "beijing", "shanghai", "shenzhen", "hong kong", "singapore", "sydney", "toronto", "amsterdam",
           "brussels", "milan", "madrid", "barcelona", "dubai", "europe", "asia", "africa", "america", "zuerich"}
def _geo_names() -> set[str]:
    from fundus.geo import COUNTRIES, REGIONS
    out = set()
    for label, _, alts in COUNTRIES.values():
        out |= {norm(label), *(norm(a) for a in alts)}
    for names in REGIONS.values():
        out |= {norm(n) for n in names}
    return out


_PLACES |= _geo_names()
_GENERIC = set("""
index group institute university college school health healthcare clinic hospital spital bank insurance report survey
news digital data research media global international national foundation center centre association council office
agency ministry department partners capital ventures labs lab systems solutions technologies technology consulting
analytics insights intelligence network services software cloud platform studio times journal review post daily
weekly magazine press science nature future open company companies business market markets retail motors auto
automotive energy finance financial government federal state public private union commission board committee
alliance consortium initiative project program programme team world economy economics management marketing sales
ai artificial generative model models agent agents copilot assistant app apps tool tools service product products
store online mobile smart care medical life lifestyle luxury fashion beauty home travel hotel hotels air
people human humans users customers patients doctors physicians dealers dealer brand brands
""".split())
_COMMON_ACRONYMS = set("""
AI CEO CFO CIO CMO CTO COO CHRO CDO CISO GDP API APIS LLM LLMS ROI KPI KPIS USA USD EUR CHF GBP JPY CNY B2B B2C SME
SMES SMB IT HR ESG EU UK US FAQ PDF CRM ERP EHR EMR GPT NLP ML OEM OEMS DMS SAAS IPO FTE NPS CAGR YOY QOQ EBIT EBITDA
APAC EMEA DACH LATAM MENA AGI GPU GPUS CPU TCO SLA RAG MRI ICU ED GP ER NHS? CEE TV PR IOT VR AR XR UX UI QA FY Q1 Q2
Q3 Q4 H1 H2 PHD MBA BSC MSC USP DTC D2C PPP GHG CO2 EV EVS ICE BEV PHEV ADAS NEW TOP ALL AND THE FOR NOT
""".replace("?", "").split())
_ABBREV_WHITELIST = {"EY", "HP", "3M", "LG", "GM", "BP"}


# ------------------------------------------------------------------ resolver (gazetteer + lookup)
_WORDS: set[str] | None = None


def common_words() -> set[str]:
    """Lower-case English and German dictionary words (system word lists; empty if none are installed)."""
    global _WORDS
    if _WORDS is None:
        _WORDS = set()
        for f in ("american-english", "british-english", "ngerman", "swiss"):
            try:
                with open(f"/usr/share/dict/{f}", encoding="utf-8", errors="ignore") as fh:
                    _WORDS |= {w.strip() for w in fh if w.strip() and w.strip().islower()}
            except OSError:
                pass
    return _WORDS


def word_like_abbrev(alias: str, entity_name: str | None) -> bool:
    """An abbreviation that is also an ordinary word ('NICE', 'WHO', 'SAGE'), unless it is the entity's own name
    ('SAP', 'KAYAK'): in text it names the entity only when the entity is named some other way too."""
    a = alias.strip()
    return a.lower() in common_words() and fold(a).casefold() != fold(entity_name or "").casefold()


_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ][\w’'-]*|\d+")


def spelled_otherwise(text: str, abbrev_norm: str, entity_name: str | None) -> bool:
    """True if `text` expands the abbreviation into words other than the entity's name: right before it
    ('novelty, lock-in, complementarities, efficiency (NICE)'), right after it in capitals ('NICE: Novelty, Lock-in, …')
    or as a letter list ('N Novelty … L Lock-in … C Complementarities … E Efficiency')."""
    letters = abbrev_norm.replace(" ", "").casefold()
    if len(letters) < 2:
        return False
    words = _WORD_RE.findall(text or "")
    low = [w.casefold() for w in words]
    name_low = (entity_name or "").casefold()

    def fits(w: str, ch: str) -> bool:        # 'lock-in' stands for L and for I
        return len(w) > 1 and any(part[:1] == ch for part in w.split("-"))

    def not_entity(seq: list[str]) -> bool:
        return not all(w.casefold() in name_low for w in seq)

    # 1) letter list: single letters each followed by a word with that initial; the letters spell the abbreviation
    #    (one letter may differ: 'NICE' is laid out as N, L(ock-in), C, E)
    pairs = [(low[k], words[k + 1]) for k in range(len(low) - 1)
             if len(low[k]) == 1 and low[k].isalpha() and fits(low[k + 1], low[k])]
    if len(pairs) >= max(3, len(letters) - 1):
        seq, li = [], 0
        for ch, w in pairs:
            if li < len(letters) and (ch == letters[li] or fits(w.casefold(), letters[li])):
                seq.append(w); li += 1
        if li == len(letters) and not_entity(seq):
            return True
    # 2) an expansion right next to the abbreviation (fillers like 'and' allowed)
    fillers = {"and", "und", "of", "the", "for", "&", "et", "for"}

    def run(start: int, stop: int) -> tuple[list[str], int]:
        seq, k, li = [], start, 0
        while k < len(low) and li < len(letters) and k != stop:
            if fits(low[k], letters[li]):
                seq.append(words[k]); li += 1
            elif not (low[k] in fillers and seq):
                break
            k += 1
        return (seq, k) if li == len(letters) else ([], k)
    in_parens = re.search(r"\(\s*" + re.escape(letters) + r"\s*\)", text or "", re.I) is not None
    for a in [k for k, w in enumerate(low) if w == letters]:
        for start in range(max(0, a - 3 * len(letters)), a if in_parens else 0):   # '… efficiency (NICE)'
            seq, end = run(start, a)
            if seq and end == a and not_entity(seq):
                return True
        seq, _ = run(a + 1, -1)                                        # starts right after it, in capitals
        if seq and all(w[:1].isupper() for w in seq) and not_entity(seq):
            return True
    return False


class Resolver:
    """In-memory alias dictionaries built from the database.

    lookup:  alias_norm → {entity ids}  (all aliases of non-pending entities; exact field lookups)
    gaz:     alias_norm → (entity id, abbrev, lowercase_style)  (unambiguous, specific aliases only: mentions, queries)
    """

    def __init__(self, store: Store):
        db = store.db
        self.kind: dict[int, str] = {}
        self.name: dict[int, str] = {}
        pending = set()
        for eid, kind, name, enriched in db.execute("SELECT id, kind, name, enriched_at FROM entity"):
            self.kind[eid], self.name[eid] = kind, name
            if enriched is None:
                pending.add(eid)
        rows = db.execute("SELECT entity_id, alias, alias_norm, source FROM entity_alias").fetchall()
        confirmed = {eid for eid, _, _, src in rows if src not in ("pattern", "abbrev")}
        pending -= confirmed           # rule seeds are usable before enrichment; pattern-only seeds are not
        self.pending = pending
        self.lookup: dict[str, set[int]] = defaultdict(set)
        spec: dict[str, set[int]] = defaultdict(set)
        meta: dict[tuple[str, int], tuple[str, str]] = {}
        for eid, alias, an, src in rows:
            if eid in pending or not an:
                continue
            self.lookup[an].add(eid)
            if src not in LOOKUP_ONLY or src in QUERY_OK:
                spec[an].add(eid)
                meta[(an, eid)] = (alias, src)
        self.gaz: dict[str, tuple[int, bool, bool]] = {}        # text mentions
        self.qgaz: dict[str, tuple[int, bool, bool]] = {}       # search queries (also LLM-'ambiguous' aliases)
        self.first_max: dict[str, int] = {}
        self.qfirst_max: dict[str, int] = {}
        self.wordlike: set[str] = set()        # abbreviations that are also ordinary words (see word_like_abbrev)
        for an, eids in spec.items():
            if len(eids) != 1:
                continue
            eid = next(iter(eids))
            alias, src = meta[(an, eid)]
            ok, abbrev = eligible(alias, an, self.kind[eid])
            lower_style = alias == alias.lower()
            if not ok or (lower_style and " " not in an and not any(c.isdigit() for c in an)):
                continue       # lower-case one-word brands ('trend', 'voize') are indistinguishable from ordinary words
            entry = (eid, abbrev, lower_style)
            if abbrev and word_like_abbrev(alias, self.name[eid]):
                self.wordlike.add(an)
            toks = an.split()
            self.qgaz[an] = entry
            self.qfirst_max[toks[0]] = max(self.qfirst_max.get(toks[0], 0), len(toks))
            if src not in QUERY_OK:
                self.gaz[an] = entry
                self.first_max[toks[0]] = max(self.first_max.get(toks[0], 0), len(toks))

    # -------------------------------------------------------------- matching
    def mentions(self, text: str | None, query: bool = False) -> list[tuple[int, str]]:
        """Entities named in `text`: [(entity id, matched alias_norm)], longest match first, no overlaps.
        Abbreviations must be upper case in the text; single words and other aliases need a capitalised token
        (proper noun) unless the alias itself is lower-case styled. In queries (`query=True`) multi-word aliases also
        match in lower case, and a trailing genitive 's' is tolerated ('Gartners')."""
        toks = tokens(text)
        if not toks:
            return []
        low = [t.casefold() for t in toks]
        lower_query = query and not any(t[:1].isupper() for t in toks)
        gaz, first_max = (self.qgaz, self.qfirst_max) if query else (self.gaz, self.first_max)
        out, i, n = [], 0, len(toks)
        while i < n:
            m = first_max.get(low[i], 0)
            hit = None
            for L in range(min(m, n - i), 0, -1):
                key = " ".join(low[i:i + L])
                g = gaz.get(key)
                if g and self._accept(g, toks[i:i + L], L, query, lower_query):
                    hit = (g[0], key, L)
                    break
            if hit is None and query and len(low[i]) > 5 and low[i].endswith("s"):
                g = gaz.get(low[i][:-1])
                if g and not g[1] and not g[2] and toks[i][:1].isupper():
                    hit = (g[0], low[i][:-1], 1)
            if hit:
                out.append((hit[0], hit[1]))
                i += hit[2]
            else:
                i += 1
        if not query and any(an in self.wordlike for _, an in out):
            # 'NICE' (Novelty, Lock-in, Complementarities, Efficiency) is not the health institute: an abbreviation that
            # is also an ordinary word is dropped when the text spells it out as something else
            out = [(e, an) for e, an in out if an not in self.wordlike or not spelled_otherwise(text, an, self.name.get(e))]
        return out

    @staticmethod
    def _accept(g, orig: list[str], L: int, query: bool, lower_query: bool) -> bool:
        _, abbrev, lower_style = g
        if abbrev:
            return orig[0].isupper()
        if lower_style:
            return True
        if L > 1 and query:
            return True
        if lower_query and L == 1 and len(orig[0]) >= 5:
            return True
        return any(t[:1].isupper() or t[:1].isdigit() for t in orig)

    def resolve_value(self, value: str | None, kinds: tuple[str, ...] | None = None) -> set[int]:
        """Entities a field value denotes: exact alias (raw or cleaned) first, else the entities named in it."""
        if not value:
            return set()
        found = set()
        for cand in (value, clean_name(value)):
            if cand:
                found |= self.lookup.get(norm(cand), set())
        if not found:
            found = {eid for eid, _ in self.mentions(value)}
        if kinds:
            found = {e for e in found if self.kind.get(e) in kinds}
        return found


def eligible(alias: str, alias_norm: str, kind: str) -> tuple[bool, bool]:
    """(usable for mentions/queries, is abbreviation) – conservative: no short, generic, place or ambiguous aliases."""
    toks = alias_norm.split()
    if is_abbrev(alias):
        a = fold(alias).upper()
        if a in _COMMON_ACRONYMS:
            return False, True
        return (len(a) >= 3 or a in _ABBREV_WHITELIST), True
    if len(alias_norm) < 3:
        return False, False
    if kind == "person":
        return len(toks) >= 2, False
    if alias_norm in _PLACES:
        return False, False
    if len(toks) == 1:
        t = toks[0]
        if t in _GENERIC or len(t) < 3 or t.isdigit():
            return False, False
    elif all(t in _GENERIC or t in ("of", "the", "and", "for", "de", "der", "und") for t in toks):
        return False, False
    return True, False


_resolver_cache: dict = {}


def resolver(store: Store) -> Resolver:
    """Resolver for this database, rebuilt when entities or aliases changed."""
    sig = store.db.execute("SELECT (SELECT coalesce(max(rowid), 0) || ':' || count(*) FROM entity_alias) || '/' || "
                           "(SELECT coalesce(max(id), 0) || ':' || count(*) || ':' || count(enriched_at) FROM entity)").fetchone()[0]
    key = str(store.path)
    hit = _resolver_cache.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    r = Resolver(store)
    _resolver_cache[key] = (sig, r)
    return r


# ------------------------------------------------------------------ entity writes
def add_alias(store: Store, eid: int, alias: str | None, source: str, lang: str | None = None) -> bool:
    """Add one alias (plus its ä→ae transliteration); returns True if something new was stored."""
    if not alias or not alias.strip():
        return False
    alias = re.sub(r"\s+", " ", alias).strip()
    new = False
    short = short_variant(alias)
    for a in (alias, umlaut_variant(alias), short, umlaut_variant(short) if short else None):
        if not a:
            continue
        an = norm(a)
        if not an:
            continue
        cur = store.db.execute("INSERT OR IGNORE INTO entity_alias(entity_id, alias, alias_norm, lang, source) "
                               "VALUES (?,?,?,?,?)", (eid, a, an, lang, source))
        new |= cur.rowcount > 0
    return new


def create_entity(store: Store, kind: str, name: str, source: str, country: str | None = None,
                  aliases: list[tuple[str, str]] = ()) -> int:
    eid = store.db.execute("INSERT INTO entity(kind, name, country) VALUES (?,?,?)", (kind, name, country)).lastrowid
    add_alias(store, eid, name, source)
    for a, src in aliases:
        add_alias(store, eid, a, src)
    store.log("entity", eid, "insert", {"name": name, "kind": kind, "source": source})
    return eid


def _find(store: Store, names, kind: str | None = None, rejected_ok=False) -> int | None:
    """Existing entity owning one of these names as alias (unique match; preferring the given kind)."""
    for n in names:
        an = norm(n)
        if not an:
            continue
        rows = store.db.execute("SELECT DISTINCT a.entity_id, e.kind FROM entity_alias a JOIN entity e ON e.id = a.entity_id "
                                "WHERE a.alias_norm = ?", (an,)).fetchall()
        if kind:
            same = [r[0] for r in rows if r[1] == kind]
            if len(same) == 1:
                return same[0]
        if len(rows) == 1:
            return rows[0][0]
    return None


def is_rejected(store: Store, name: str) -> bool:
    return bool(store.db.execute("SELECT 1 FROM entity_rejected WHERE alias_norm = ?", (norm(name),)).fetchone())


def reject(store: Store, name: str, reason: str) -> None:
    if norm(name):
        store.db.execute("INSERT OR IGNORE INTO entity_rejected(alias_norm, alias, reason) VALUES (?,?,?)",
                         (norm(name), name, reason))


def delete_entity(store: Store, eid: int, reason: str) -> None:
    row = store.db.execute("SELECT * FROM entity WHERE id = ?", (eid,)).fetchone()
    if row is None:
        return
    aliases = [dict(r) for r in store.db.execute("SELECT alias, alias_norm, lang, source FROM entity_alias "
                                                  "WHERE entity_id = ?", (eid,))]
    for a in aliases:
        if a["source"] not in ("abbrev",):
            reject(store, a["alias"], reason)
    store.db.execute("UPDATE entity SET parent_id = NULL WHERE parent_id = ?", (eid,))
    store.db.execute("DELETE FROM entity_link WHERE entity_id = ?", (eid,))
    store.db.execute("DELETE FROM entity_alias WHERE entity_id = ?", (eid,))
    store.db.execute("DELETE FROM entity WHERE id = ?", (eid,))
    store.log("entity", eid, "delete", {"name": row["name"], "reason": reason, "aliases": [a["alias"] for a in aliases]})


def merge(store: Store, src: int, dst: int, actor: str | None = None, reason: str = "manual") -> dict:
    """Fold entity `src` into `dst`: aliases, links and children move, empty fields are filled, src is deleted.
    The changelog row ('entity', dst, 'merge') keeps everything needed for `unmerge`."""
    if src == dst:
        raise ValueError("cannot merge an entity into itself")
    s = store.db.execute("SELECT * FROM entity WHERE id = ?", (src,)).fetchone()
    d = store.db.execute("SELECT * FROM entity WHERE id = ?", (dst,)).fetchone()
    if s is None or d is None:
        raise LookupError(f"entity {src if s is None else dst} not found")
    db = store.db
    have = {r[0] for r in db.execute("SELECT alias_norm FROM entity_alias WHERE entity_id = ?", (dst,))}
    aliases = [dict(r) for r in db.execute("SELECT alias, alias_norm, lang, source FROM entity_alias WHERE entity_id = ?", (src,))]
    manual = [dict(r) for r in db.execute("SELECT item_kind, item_id, role, source, confidence FROM entity_link "
                                          "WHERE entity_id = ? AND source != 'rule'", (src,))]
    children = [r[0] for r in db.execute("SELECT id FROM entity WHERE parent_id = ?", (src,))]
    db.execute("INSERT OR IGNORE INTO entity_alias(entity_id, alias, alias_norm, lang, source) "
               "SELECT ?, alias, alias_norm, lang, source FROM entity_alias WHERE entity_id = ?", (dst, src))
    n_links = db.execute("INSERT OR IGNORE INTO entity_link(entity_id, item_kind, item_id, role, source, confidence, created_at) "
                         "SELECT ?, item_kind, item_id, role, source, confidence, created_at FROM entity_link "
                         "WHERE entity_id = ?", (dst, src)).rowcount
    db.execute("UPDATE entity SET parent_id = ? WHERE parent_id = ? AND id != ?", (dst, src, dst))
    fill = {k: s[k] for k in ("description", "country", "parent_id") if s[k] and not d[k] and not (k == "parent_id" and s[k] == dst)}
    if fill:
        db.execute(f"UPDATE entity SET {', '.join(f'{k} = ?' for k in fill)} WHERE id = ?", [*fill.values(), dst])
    db.execute("DELETE FROM entity_link WHERE entity_id = ?", (src,))
    db.execute("DELETE FROM entity_alias WHERE entity_id = ?", (src,))
    db.execute("DELETE FROM entity WHERE id = ?", (src,))
    detail = {"from": src, "into": dst, "reason": reason, "src": {k: s[k] for k in s.keys()},
              "aliases": aliases, "aliases_added": [a["alias_norm"] for a in aliases if a["alias_norm"] not in have],
              "manual_links": manual, "children": children, "filled": fill, "links_moved": n_links}
    prev, store.actor = store.actor, actor or store.actor
    try:
        store.log("entity", dst, "merge", detail)
    finally:
        store.actor = prev
    return {"merged": src, "into": dst, "name": s["name"], "into_name": d["name"], "links_moved": n_links}


def unmerge(store: Store, changelog_id: int) -> dict:
    """Undo one logged merge: the source entity comes back with its id, aliases, children and manual links; aliases it
    had brought along are removed from the target again; the items of both entities are relinked."""
    r = store.db.execute("SELECT item_kind, action, detail FROM changelog WHERE id = ?", (changelog_id,)).fetchone()
    if r is None or r[0] != "entity" or r[1] != "merge":
        raise LookupError(f"changelog #{changelog_id} is not an entity merge")
    d = json.loads(r[2])
    s, dst = d["src"], d["into"]
    if store.db.execute("SELECT 1 FROM entity WHERE id = ?", (s["id"],)).fetchone():
        raise ValueError(f"entity {s['id']} exists again – already undone?")
    db = store.db
    db.execute("INSERT INTO entity(id, kind, name, description, country, parent_id, enriched_at, created_at) "
               "VALUES (?,?,?,?,?,?,?,?)", (s["id"], s["kind"], s["name"], s["description"], s["country"],
                                            s["parent_id"] if s["parent_id"] != dst else None, s["enriched_at"], s["created_at"]))
    for a in d["aliases"]:
        db.execute("INSERT OR IGNORE INTO entity_alias(entity_id, alias, alias_norm, lang, source) VALUES (?,?,?,?,?)",
                   (s["id"], a["alias"], a["alias_norm"], a["lang"], a["source"]))
    for an in d["aliases_added"]:
        db.execute("DELETE FROM entity_alias WHERE entity_id = ? AND alias_norm = ?", (dst, an))
    for ch in d["children"]:
        db.execute("UPDATE entity SET parent_id = ? WHERE id = ?", (s["id"], ch))
    for l in d["manual_links"]:
        db.execute("INSERT OR IGNORE INTO entity_link(entity_id, item_kind, item_id, role, source, confidence) "
                   "VALUES (?,?,?,?,?,?)", (s["id"], l["item_kind"], l["item_id"], l["role"], l["source"], l["confidence"]))
        db.execute("DELETE FROM entity_link WHERE entity_id = ? AND item_kind = ? AND item_id = ? AND role = ? "
                   "AND source = ?", (dst, l["item_kind"], l["item_id"], l["role"], l["source"]))
    if d.get("filled"):
        db.execute(f"UPDATE entity SET {', '.join(f'{k} = NULL' for k in d['filled'])} WHERE id = ?", (dst,))
    store.log("entity", s["id"], "unmerge", {"changelog_id": changelog_id, "from": dst})
    items = {(k, i) for k, i in db.execute("SELECT item_kind, item_id FROM entity_link WHERE entity_id = ?", (dst,))}
    n = link(store, items=items)["items"] if items else 0
    return {"restored": s["id"], "name": s["name"], "from": dst, "relinked_items": n}


# ------------------------------------------------------------------ seeding by rule
def _seed_value(store: Store, value: str | None, kind: str, source: str, cut_title: bool = False,
                country: str | None = None, extra_aliases=()) -> int | None:
    """Find or create the entity for one field value. Raw values that differ from the cleaned name are kept as
    lookup-only aliases ('raw')."""
    if not value or not value.strip():
        return None
    name = clean_name(value, cut_title=cut_title)
    if not name or len(norm(name)) < 2 or is_rejected(store, value) or is_rejected(store, name):
        return None
    eid = _find(store, [value, name], kind)
    created = False
    if eid is None:
        if store.db.execute("SELECT 1 FROM entity_alias WHERE alias_norm IN (?, ?) LIMIT 1", (norm(value), norm(name))).fetchone():
            return None        # the name already belongs to several entities (e.g. the parts of a co-publication)
        eid = create_entity(store, kind, name, source, country=country)
        created = True
    if norm(value) != norm(name):
        add_alias(store, eid, value, "raw")
    elif not created:
        add_alias(store, eid, value, source)
    for ab in paren_abbrevs(value):
        add_alias(store, eid, ab, "abbrev")
    for a, src in extra_aliases:
        add_alias(store, eid, a, src)
    return eid


def seed(store: Store, since: int | None = None) -> dict:
    """Rule seeds: publishers (+ their slug variants), quote/practice organisations, organisation/tool facts, quote
    speakers (affiliation = the quote's organisation). With `since` only values of items changed after that changelog
    id (publishers are always checked – they are few). Idempotent."""
    db = store.db
    counts = Counter()
    before = db.execute("SELECT count(*) FROM entity").fetchone()[0]
    variants: dict[str, list[str]] = defaultdict(list)
    for alias, pid in db.execute("SELECT alias, publisher_id FROM publisher_alias"):
        variants[pid].append(alias.replace("-", " "))
    have = {r[0] for r in db.execute("SELECT alias_norm FROM entity_alias")}
    for pid, name, country in db.execute("SELECT id, name, country FROM publisher").fetchall():
        if norm(name) in have:
            for (eid,) in db.execute("SELECT DISTINCT entity_id FROM entity_alias WHERE alias_norm = ?", (norm(name),)).fetchall():
                for v in variants.get(pid, []):
                    add_alias(store, eid, v, "publisher_alias")
            continue
        _seed_value(store, name, "organisation", "publisher", country=country,
                    extra_aliases=[(v, "publisher_alias") for v in variants.get(pid, [])])
        counts["publisher"] += 1
    only = ""
    if since is not None:
        store._changed_since(since)
        only = " AND CAST(id AS TEXT) IN (SELECT id FROM _chg WHERE kind = '{kind}')"
    for kind, col, ekind, src, cut in (("quote", "organisation", "organisation", "quote.organisation", False),
                                       ("practice", "organisation", "organisation", "practice.organisation", False),
                                       ("fact", "title", None, "fact", True)):
        extra = " AND kind IN ('organisation', 'tool')" if kind == "fact" else ""
        rows = db.execute(f"SELECT DISTINCT {col}{', kind' if kind == 'fact' else ''} FROM {kind} "
                          f"WHERE {col} IS NOT NULL{extra}{only.format(kind=kind)}").fetchall()
        for r in rows:
            value = r[0]
            if norm(value) in have:
                continue
            k = ekind or ("product" if r[1] == "tool" else "organisation")
            s = src if kind != "fact" else f"fact.{r[1]}"
            _seed_value(store, value, k, s, cut_title=cut)
            counts[s] += 1
    for speaker, org, role in db.execute(f"SELECT DISTINCT speaker, organisation, role FROM quote WHERE speaker IS NOT NULL"
                                         f"{only.format(kind='quote')}").fetchall():
        name = clean_name(speaker)
        if not name or len(name.split()) < 2 or not re.search(r"[A-Z]", name[:1]) or norm(speaker) in have:
            continue
        eid = _seed_value(store, speaker, "person", "speaker")
        if eid and (role or org):
            db.execute("UPDATE entity SET description = ? WHERE id = ? AND description IS NULL",
                       (", ".join(x for x in (clean_name(role), clean_name(org)) if x)[:200] or None, eid))
        counts["speaker"] += 1
    counts["created"] = db.execute("SELECT count(*) FROM entity").fetchone()[0] - before
    return dict(counts)


_ACRO_W = r"[A-ZÄÖÜ][\w\.\-&']*"
_ACRO_JOIN = r"(?:" + _ACRO_W + r"|of|for|de|du|der|des|die|und|and|the|&|fuer|für)"
_ACRO_1 = re.compile(r"(" + _ACRO_JOIN + r"(?:[ \-]" + _ACRO_JOIN + r"){0,7})\s*\(([A-Z][A-Za-z0-9&]{1,9})[\);,]")
_ACRO_2 = re.compile(r"\b([A-Z][A-Z0-9&]{1,9})\s*\((" + _ACRO_W + r"(?:[ \-][\w\.\-&']+){0,7})\)")


def acronym_definitions(text: str | None) -> list[tuple[str, str]]:
    """'University Hospital Zurich (USZ' / 'USZ (Universitaetsspital Zuerich)' → [(long form, abbreviation)]."""
    out = []
    if not text:
        return out
    for m in _ACRO_1.finditer(text):
        words, ab = m.group(1).split(), m.group(2)
        if not is_abbrev(ab):
            continue
        for k in range(len(words)):
            cand = " ".join(words[k:])
            if cand.split()[0][:1].isupper() and initials_match(ab, cand):
                out.append((cand, ab))
                break
    for m in _ACRO_2.finditer(text):
        ab, lf = m.group(1), m.group(2).strip()
        if is_abbrev(ab) and initials_match(ab, lf):
            out.append((lf, ab))
    return [(lf, ab) for lf, ab in out if fold(ab).upper() not in _COMMON_ACRONYMS and len(lf) <= 90]


def seed_patterns(store: Store, items: set[tuple[str, str]] | None = None) -> dict:
    """Acronym definitions in item text. A known long form gains the abbreviation; an unknown one becomes a pending
    entity (used only after the LLM confirmed it is an organisation/product)."""
    found: Counter = Counter()
    for kind, iid, text in _item_texts(store, items):
        for lf, ab in acronym_definitions(text):
            found[(lf, ab)] += 1
    added = created = 0
    for (lf, ab), _ in found.most_common():
        if is_rejected(store, lf):
            continue
        eid = _find(store, [lf])
        if eid is not None:
            added += add_alias(store, eid, ab, "abbrev")
            continue
        eid = create_entity(store, "organisation", lf, "pattern", aliases=[(ab, "abbrev")])
        created += 1
    return {"candidates": len(found), "abbrevs_added": added, "pending_created": created}


# ------------------------------------------------------------------ LLM enrichment
PROMPT = """You normalise names of organisations, people and products for a research evidence archive (de/en/fr sources).
Input: a JSON list of objects {"i", "name", "from", "context"}: a raw name, where it was found (publisher field, quote or practice organisation, fact title, quote speaker, or an acronym definition in text) and sometimes the raw text around it.
For each input return one object with:
- "i": the input index
- "name": the input name, copied exactly
- "kind": "organisation" | "person" | "product" | "none" ("none" = not one specific named entity: generic roles like "Report authors", descriptions, topics, concepts, laws, datasets, indices, surveys, metrics)
- "canonical": the standard name of the entity without comments or legal-form noise (e.g. "McKinsey & Company", "University Hospital Zurich"); null if kind is none or the input names several entities
- "aliases": other well-established names of exactly this entity: official German/English/French names and short names people commonly use (e.g. for "University Hospital Basel": "Universitätsspital Basel", "Unispital Basel"). Never: descriptions, city or country names alone, generic words, names of parent, child or partner organisations
- "abbreviations": established acronyms of exactly this entity (e.g. "USB", "HSG", "BCG"); [] if none or unsure
- "ambiguous": the subset of name, aliases and abbreviations that alone usually means something else too (a place, a common word, a person's name, another well-known organisation), e.g. "Zurich" for Zurich Insurance Group
- "parent": the obvious parent organisation (e.g. "McKinsey Global Institute" -> "McKinsey & Company"; the employer of a person if well known), else null
- "parts": if the input names several distinct entities (co-publications "X / Y", "X & Y" when both are organisations, "X with Y, Z"), the canonical names of each of them; else []
- "country": ISO-2 code of the headquarters if well known, else null
Be conservative: when unsure leave a field empty. Do not invent acronyms. "Bain & Company" is one organisation, "Bain & Company / Altagamma" are two.
The canonical name must denote exactly the input entity, never a related one: a children's hospital is not the university hospital, a department or institute is not its university (that is the parent), the Swiss Federal Statistical Office is not Germany's Statistisches Bundesamt. Comments in the input ("via X", "press release", "careers", "cited in Y") are not part of the name.
Answer with JSON: {"entities": [ ... ]} with exactly one object per input."""


class OllamaChat:
    """Minimal Ollama /api/chat client in JSON mode (respects an optional FUNDUS_MAX_MODELS limit)."""

    def __init__(self, model: str = LLM_MODEL, url: str | None = None, max_tokens: int = LLM_MAX_TOKENS,
                 timeout: float = LLM_TIMEOUT):
        from fundus import embed
        self.model = model
        self.url = (url or embed.OLLAMA_URL).rstrip("/")
        self.max_tokens, self.timeout = int(max_tokens), timeout

    def check_capacity(self) -> None:
        from fundus import embed
        if embed.MAX_LOADED_MODELS <= 0:
            return
        loaded = embed.OllamaEmbedder(url=self.url).loaded_models()
        if not embed.capacity_ok(loaded, self.model):
            raise embed.EmbedUnavailable(f"ollama busy: {', '.join(loaded)} loaded; not loading {self.model} (FUNDUS_MAX_MODELS={embed.MAX_LOADED_MODELS})")

    def complete(self, system: str, user: str, schema: dict | None = None) -> dict:
        """JSON answer (with `schema`: constrained to that JSON schema). A runaway answer stops at max_tokens (then it is
        cut JSON → ValueError); a request that waits or runs longer than `timeout` is dropped, which also makes Ollama
        stop generating."""
        body = {"model": self.model, "format": schema or "json", "stream": False, "think": False, "keep_alive": LLM_KEEP_ALIVE,
                # num_ctx: same context as the warm shared gemma4 instance – without it Ollama uses its default context
                # and loads gemma4 a second time (a duplicate copy on the GPU)
                "options": {"temperature": 0, "num_ctx": LLM_NUM_CTX, "num_predict": self.max_tokens},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        req = urllib.request.Request(self.url + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read())
        if data.get("done_reason") == "length":
            raise ValueError(f"answer longer than {self.max_tokens} tokens (model looping) – cut off")
        return json.loads(data["message"]["content"])


def _cache_path(store: Store) -> Path:
    return Path(os.environ.get("FUNDUS_ENTITY_CACHE") or (store.path.parent / "entity_llm_cache.jsonl"))


def _load_cache(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                out[r["key"]] = r["answer"]
            except (ValueError, KeyError):
                continue
    return out


def _llm_input(store: Store, eid: int) -> dict:
    e = store.db.execute("SELECT kind, name FROM entity WHERE id = ?", (eid,)).fetchone()
    srcs = [r for r in store.db.execute("SELECT alias, source FROM entity_alias WHERE entity_id = ? ORDER BY rowid", (eid,))]
    order = ["publisher", "quote.organisation", "practice.organisation", "fact.organisation", "fact.tool", "speaker",
             "pattern", "llm_parent", "llm_part"]
    found = sorted({s for _, s in srcs if s in order}, key=order.index)
    where = {"publisher": "publisher field", "quote.organisation": "quote organisation",
             "practice.organisation": "practice organisation", "fact.organisation": "fact title (organisation)",
             "fact.tool": "fact title (tool/product)", "speaker": "quote speaker", "pattern": "acronym definition in text",
             "llm_parent": "parent organisation", "llm_part": "part of a co-publication"}.get(found[0] if found else "", "name")
    raw = next((a for a, s in srcs if s == "raw"), None)
    abbr = next((a for a, s in srcs if s == "abbrev"), None)
    inp = {"name": e["name"], "from": where}
    ctx = raw or (f"{e['name']} ({abbr})" if abbr and "pattern" in found else None)
    if ctx:
        inp["context"] = ctx[:200]
    return inp


def _cache_key(inp: dict) -> str:
    version = hashlib.sha1(PROMPT.encode()).hexdigest()[:8]     # a changed prompt asks the LLM again
    return json.dumps([LLM_MODEL, version, inp.get("name"), inp.get("from"), inp.get("context")], ensure_ascii=False)


def enrich(store: Store, llm=None, limit_batches: int | None = None, batch_size: int = LLM_BATCH, progress=None) -> dict:
    """LLM alias enrichment for every entity without `enriched_at` (resumable; one transaction per batch). Answers are
    cached in a JSONL file (FUNDUS_ENTITY_CACHE, default next to the database), so rebuilding costs no LLM time."""
    from fundus import embed
    llm = llm or OllamaChat()
    path = _cache_path(store)
    from fundus.store import DEFAULT_DB
    shared = DEFAULT_DB.parent / "entity_llm_cache.jsonl"   # a copy of the archive also reuses the main cache
    cache = {**(_load_cache(shared) if shared != path and not os.environ.get("FUNDUS_ENTITY_CACHE") else {}),
             **_load_cache(path)}
    stats = Counter()
    t0 = time.monotonic()
    llm_secs = 0.0
    failed: Counter = Counter()
    batches = 0
    while True:
        todo = [r[0] for r in store.db.execute("SELECT id FROM entity WHERE enriched_at IS NULL ORDER BY id")]
        todo = [e for e in todo if failed[e] < 2]
        if not todo:
            break
        if limit_batches is not None and batches >= limit_batches:
            stats["stopped"] = "limit"
            break
        chunk = todo[:batch_size]
        inputs = {eid: _llm_input(store, eid) for eid in chunk}
        answers: dict[int, dict] = {}
        ask = [eid for eid in chunk if _cache_key(inputs[eid]) not in cache]
        for eid in chunk:
            if eid not in ask:
                answers[eid] = cache[_cache_key(inputs[eid])]
                stats["cached"] += 1
        if ask:
            if hasattr(llm, "check_capacity"):
                try:
                    llm.check_capacity()
                except embed.EmbedUnavailable as e:
                    stats["stopped"] = str(e)
                    break
            payload = [{"i": n, **inputs[eid]} for n, eid in enumerate(ask)]
            t1 = time.monotonic()
            try:
                res = llm.complete(PROMPT, json.dumps(payload, ensure_ascii=False))
            except Exception as e:    # timeout, bad JSON: retry these entities once more, then give up on them
                for eid in ask:
                    failed[eid] += 1
                stats["errors"] += 1
                stats["last_error"] = f"{type(e).__name__}: {str(e)[:120]}"
                continue
            llm_secs += time.monotonic() - t1
            stats["llm_batches"] += 1
            stats["llm_entities"] += len(ask)
            got = {}
            for a in (res.get("entities") or []) if isinstance(res, dict) else []:
                if isinstance(a, dict) and isinstance(a.get("i"), int) and 0 <= a["i"] < len(ask):
                    eid = ask[a["i"]]
                    if norm(a.get("name") if isinstance(a.get("name"), str) else "") != norm(inputs[eid]["name"]):
                        stats["misaligned"] += 1      # answer belongs to another input (shifted list): ask again
                        continue
                    got[eid] = a
            with path.open("a", encoding="utf-8") as f:
                for eid, a in got.items():
                    key = _cache_key(inputs[eid])
                    cache[key] = a
                    f.write(json.dumps({"key": key, "answer": a}, ensure_ascii=False) + "\n")
            for eid in ask:
                if eid in got:
                    answers[eid] = got[eid]
                else:
                    failed[eid] += 1
                    stats["missing"] += 1
        with store.batch(ACTOR):
            for eid in chunk:
                if eid in answers:
                    stats[apply_answer(store, eid, answers[eid])] += 1
                elif failed[eid] >= 2:      # give up: keep the rule seed as it is
                    store.db.execute("UPDATE entity SET enriched_at = CURRENT_TIMESTAMP WHERE id = ?", (eid,))
                    stats["gave_up"] += 1
        batches += 1
        if progress:
            progress(stats)
    stats["seconds"] = round(time.monotonic() - t0, 1)
    stats["llm_seconds"] = round(llm_secs, 1)
    if stats["llm_batches"]:
        stats["llm_s_per_batch"] = round(llm_secs / stats["llm_batches"], 1)
    return dict(stats)


def plausible_rename(name: str, canonical: str) -> bool:
    """Conservative check that an LLM canonical name still denotes the input: same name, a shared word (≥ 4 letters,
    not a place; prefixes of ≥ 5 letters count: 'Universitätsspital' ~ 'University'), or one is the other's
    abbreviation. Rejects answers that drifted to another entity ('COPE' → 'ICMJE')."""
    a, b = norm(name), norm(canonical)
    if a == b:
        return True
    if is_abbrev(name.strip()) and initials_match(name, canonical) or is_abbrev(canonical.strip()) and initials_match(canonical, name):
        return True
    ta = [t for t in a.split() if len(t) >= 4 and t not in _PLACES]
    tb = [t for t in b.split() if len(t) >= 4 and t not in _PLACES]
    return any(x == y or (len(x) >= 5 and len(y) >= 5 and x[:5] == y[:5]) for x in ta for y in tb)


def _strs(v) -> list[str]:
    if isinstance(v, str):
        v = [v]
    return [s.strip() for s in (v or []) if isinstance(s, str) and s.strip() and len(s.strip()) <= 120]


def apply_answer(store: Store, eid: int, a: dict) -> str:
    """Apply one LLM answer to an entity; returns what happened (enriched | dropped | split | kept)."""
    db = store.db
    e = db.execute("SELECT * FROM entity WHERE id = ?", (eid,)).fetchone()
    if e is None:
        return "gone"
    srcs = {r[0] for r in db.execute("SELECT source FROM entity_alias WHERE entity_id = ?", (eid,))}
    kind = a.get("kind")
    parts = _strs(a.get("parts"))
    done = "UPDATE entity SET enriched_at = CURRENT_TIMESTAMP WHERE id = ?"
    if kind not in KINDS and not parts:
        if "publisher" in srcs:          # every publisher stays an organisation
            db.execute(done, (eid,))
            return "kept"
        delete_entity(store, eid, "llm: not a named entity")
        return "dropped"
    if parts and len(parts) >= 2:
        pk = kind if kind in KINDS and kind != "person" else "organisation"
        targets = []
        parts = [p for p in parts if plausible_rename(e["name"], p) or norm(p) in norm(e["name"])
                 or any(plausible_rename(r[0], p) for r in db.execute("SELECT alias FROM entity_alias WHERE entity_id = ?", (eid,)))]
        for p in parts:
            t = _find(store, [p], pk)
            if t is None:
                if is_rejected(store, p):
                    continue
                t = create_entity(store, pk, p, "llm_part")
            targets.append(t)
        targets = [t for t in dict.fromkeys(targets) if t != eid]
        if not targets:
            db.execute(done, (eid,))
            return "kept"
        aliases = db.execute("SELECT alias FROM entity_alias WHERE entity_id = ?", (eid,)).fetchall()
        for t in targets:
            for (al,) in aliases:
                add_alias(store, t, al, "compound")
        manual = db.execute("SELECT item_kind, item_id, role, source, confidence FROM entity_link WHERE entity_id = ? "
                            "AND source != 'rule'", (eid,)).fetchall()
        for t in targets:
            db.executemany("INSERT OR IGNORE INTO entity_link(entity_id, item_kind, item_id, role, source, confidence) "
                           "VALUES (?,?,?,?,?,?)", [(t, *m) for m in manual])
        names = [db.execute("SELECT name FROM entity WHERE id = ?", (t,)).fetchone()[0] for t in targets]
        db.execute("UPDATE entity SET parent_id = NULL WHERE parent_id = ?", (eid,))
        db.execute("DELETE FROM entity_link WHERE entity_id = ?", (eid,))
        db.execute("DELETE FROM entity_alias WHERE entity_id = ?", (eid,))
        db.execute("DELETE FROM entity WHERE id = ?", (eid,))
        store.log("entity", eid, "split", {"name": e["name"], "into": dict(zip(targets, names))})
        return "split"
    if kind not in KINDS:
        kind = e["kind"]
    canonical = (a.get("canonical") or "").strip() if isinstance(a.get("canonical"), str) else ""
    if canonical and not plausible_rename(e["name"], canonical):
        db.execute(done, (eid,))        # the answer drifted to another entity: keep the rule seed untouched
        store.log("entity", eid, "llm_rejected", {"name": e["name"], "canonical": canonical})
        return "implausible"
    if kind != e["kind"] and not ({"publisher", "quote.organisation", "practice.organisation"} & srcs and kind == "person"):
        db.execute("UPDATE entity SET kind = ? WHERE id = ?", (kind, eid))
    ambiguous = {norm(x) for x in _strs(a.get("ambiguous"))}
    if canonical and len(canonical) <= 120 and norm(canonical) != norm(e["name"]):
        add_alias(store, eid, e["name"], "name")
        db.execute("UPDATE entity SET name = ? WHERE id = ?", (canonical, eid))
    if canonical:
        add_alias(store, eid, canonical, "ambiguous" if norm(canonical) in ambiguous else "name")
    for al in _strs(a.get("aliases")):
        add_alias(store, eid, al, "ambiguous" if norm(al) in ambiguous else "llm")
    for ab in _strs(a.get("abbreviations")):
        if is_abbrev(ab) or re.fullmatch(r"[A-Z][A-Za-z0-9&]{1,9}", ab):
            add_alias(store, eid, ab, "ambiguous" if norm(ab) in ambiguous else "abbrev")
    for an in ambiguous:                 # an existing alias the LLM flags as ambiguous becomes lookup-only
        db.execute("UPDATE entity_alias SET source = 'ambiguous' WHERE entity_id = ? AND alias_norm = ? "
                   "AND source IN ('llm', 'name', 'abbrev', 'pattern')", (eid, an))
    country = a.get("country")
    if isinstance(country, str) and re.fullmatch(r"[A-Za-z]{2}", country.strip()):
        db.execute("UPDATE entity SET country = ? WHERE id = ? AND country IS NULL", (country.strip().upper(), eid))
    parent = a.get("parent") if isinstance(a.get("parent"), str) else None
    if parent and norm(parent) and norm(parent) != norm(canonical or e["name"]) and not e["parent_id"]:
        pid = _find(store, [parent], "organisation")
        if pid is None and not is_rejected(store, parent):
            pid = create_entity(store, "organisation", parent.strip(), "llm_parent")
        if pid and pid != eid and not _is_descendant(store, pid, eid):
            db.execute("UPDATE entity SET parent_id = ? WHERE id = ?", (pid, eid))
    db.execute(done, (eid,))
    return "enriched"


def _is_descendant(store: Store, a: int, b: int) -> bool:
    """True if `a` lies below `b` in the parent chain (setting a as b's parent would make a cycle)."""
    seen, cur = set(), a
    while cur and cur not in seen:
        if cur == b:
            return True
        seen.add(cur)
        r = store.db.execute("SELECT parent_id FROM entity WHERE id = ?", (cur,)).fetchone()
        cur = r[0] if r else None
    return False


def cluster(store: Store) -> list[dict]:
    """Merge duplicates conservatively: same kind and same normalised name, or (organisations/products) one entity's
    name is a specific alias of exactly one other entity. The entity with more links (else the older one) stays."""
    db = store.db
    merges = []

    def weight(eid):
        return (db.execute("SELECT count(*) FROM entity_link WHERE entity_id = ?", (eid,)).fetchone()[0], -eid)

    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for eid, kind, name in db.execute("SELECT id, kind, name FROM entity").fetchall():
        groups[(kind, norm(name))].append(eid)
    for (kind, n), eids in groups.items():
        if len(eids) < 2 or not n:
            continue
        keep = max(eids, key=weight)
        for e in eids:
            if e != keep:
                merges.append(merge(store, e, keep, actor="entities:cluster", reason="same name"))
    r = Resolver(store)
    for eid, kind, name in db.execute("SELECT id, kind, name FROM entity WHERE kind != 'person'").fetchall():
        if eid in r.pending:
            continue
        an = norm(name)
        owners = {o for o in db.execute("SELECT entity_id FROM entity_alias WHERE alias_norm = ? AND entity_id != ? "
                                        "AND source NOT IN ('raw','compound','publisher_alias','ambiguous','common_word','abbrev')",
                                        (an, eid)).fetchall()}
        owners = {o[0] for o in owners}
        if len(owners) != 1:
            continue
        other = next(iter(owners))
        row = db.execute("SELECT kind FROM entity WHERE id = ?", (other,)).fetchone()
        if row is None or row[0] != kind or not db.execute("SELECT 1 FROM entity WHERE id = ?", (eid,)).fetchone():
            continue
        if _is_descendant(store, other, eid) or _is_descendant(store, eid, other):
            continue           # parent and child stay separate
        keep, drop = (other, eid) if weight(other) >= weight(eid) else (eid, other)
        merges.append(merge(store, drop, keep, actor="entities:cluster", reason="name is alias of the other"))
    return merges


def ensure_variants(store: Store) -> int:
    """Add transliteration and short-form variants to aliases stored before these rules existed (idempotent)."""
    n = 0
    for eid, alias, src in store.db.execute("SELECT entity_id, alias, source FROM entity_alias WHERE source NOT IN "
                                            "('raw','compound','publisher_alias')").fetchall():
        n += add_alias(store, eid, alias, src)
    return n


def flag_common_words(store: Store, min_lower: int = 3, share: float = 0.2) -> int:
    """Single-word aliases that the archive mostly uses as ordinary words ('Radiology', 'Regulation', 'Medium') become
    lookup-only ('common_word'): counted over all item texts, a word written in lower case in ≥ `share` of its
    occurrences (and at least `min_lower` times) is not a reliable name. Abbreviations are exempt."""
    rows = store.db.execute("SELECT entity_id, alias, alias_norm FROM entity_alias WHERE source NOT IN "
                            "('raw','compound','publisher_alias','ambiguous','common_word') AND alias_norm NOT LIKE '% %'").fetchall()
    cand = {an for _, alias, an in rows if not is_abbrev(alias) and alias != alias.lower()
            and not re.search(r"[a-z][A-Z]", alias)}          # CamelCase names ('MarTech') are never ordinary words
    if not cand:
        return 0
    low, cap = Counter(), Counter()
    url = re.compile(r"https?://\S+|\b[\w.-]+\.(?:com|org|net|io|ai|de|ch|uk|edu|gov|co)\b\S*|\S+@\S+")
    for _, _, text in _item_texts(store):
        prev_end = True
        for t in tokens(url.sub(" ", text)):
            c = t.casefold()
            if c in cand:
                if t == c:
                    low[c] += 1
                elif not prev_end:
                    cap[c] += 1
            prev_end = False
    common = {c for c in cand if low[c] >= min_lower and low[c] >= share * (low[c] + cap[c])}
    n = 0
    for c in common:
        n += store.db.execute("UPDATE entity_alias SET source = 'common_word' WHERE alias_norm = ? AND source NOT IN "
                              "('raw','compound','publisher_alias','ambiguous','common_word')", (c,)).rowcount
    return n


# ------------------------------------------------------------------ linking
def _item_texts(store: Store, items: set[tuple[str, str]] | None = None):
    """(kind, id, text) for mention scanning; all items, or only the given ones."""
    for kind in LINK_KINDS:
        cols = MENTION_FIELDS[kind]
        sel = f"SELECT id, {', '.join(cols)} FROM {kind}"
        if items is None:
            rows = store.db.execute(sel)
        else:
            ids = [i for k, i in items if k == kind]
            if not ids:
                continue
            rows = []
            for c in range(0, len(ids), 900):
                part = ids[c:c + 900]
                keys = part if kind == "series" else [int(i) for i in part if str(i).isdigit()]
                if keys:
                    rows += store.db.execute(sel + f" WHERE id IN ({','.join('?' * len(keys))})", keys).fetchall()
        for r in rows:
            yield kind, str(r[0]), "\n".join(str(v) for v in r[1:] if v not in (None, ""))


_VIA = [re.compile(p, re.I) for p in (
    r"\(via ([^)]{2,80})\)", r"\bvia ([A-Z][\w&\.\- ]{1,60}?)(?=[,;\.\)]|$| and | data| report)",
    r"\bas cited by ([A-Z][\w&\.\- ]{1,60}?)(?=[,;\.\)]|$)", r"\bcited (?:from|in) ([A-Z][\w&\.\- ]{1,60}?)(?=[,;\.\)]|$)",
    r"underlying source:\s*([^;\.\n\)]{2,80})", r"original source:\s*([^;\.\n\)]{2,80})",
    r"\b([A-Z][\w&\.\-]*(?: [A-Z][\w&\.\-]*){0,4}) via [A-Z]")]


def via_mentions(text: str | None) -> list[str]:
    """Names in sourcing notes: '(via Stanford AI Index)', 'as cited by X', 'Underlying source: X', 'McKinsey via …'."""
    out = []
    for p in _VIA:
        out += [m.strip() for m in p.findall(text or "")]
    return out


def links_for(store: Store, r: Resolver, kind: str, row, pub_ents: dict, ctx: dict) -> dict[tuple[int, str], float]:
    """{(entity id, role): confidence} for one item row."""
    out: dict[tuple[int, str], float] = {}

    def put(eids, role, conf=1.0):
        for e in eids:
            out[(e, role)] = max(out.get((e, role), 0), conf)
    if kind == "series":
        put(pub_ents.get(row["publisher_id"], ()), "publisher")
    elif row["document_id"]:
        put(pub_ents.get(ctx["doc_pub"].get(row["document_id"]), ()), "publisher")
    if kind == "quote":
        put(r.resolve_value(row["organisation"], ("organisation", "product")), "organisation")
        put(r.resolve_value(row["speaker"], ("person",)), "speaker")
    elif kind == "practice":
        put(r.resolve_value(row["organisation"], ("organisation", "product")), "organisation")
    elif kind == "forecast" and row["forecaster"]:
        ents = r.resolve_value(row["forecaster"])
        put({e for e in ents if r.kind.get(e) == "person"}, "speaker")
        put({e for e in ents if r.kind.get(e) != "person"}, "organisation")
    elif kind == "fact" and row["kind"] in ("organisation", "tool"):
        subj = set()
        for cand in (row["title"], clean_name(row["title"], cut_title=True)):
            if cand:
                subj |= r.lookup.get(norm(cand), set())
        put({e for e in subj if e not in r.pending}, "subject")
    text = "\n".join(str(row[c]) for c in MENTION_FIELDS[kind] if c in row.keys() and row[c] not in (None, ""))
    for v in via_mentions(text):           # sourcing chains, mostly in notes ('(via X)', 'Underlying source: X')
        put(r.resolve_value(v), "via", 0.9)
    strong = {e for e, _ in out}
    # mention confidence = prominence: in the headline (first 120 characters of the main field) 0.9, elsewhere in
    # the main field 0.7, only in secondary fields (notes, results, context, lists of surveyed organisations) 0.5
    main = str(row[MENTION_FIELDS[kind][0]] or "")
    head = {e for e, _ in r.mentions(main[:120])}
    in_main = {e for e, _ in r.mentions(main)}
    for eid, _ in r.mentions(text):
        if eid not in strong:
            conf = 0.9 if eid in head else 0.7 if eid in in_main else MINOR_MENTION
            out[(eid, "mentioned")] = max(out.get((eid, "mentioned"), 0), conf)
    return out


MINOR_MENTION = 0.5


def _publisher_entities(store: Store, r: Resolver) -> dict[str, set[int]]:
    out = {}
    for pid, name in store.db.execute("SELECT id, name FROM publisher"):
        ents = set()
        for cand in (name, clean_name(name)):
            if cand:
                ents |= r.lookup.get(norm(cand), set())
        if not ents:
            ents = {e for e, _ in r.mentions(name)}
        ents = {e for e in ents if r.kind.get(e) != "person"}
        if ents:
            out[pid] = ents
    return out


def link(store: Store, items: set[tuple[str, str]] | None = None) -> dict:
    """(Re)compute rule links for the given items, or for all items (full rebuild; also drops orphans).
    Manual and LLM links are kept. Also fills person affiliations (parent) from quotes."""
    t0 = time.monotonic()
    r = resolver(store)
    db = store.db
    pub_ents = _publisher_entities(store, r)
    ctx = {"doc_pub": dict(db.execute("SELECT id, publisher_id FROM document WHERE publisher_id IS NOT NULL").fetchall())}
    n_items = n_links = 0
    roles = Counter()
    if items is None:
        db.execute("DELETE FROM entity_link WHERE source = 'rule'")
    affil: dict[int, Counter] = defaultdict(Counter)
    for kind in LINK_KINDS:
        if items is None:
            rows = db.execute(f"SELECT * FROM {kind}")
        else:
            ids = sorted({i for k, i in items if k == kind})
            rows = []
            for c in range(0, len(ids), 900):
                part = ids[c:c + 900]
                keys = part if kind == "series" else [int(i) for i in part if str(i).isdigit()]
                if keys:
                    rows += db.execute(f"SELECT * FROM {kind} WHERE id IN ({','.join('?' * len(keys))})", keys).fetchall()
            for c in range(0, len(ids), 900):
                part = ids[c:c + 900]
                db.execute(f"DELETE FROM entity_link WHERE source = 'rule' AND item_kind = ? AND item_id IN "
                           f"({','.join('?' * len(part))})", [kind, *part])
        batch = []
        for row in rows:
            iid = str(row["id"])
            ls = links_for(store, r, kind, row, pub_ents, ctx)
            n_items += 1
            for (eid, role), conf in ls.items():
                batch.append((eid, kind, iid, role, conf))
                roles[role] += 1
            if kind == "quote":
                sp = [e for (e, ro) in ls if ro == "speaker"]
                org = [e for (e, ro) in ls if ro == "organisation"]
                if len(sp) == 1 and len(org) == 1:
                    affil[sp[0]][org[0]] += 1
            if len(batch) >= 5000:
                n_links += _insert_links(db, batch)
                batch = []
        n_links += _insert_links(db, batch)
    for person, orgs in affil.items():
        db.execute("UPDATE entity SET parent_id = ? WHERE id = ? AND parent_id IS NULL AND kind = 'person'",
                   (orgs.most_common(1)[0][0], person))
    orphans = 0
    if items is None:
        for kind in LINK_KINDS + ("exhibit",):
            orphans += db.execute(f"DELETE FROM entity_link WHERE item_kind = ? AND item_id NOT IN "
                                  f"(SELECT CAST(id AS TEXT) FROM {kind})", (kind,)).rowcount
    # exhibits (slides) share the entity graph: relink them on a full run, or the ones among `items`
    ex_ids = None if items is None else sorted({int(i) for k, i in items if k == "exhibit" and str(i).isdigit()})
    n_ex = 0
    if ex_ids is None or ex_ids:
        from fundus import exhibits
        n_ex = exhibits.relink_entities(store, ex_ids)
    db.commit()
    return {"items": n_items, "links": n_links, "by_role": dict(roles), "orphans_removed": orphans, "exhibits": n_ex,
            "seconds": round(time.monotonic() - t0, 2)}


def _insert_links(db, batch) -> int:
    if not batch:
        return 0
    before = db.total_changes
    db.executemany("INSERT OR IGNORE INTO entity_link(entity_id, item_kind, item_id, role, source, confidence) "
                   "VALUES (?,?,?,?,'rule',?)", batch)
    return db.total_changes - before


# ------------------------------------------------------------------ incremental maintenance
MARK_CHANGELOG = "entities:changelog_id"
MARK_ALIAS = "entities:alias_rowid"


def _meta(store: Store, key: str) -> int | None:
    r = store.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return int(r[0]) if r and r[0] is not None else None


def _set_meta(store: Store, key: str, value) -> None:
    store.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))


def _fts_items(store: Store, alias_norms: list[str]) -> set[tuple[str, str]]:
    """Items whose full-text body contains one of these aliases (candidates for new mentions)."""
    out: set[tuple[str, str]] = set()
    for an in alias_norms:
        phrase = '"' + an.replace('"', "") + '"'
        try:
            out |= {(k, i) for k, i in store.db.execute("SELECT item_kind, item_id FROM search_index WHERE search_index "
                                                         "MATCH ?", (phrase,))}
        except Exception:
            continue
    return out


def update(store: Store) -> dict:
    """Incremental step for the import maintenance and the embed timer: rule seeds from changed items, pending
    acronym definitions, then relinking of (a) items changed since the last run, (b) items of documents/publishers
    that changed and (c) items containing aliases added since the last run (found via the full-text index).
    First run (no marker): full link."""
    t0 = time.monotonic()
    since = _meta(store, MARK_CHANGELOG)
    alias_mark = _meta(store, MARK_ALIAS)
    top = store.db.execute("SELECT coalesce(max(id), 0) FROM changelog").fetchone()[0]
    out: dict = {}
    with store.batch(ACTOR):
        if since is None:
            out["seed"] = seed(store)
            out["patterns"] = seed_patterns(store)
            out["link"] = link(store)
        else:
            out["seed"] = seed(store, since=since)
            store._changed_since(since)
            changed = {(k, i) for k, i in store.db.execute("SELECT kind, id FROM _chg") if k in LINK_KINDS}
            # documents/publishers touched → their items; merged (deleted) items → drop their links
            for mkind, detail in store.db.execute("SELECT item_kind, detail FROM changelog WHERE id > ? AND action = 'merge' "
                                                  "AND item_kind IN ('observation','forecast','study','quote','practice','fact')",
                                                  (since,)).fetchall():
                try:
                    gone = json.loads(detail or "{}").get("merged")
                except ValueError:
                    gone = None
                if gone is not None and not store.db.execute(f"SELECT 1 FROM {mkind} WHERE id = ?", (int(gone),)).fetchone():
                    store.db.execute("DELETE FROM entity_link WHERE item_kind = ? AND item_id = ?", (mkind, str(gone)))
                    changed.discard((mkind, str(gone)))
            pubs = [r[0] for r in store.db.execute("SELECT DISTINCT item_id FROM changelog WHERE id > ? AND item_kind = 'publisher'",
                                                   (since,))]
            for pid in pubs:
                for kind in ITEM_KINDS:
                    changed |= {(kind, str(i)) for (i,) in store.db.execute(
                        f"SELECT x.id FROM {kind} x JOIN document d ON d.id = x.document_id WHERE d.publisher_id = ?", (pid,))}
                changed |= {("series", s) for (s,) in store.db.execute("SELECT id FROM series WHERE publisher_id = ?", (pid,))}
            out["patterns"] = seed_patterns(store, changed) if changed else {}
            new_aliases = [r[0] for r in store.db.execute("SELECT DISTINCT alias_norm FROM entity_alias WHERE rowid > ?",
                                                          (alias_mark or 0,))]
            cand = _fts_items(store, new_aliases) if new_aliases else set()
            # items already linked to entities whose aliases changed (renames, ambiguity flags)
            todo = changed | cand
            out["link"] = link(store, items=todo) if todo else {"items": 0}
            out["changed_items"], out["alias_candidates"] = len(changed), len(cand)
        _set_meta(store, MARK_CHANGELOG, top)
        _set_meta(store, MARK_ALIAS, store.db.execute("SELECT coalesce(max(rowid), 0) FROM entity_alias").fetchone()[0])
    out["pending_llm"] = store.db.execute("SELECT count(*) FROM entity WHERE enriched_at IS NULL").fetchone()[0]
    out["seconds"] = round(time.monotonic() - t0, 2)
    return out


def build(store: Store, llm=None, use_llm: bool = True, limit_batches: int | None = None, progress=None) -> dict:
    """Full pipeline: rule seeds, acronym definitions, LLM enrichment (new entities only; resumable), duplicate
    clustering, full relink, markers for the incremental step."""
    out: dict = {}
    t0 = time.monotonic()
    top = store.db.execute("SELECT coalesce(max(id), 0) FROM changelog").fetchone()[0]
    with store.batch(ACTOR):
        out["seed"] = seed(store)
        out["patterns"] = seed_patterns(store)
    if use_llm:
        out["llm"] = enrich(store, llm=llm, limit_batches=limit_batches, progress=progress)
    with store.batch("entities:cluster"):
        out["merged"] = len(cluster(store))
        out["variants_added"] = ensure_variants(store)
        out["common_word_aliases"] = flag_common_words(store)
    out["link"] = link(store)
    with store.batch(ACTOR):
        _set_meta(store, MARK_CHANGELOG, top)
        _set_meta(store, MARK_ALIAS, store.db.execute("SELECT coalesce(max(rowid), 0) FROM entity_alias").fetchone()[0])
    out["status"] = status(store)
    out["seconds"] = round(time.monotonic() - t0, 1)
    return out


# ------------------------------------------------------------------ reading
def status(store: Store) -> dict:
    db = store.db
    return {"entities": dict(db.execute("SELECT kind, count(*) FROM entity GROUP BY kind").fetchall()),
            "pending_llm": db.execute("SELECT count(*) FROM entity WHERE enriched_at IS NULL").fetchone()[0],
            "aliases": db.execute("SELECT count(*) FROM entity_alias").fetchone()[0],
            "links": dict(db.execute("SELECT role, count(*) FROM entity_link GROUP BY role").fetchall()),
            "linked_items": db.execute("SELECT count(*) FROM (SELECT DISTINCT item_kind, item_id FROM entity_link)").fetchone()[0],
            "with_parent": db.execute("SELECT count(*) FROM entity WHERE parent_id IS NOT NULL").fetchone()[0],
            "rejected_names": db.execute("SELECT count(*) FROM entity_rejected").fetchone()[0],
            "merges_logged": db.execute("SELECT count(*) FROM changelog WHERE item_kind = 'entity' AND action = 'merge'").fetchone()[0]}


def find(store: Store, name: str) -> list[int]:
    """Entity ids for a name or alias (exact, case/diacritic-insensitive), else the entities named in the text."""
    if not name or not name.strip():
        return []
    if re.fullmatch(r"\d+", name.strip()):
        return [int(name)] if store.db.execute("SELECT 1 FROM entity WHERE id = ?", (int(name),)).fetchone() else []
    rows = [r[0] for r in store.db.execute("SELECT DISTINCT entity_id FROM entity_alias WHERE alias_norm = ? "
                                           "AND source NOT IN ('raw', 'compound', 'publisher_alias')", (norm(name),))]
    if not rows:
        rows = [r[0] for r in store.db.execute("SELECT DISTINCT entity_id FROM entity_alias WHERE alias_norm = ?", (norm(name),))]
    if not rows:
        rows = list(dict.fromkeys(e for e, _ in resolver(store).mentions(name, query=True)))
    return rows
