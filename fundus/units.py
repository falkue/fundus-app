"""One notation for units and currencies, applied by Store on every write.

    normalise_unit("USD trillion") -> "USD tn"      normalise_unit("$bn") -> "USD bn"
    normalise_unit("Mio. CHF pro Jahr") -> "CHF m per year"    normalise_unit("percent") -> "%"

Values are never rescaled here; Store.add converts a value only when it joins a series whose unit has the
same currency/base but another scale (e.g. USD m into a USD bn series) and notes the original."""
from __future__ import annotations

import re

CURRENCIES = {"USD", "EUR", "GBP", "CHF", "CAD", "AUD", "CNY", "RMB", "JPY", "KRW", "INR", "SGD", "HKD", "SEK",
              "NOK", "DKK", "PLN", "BRL", "AED", "SAR"}
SYMBOLS = {"us$": "USD", "$": "USD", "€": "EUR", "£": "GBP", "chf": "CHF", "sfr": "CHF", "fr.": "CHF"}
CURRENCY_ALIASES = {"RMB": "CNY", "DOLLAR": "USD", "DOLLARS": "USD", "EURO": "EUR", "EUROS": "EUR"}
SCALES = {"k": "k", "thousand": "k", "thousands": "k", "tsd": "k", "tsd.": "k", "'000": "k",
          "m": "m", "mn": "m", "mio": "m", "mio.": "m", "mil": "m", "mill": "m", "million": "m", "millions": "m",
          "millionen": "m",
          "bn": "bn", "bln": "bn", "b": "bn", "billion": "bn", "billions": "bn", "mrd": "bn", "mrd.": "bn",
          "milliarden": "bn",
          "tn": "tn", "trn": "tn", "tr": "tn", "trillion": "tn", "trillions": "tn", "bio": "tn", "bio.": "tn",
          "billionen": "tn"}
FACTOR = {None: 1, "k": 1e3, "m": 1e6, "bn": 1e9, "tn": 1e12}
PERCENT = {"%", "percent", "per cent", "pct", "pct.", "percentage", "prozent", "% share", "share (%)", "in %"}
POINTS = {"pp", "p.p.", "ppt", "ppts", "pts", "percentage points", "percentage point", "%-points", "% points",
          "prozentpunkte", "pp."}
REST = {"pro jahr": "per year", "p.a.": "per year", "per annum": "per year", "/year": "per year", "/yr": "per year",
        "a year": "per year", "jährlich": "per year"}


def _rest(words: list[str]) -> str:
    r = " ".join(words).strip()
    return REST.get(r.lower(), r)


def parse_unit(unit: str | None) -> tuple[str | None, str | None, str]:
    """(currency, scale, rest) of a normalised or raw unit; rest is the remaining base ('', 'users', '%' …)."""
    if not unit:
        return None, None, ""
    u = re.sub(r"\s+", " ", unit.strip())
    low = u.lower()
    if low in PERCENT:
        return None, None, "%"
    if low in POINTS:
        return None, None, "pp"
    # currency symbol, glued or not: $bn, US$ m, € billion -> parse again with the ISO code
    m = re.match(r"^(us\$|\$|€|£)\s*(.*)$", u, re.I)
    if m:
        return parse_unit(f"{SYMBOLS[m.group(1).lower()]} {m.group(2)}".strip())
    words = u.replace(",", " ").split()
    cur = scale = None
    rest: list[str] = []
    for i, w in enumerate(words):
        wl, wu = w.lower(), w.upper().rstrip(".")
        if cur is None and (wu in CURRENCIES or wu in CURRENCY_ALIASES or wl in SYMBOLS) and not rest:
            cur = CURRENCY_ALIASES.get(wu, wu if wu in CURRENCIES else SYMBOLS.get(wl))
        elif scale is None and wl in SCALES and not rest:
            scale = SCALES[wl]
        else:
            rest = words[i:]
            break
    return cur, scale, _rest(rest)


def normalise_unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    u = re.sub(r"\s+", " ", str(unit)).strip()
    if not u:
        return None
    cur, scale, rest = parse_unit(u)
    if cur is None and scale is None:
        return rest if rest in ("%", "pp") else u
    return " ".join(x for x in (cur, scale, rest) if x)


def conversion(from_unit: str | None, to_unit: str | None) -> float | None:
    """Factor that turns a value in from_unit into to_unit, if both share currency and base and differ only in
    scale (USD m -> USD bn: 0.001). None if not convertible or identical."""
    if not from_unit or not to_unit or from_unit == to_unit:
        return None
    c1, s1, r1 = parse_unit(from_unit)
    c2, s2, r2 = parse_unit(to_unit)
    if c1 != c2 or r1 != r2 or s1 == s2 or (c1 is None and s1 is None) or (c2 is None and s2 is None):
        return None
    return FACTOR[s1] / FACTOR[s2]


def compatible(a: str | None, b: str | None) -> bool:
    """Same unit, or convertible by scale."""
    return not a or not b or a == b or conversion(a, b) is not None
