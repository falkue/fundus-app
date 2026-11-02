"""Imports curated extraction files `extracts/*.json` (evidence pulled out of prose research reports).

File format (one file per source project):

{
  "project": "future-cmo",                       # origin project slug
  "source_root": "/path/to/source-project",   # informational only (not read by the importer)
  "extracted_at": "2026-10-24",
  "vocab": [{"facet": "topic", "value": "...", "label": "...", "parent": null, "description": "..."}],
  "items": [
    {
      "kind": "observation" | "forecast" | "study" | "quote" | "practice" | "fact",
      "key": "stable id within this file",
      "source_file": "research/01-budgets.md",     # relative to source_root
      "fields": {... columns of the kind's table, see fundus/schema.sql ...},
      "document": {"title": "...", "url": "...", "publisher": "...", "publisher_type": "consultancy",
                   "kind": "report", "published": "2025-05", "doi": null, "authors": null, "is_vendor": 0},
      "series": {"id": "...", "name": "...", "unit": "%", "frequency": "annual", "update_hint": "..."},
      "tags": {"industry": [...], "topic": [...], "region": [...]}
    }
  ]
}

`series` is optional; when present the item's fields.series_id is set to it. `"refresh": true` in the series block
makes this file authoritative for the series' name/unit/comparability (overwrites what another source stored).
"""
from __future__ import annotations

import json
from pathlib import Path

PROJECT = "extracts"
SOURCE_ROOT = str(Path(__file__).resolve().parent.parent / "extracts")
DOC_FIELDS = ("doi", "kind", "published", "authors", "language", "access", "is_vendor", "notes")


def run(store, root=SOURCE_ROOT) -> dict:
    from fundus.fingerprint import remember, source_fingerprint, unchanged
    counts: dict[str, int] = {}
    for path in sorted(Path(root).glob("*.json")):
        fp = source_fingerprint([path], __file__)
        if unchanged(store, f"extracts:{path.name}", fp):   # file and import rules unchanged since the last import
            counts["files_unchanged"] = counts.get("files_unchanged", 0) + 1
            continue
        data = json.loads(path.read_text())
        project = data["project"]
        with store.batch(f"importer:extracts:{project}"):
            for v in data.get("vocab") or []:
                store.add_vocab(v["facet"], v["value"], v["label"], v.get("parent"), v.get("description"))
            for item in data["items"]:
                fields = dict(item["fields"])
                doc = item.get("document")
                if doc and (doc.get("title") or doc.get("url")):
                    pub = doc.get("publisher")
                    if pub:
                        store.publisher(pub, type=doc.get("publisher_type"))
                    fields["document_id"] = store.document(
                        doc.get("title") or doc["url"], url=doc.get("url"), publisher=pub,
                        **{k: doc[k] for k in DOC_FIELDS if doc.get(k) not in (None, "")})
                ser = item.get("series")
                if ser:
                    s = dict(ser)
                    refresh = bool(s.pop("refresh", False))   # true: this file is authoritative for the series' metadata
                    fields["series_id"] = store.series(s.pop("id"), s.pop("name"), publisher=(doc or {}).get("publisher"),
                                                       refresh=refresh, **s)
                store.add(item["kind"], fields, tags=item.get("tags"),
                          origin=(project, item.get("source_file", ""), item["key"]))
                counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        remember(store, f"extracts:{path.name}", fp)
    return counts


if __name__ == "__main__":
    from importers._cli import main
    main(run)
