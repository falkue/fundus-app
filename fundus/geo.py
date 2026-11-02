"""Country/region recognition for the free-text `geo` field: Store tags every item with the matching region slug
(`geo="United States – Texas"` → region tag `us`; `"Singapore"` or `"SG"` → `sg`), so filters by country work no
matter how a source spelled it."""
from __future__ import annotations

import re

# slug: (label, parent region, other spellings)
COUNTRIES = {
    "us": ("United States", "north-america", ["usa", "u.s.", "united states of america", "america"]),
    "ca": ("Canada", "north-america", []),
    "mx": ("Mexico", "latam", []),
    "br": ("Brazil", "latam", []),
    "ar": ("Argentina", "latam", []),
    "cl": ("Chile", "latam", []),
    "co": ("Colombia", "latam", []),
    "pe": ("Peru", "latam", []),
    "uy": ("Uruguay", "latam", []),
    "cr": ("Costa Rica", "latam", []),
    "uk": ("United Kingdom", "europe", ["gb", "great britain", "britain", "england"]),
    "de": ("Germany", "dach", ["deutschland"]),
    "at": ("Austria", "dach", ["österreich"]),
    "ch": ("Switzerland", "dach", ["schweiz", "suisse"]),
    "fr": ("France", "europe", []),
    "it": ("Italy", "europe", []),
    "es": ("Spain", "europe", []),
    "pt": ("Portugal", "europe", []),
    "nl": ("Netherlands", "europe", ["the netherlands", "holland"]),
    "be": ("Belgium", "europe", []),
    "lu": ("Luxembourg", "europe", []),
    "ie": ("Ireland", "europe", []),
    "pl": ("Poland", "europe", []),
    "cz": ("Czech Republic", "europe", ["czechia"]),
    "hu": ("Hungary", "europe", []),
    "ro": ("Romania", "europe", []),
    "bg": ("Bulgaria", "europe", []),
    "gr": ("Greece", "europe", []),
    "cy": ("Cyprus", "europe", []),
    "hr": ("Croatia", "europe", []),
    "si": ("Slovenia", "europe", []),
    "sk": ("Slovakia", "europe", []),
    "lt": ("Lithuania", "europe", []),
    "lv": ("Latvia", "europe", []),
    "ee": ("Estonia", "europe", []),
    "dk": ("Denmark", "nordics", []),
    "se": ("Sweden", "nordics", []),
    "no": ("Norway", "nordics", []),
    "fi": ("Finland", "nordics", []),
    "is": ("Iceland", "nordics", []),
    "tr": ("Turkey", "europe", ["türkiye", "turkiye"]),
    "ru": ("Russia", "europe", ["russian federation"]),
    "ua": ("Ukraine", "europe", []),
    "cn": ("China", "apac", ["mainland china", "prc"]),
    "hk": ("Hong Kong", "apac", []),
    "tw": ("Taiwan", "apac", []),
    "jp": ("Japan", "apac", []),
    "kr": ("South Korea", "apac", ["korea", "republic of korea"]),
    "in": ("India", "apac", []),
    "sg": ("Singapore", "apac", []),
    "id": ("Indonesia", "apac", []),
    "my": ("Malaysia", "apac", []),
    "th": ("Thailand", "apac", []),
    "vn": ("Vietnam", "apac", ["viet nam"]),
    "ph": ("Philippines", "apac", []),
    "au": ("Australia", "apac", []),
    "nz": ("New Zealand", "apac", []),
    "il": ("Israel", "middle-east", []),
    "ae": ("United Arab Emirates", "middle-east", ["uae"]),
    "sa": ("Saudi Arabia", "middle-east", []),
    "qa": ("Qatar", "middle-east", []),
    "za": ("South Africa", "africa", []),
    "ng": ("Nigeria", "africa", []),
    "ke": ("Kenya", "africa", []),
    "eg": ("Egypt", "africa", []),
}
REGIONS = {"global": ["world", "worldwide", "global"], "europe": ["europe", "eu", "eu27", "eu-27", "european union"],
           "north-america": ["north america"], "apac": ["asia", "asia-pacific", "asia pacific", "apac"],
           "latam": ["latin america"], "middle-east": ["middle east", "mena"], "africa": ["africa"],
           "dach": ["dach"], "nordics": ["nordics", "nordic countries"]}

_LOOKUP: dict[str, str] = {}
for _slug, (_label, _parent, _alt) in COUNTRIES.items():
    for _name in [_slug, _label, *_alt]:
        _LOOKUP[_name.lower()] = _slug
for _slug, _names in REGIONS.items():
    for _name in _names:
        _LOOKUP.setdefault(_name, _slug)


def region_slug(geo: str | None) -> str | None:
    """Region slug for a geo text, or None if unclear (several countries, unknown place)."""
    if not geo:
        return None
    g = re.sub(r"\s+", " ", str(geo)).strip().lower()
    if g in _LOOKUP:
        return _LOOKUP[g]
    head = re.split(r"\s+[–—-]\s+|\s*\(|,", g)[0].strip()   # "United States – Texas", "China (mainland)"
    if head in _LOOKUP:
        return _LOOKUP[head]
    if g.startswith(("world", "global")):                     # "World (excl. China)", "Global (11 markets)"
        return "global"
    return None


WORLD = "WORLD"
_SEP_RE = re.compile(r"^(.*?)(\s+[–—-]\s+.*|\s*\(.*|,.*)$")


def canonical_geo(geo: str | None) -> str | None:
    """One notation for the free-text geo field (Falk, 28 Oct 2026: codes): a country as its upper-case code
    ("United States", "USA", "us" → "US"; "Deutschland" → "DE"), the whole world as "WORLD" ("Global", "worldwide").
    A country in front of a detail keeps the detail ("United States – Texas" → "US – Texas", "China (mainland)" →
    "CN (mainland)"). Everything else (several countries, regions such as "Europe", cities) stays as written,
    only with tidy whitespace."""
    if geo is None:
        return None
    g = re.sub(r"\s+", " ", str(geo)).strip()
    if not g:
        return None

    def code(text: str) -> str | None:
        k = text.strip().lower()
        if k in ("world", "worldwide", "global", "globally", "welt", "weltweit"):
            return WORLD
        s = _LOOKUP.get(k)
        return s.upper() if s in COUNTRIES else None
    c = code(g)
    if c:
        return c
    m = _SEP_RE.match(g)
    if m:
        c = code(m.group(1))
        if c and c != WORLD:
            return c + m.group(2)
    return g
