"""`fundus` command line.

    fundus [--db PATH] stats | search TEXT [filters] | get KIND ID | series ID | overview [--industry X]
           | due [--older-than DAYS] | forecasts [--due] [--apply] | resolve ID OUTCOME [...]
           | import NAME|all [--root PATH] | reindex | export --format jsonl|csv [--out FILE]
           | exhibits import DIR | sync | fold-versions [--apply] | deck set|list|show|delete|pdf … | add HTML PNG | list | show REF | versions REF | share REF | policy set MODE|show
           | exhibits set create PROJECT "TITLE" --folder REL [--folder …] [--slug S] | set list [PROJECT]
             | set show PROJECT/SET | set folder add|remove PROJECT/SET REL | set rename PROJECT/SET "TITLE" [--slug S]
             | set sharing PROJECT/SET shared|project|inherit
           | project list | project title PROJECT "NAME" (empty name: remove; the slug stays the identity)
           | serve [--host H] [--port P] [--scheduler auto|on|off] [--log-file F]   (the server, fundus/mcp_server.py)
           | render HTML [PNG] … [--mode M] [--scale N] [--no-register|--dry-run|--wait]   (fundus/render.py)
           | service install|uninstall|status [--dry-run] [--force] [--host H] [--port P]   (fundus/service.py)
           | scheduler status | scheduler run JOB   (fundus/scheduler.py)
"""
from __future__ import annotations

import argparse
import csv
import importlib
import json
import sqlite3
import pkgutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fundus import forecasts as fc  # noqa: E402
from fundus import query as q  # noqa: E402
from fundus.store import DEFAULT_DB, ITEM_KINDS, Store  # noqa: E402


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def importer_names() -> list[str]:
    """Public importer modules (importers/*.py not starting with '_') that define run()."""
    import importers
    names = []
    for m in sorted(pkgutil.iter_modules(importers.__path__), key=lambda m: m.name):
        if m.name.startswith("_") or m.ispkg:
            continue
        mod = importlib.import_module(f"importers.{m.name}")
        if callable(getattr(mod, "run", None)):
            names.append(m.name)
    return names


def run_import(st: Store, name: str, root: str | None = None) -> dict:
    names = importer_names() if name == "all" else [name]
    if name == "all" and root:
        raise SystemExit("--root only works with a single importer")
    out = {}
    for n in names:
        mod = importlib.import_module(f"importers.{n}")
        if not callable(getattr(mod, "run", None)):
            raise SystemExit(f"importers.{n} has no run()")
        if name != "all":
            out[n] = mod.run(st, root) if root else mod.run(st)
            continue
        try:  # one broken source must not stop the others; its batch is rolled back
            out[n] = mod.run(st)
        except Exception as e:
            out[n] = {"error": f"{type(e).__name__}: {e}"}
    return out


def snapshot(st: Store, target: str | None = None) -> dict:
    """Consistent copy of the live database (SQLite backup API) for the file-level restic backup."""
    import sqlite3
    from pathlib import Path
    dst = Path(target) if target else st.path.parent / "backup" / "fundus.sqlite"
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    with sqlite3.connect(tmp) as out:
        st.db.backup(out)
    tmp.replace(dst)
    return {"snapshot": str(dst), "bytes": dst.stat().st_size}


EXPORT_COLS = ["kind", "id", "label", "value", "value_text", "value_low", "value_high", "unit", "period", "geo",
               "status", "visibility", "series_id", "publisher", "document_title", "url", "published",
               "industry", "topic", "region", "project"]


def export_rows(st: Store, include_internal: bool = False, kinds=ITEM_KINDS):
    """Yield one dict per item: all non-empty fields + document/publisher + tags."""
    for kind in kinds:
        vis = "" if include_internal else " WHERE visibility != 'internal'"
        for r in st.db.execute(f"SELECT * FROM {kind}{vis} ORDER BY id"):
            row = {k: r[k] for k in r.keys() if r[k] not in (None, "")}
            item = {"kind": kind, **row}
            if r["document_id"]:
                d = st.db.execute("SELECT d.title, d.url, d.published, d.kind, d.doi, d.journal, p.name AS publisher FROM document d "
                                  "LEFT JOIN publisher p ON p.id = d.publisher_id WHERE d.id = ?", (r["document_id"],)).fetchone()
                if d:
                    item["document"] = {k: d[k] for k in d.keys() if d[k] not in (None, "")}
            item["tags"] = q.tags_of(st, kind, r["id"])
            yield item


def _csv_row(item: dict) -> dict:
    kind = item["kind"]
    doc = item.get("document", {})
    tags = item.get("tags", {})
    period = item.get("period") or item.get("target_period") or item.get("said_on") or item.get("year") or item.get("date")
    return {"kind": kind, "id": item["id"], "label": item.get(q.LABEL_COL[kind]), "value": item.get("value"),
            "value_text": item.get("value_text"), "value_low": item.get("value_low"), "value_high": item.get("value_high"),
            "unit": item.get("unit"), "period": period, "geo": item.get("geo") or item.get("region") or item.get("country"),
            "status": item.get("status"), "visibility": item.get("visibility"), "series_id": item.get("series_id"),
            "publisher": doc.get("publisher"), "document_title": doc.get("title"), "url": doc.get("url"),
            "published": doc.get("published"),
            **{f: ";".join(tags.get(f, [])) for f in ("industry", "topic", "region", "project")}}


def embed_defaults() -> str:
    from fundus import embed
    return embed.default_space().key


def cmd_embed(st: Store, a) -> int:
    """`fundus embed`: incremental embedding run (default), or space management (--status/--activate/...)."""
    from fundus import embed
    base = embed.active_space(st) or embed.default_space()
    given = a.model or a.dim or a.quant
    space = embed.Space(a.model or base.model, a.dim or base.dim, a.quant or base.quant)
    if a.status:
        _print(embed.status(st))
        return 0
    if a.drop:
        if not given:
            raise ValueError("--drop needs --model/--dim/--quant naming the space")
        _print(embed.drop_space(st, space))
        return 0
    if a.derive_from:
        if not given:
            raise ValueError("--derive-from needs --dim and/or --quant naming the target space")
        _print(embed.derive_space(st, embed.Space.parse(a.derive_from), space))
        return 0
    if a.rebuild_index:
        _print(embed.rebuild_index(st, space))
        return 0
    if a.activate:
        embed.set_active(st, space)
        _print(embed.status(st))
        return 0
    def progress(done, total):
        if done % 960 == 0 or done == total:
            print(f"embedded {done}/{total}", file=sys.stderr, flush=True)
    res = embed.embed_pending(st, limit=a.limit, all_items=a.all, space=space if given else None, progress=progress)
    try:
        res["search_log_pruned"] = embed.prune_search_log(st, a.keep_log_days)
    except sqlite3.OperationalError as e:    # the nightly import holds the write lock: prune next round
        res["search_log_pruned"] = f"skipped: {e}"
    try:   # the embed timer also links items written by sessions since the last run to entities (cheap, no LLM)
        from fundus import entities
        res["entities"] = {k: v for k, v in entities.update(st).items() if k in ("changed_items", "link", "pending_llm", "seconds")}
    except Exception as e:
        st.db.rollback()
        res["entities"] = {"error": f"{type(e).__name__}: {e}"}
    _print(res)
    return 1 if "error" in res else 0


def cmd_entities(st: Store, a) -> int:
    from fundus import entities
    if a.action == "build":
        def progress(s):
            print(json.dumps({k: v for k, v in s.items() if k != "last_error"}), file=sys.stderr, flush=True)
        _print(entities.build(st, use_llm=not a.no_llm, limit_batches=a.limit, progress=progress))
    elif a.action == "link":
        _print(entities.link(st) if a.full else entities.update(st))
    elif a.action == "update":
        _print(entities.update(st))
    elif a.action == "status":
        _print(entities.status(st))
    elif a.action == "show":
        if not a.args:
            raise ValueError("show needs a NAME")
        _print(q.entity_profile(st, " ".join(a.args), include_internal=a.include_internal))
    elif a.action == "merge":
        if len(a.args) != 2:
            raise ValueError("merge needs SRC DST (entity ids or names)")
        ids = []
        for x in a.args:
            found = entities.find(st, x)
            if len(found) != 1:
                raise ValueError(f"'{x}' matches {len(found)} entities – use the id")
            ids.append(found[0])
        with st.batch("cli"):
            res = entities.merge(st, ids[0], ids[1], actor="cli")
        res["undo"] = "fundus entities unmerge " + str(st.db.execute(
            "SELECT max(id) FROM changelog WHERE item_kind = 'entity' AND action = 'merge'").fetchone()[0])
        _print(res)
    elif a.action == "unmerge":
        if len(a.args) != 1:
            raise ValueError("unmerge needs the CHANGELOG_ID of the merge (see 'fundus entities status')")
        with st.batch("cli"):
            _print(entities.unmerge(st, int(a.args[0])))
    return 0


def cmd_exhibits(st: Store, a) -> int:
    """`fundus exhibits …`: slides/charts (fundus/exhibits.py)."""
    import os
    from datetime import datetime
    from fundus import exhibits as ex
    args = a.args
    if a.action == "deck":
        from fundus import decks
        usage = ('deck set PROJECT NAME PNG|REF [PNG|REF …] [--title T] [--set S] [--pdf FILE] | deck list [PROJECT] | '
                 'deck show PROJECT/NAME|ID | deck delete PROJECT/NAME|ID | deck pdf FILE [--project P] [--name N] [--force]')
        sub_, rest = (args[0], args[1:]) if args else ("list", [])
        if sub_ == "set" and len(rest) >= 3:
            _print(decks.set_sequence(st, rest[0], rest[1], rest[2:], title=a.title, set_ref=a.set, pdf=a.pdf, actor="cli"))
        elif sub_ == "list":
            _print(decks.list_sequences(st, rest[0] if rest else a.project))
        elif sub_ == "show" and len(rest) == 1:
            _print(decks.get_sequence(st, rest[0], project=a.project))
        elif sub_ == "delete" and len(rest) == 1:
            _print(decks.delete_sequence(st, rest[0], project=a.project, actor="cli"))
        elif sub_ == "pdf" and len(rest) == 1:
            _print(decks.import_pdf(st, rest[0], project=a.project, name=a.name, force=a.force, actor="cli"))
        else:
            raise ValueError(usage)
    elif a.action == "fold-versions":
        _print(ex.fold_script_versions(st, apply=a.apply))
    elif a.action == "sync":
        _print(ex.sync_projects(st))
    elif a.action == "import":
        if len(args) != 1:
            raise ValueError("import needs a DIR")
        def progress(done, total):
            print(f"registered {done}/{total}", file=sys.stderr, flush=True)
        res = ex.import_dir(st, args[0], project=a.project, deck=a.deck, measure=a.measure, progress=progress, set_ref=a.set)
        if not a.verbose:
            res["skipped"] = {"count": len(res["skipped"]),
                              "by_reason": {r: sum(1 for s in res["skipped"] if s["reason"] == r)
                                            for r in sorted({s["reason"] for s in res["skipped"]})}}
        _print(res)
    elif a.action == "add":
        if len(args) != 2:
            raise ValueError("add needs HTML PNG")
        stamp = datetime.now().isoformat(timespec="seconds")
        if a.hook and not ex.under_projects(args[0]) and os.environ.get("FUNDUS_EXHIBIT_FORCE") != "1":
            print(json.dumps({"ts": stamp, "skipped": "outside ~/Projects (FUNDUS_EXHIBIT_FORCE=1 registers anyway)",
                              "html": args[0]}))
            return 0
        dom = None
        if a.dom:
            try:
                dom = Path(a.dom).read_text(encoding="utf-8", errors="replace")
            finally:
                if a.hook:
                    Path(a.dom).unlink(missing_ok=True)
        res = ex.register(st, args[0], args[1], script=a.script, project=a.project, deck=a.deck, note=a.note or "",
                          dom=dom, actor="exhibits:hook" if a.hook else "cli", set_ref=a.set,
                          caller=a.caller)
        print(json.dumps({"ts": stamp, **res}, ensure_ascii=False) if a.hook else json.dumps(res, ensure_ascii=False, indent=2))
    elif a.action == "list":
        _print(ex.listing(st, project=a.project, kind=a.kind, limit=a.limit, include_internal=a.include_internal or bool(a.project),
                          set_ref=a.set))
    elif a.action in ("show", "versions"):
        if len(args) != 1:
            raise ValueError(f"{a.action} needs an exhibit id, project/slug or slug")
        fn = ex.get if a.action == "show" else ex.versions
        kw = {"script": not a.no_script} if a.action == "show" else {}
        _print(fn(st, args[0], include_internal=True, project=a.project, **kw))
    elif a.action in ("share", "unshare"):
        if len(args) != 1:
            raise ValueError(f"{a.action} needs an exhibit id or project/slug")
        _print(ex.share(st, args[0], released=a.action == "share"))
    elif a.action == "policy":
        if args[:1] == ["set"] and len(args) == 2:
            _print(ex.set_policy(st, args[1]))
        elif args in ([], ["show"]):
            n = dict(st.db.execute("SELECT visibility, count(*) FROM exhibit GROUP BY visibility").fetchall())
            _print({"policy": ex.policy(st), "modes": list(ex.POLICIES), "public": n.get("public", 0),
                    "internal": n.get("internal", 0),
                    "released": st.db.execute("SELECT count(*) FROM exhibit WHERE released = 1").fetchone()[0]})
        else:
            raise ValueError("policy set share_everything|internal_default|auto_sources | policy show")
    elif a.action == "relink":
        with st.batch("cli"):
            n = ex.relink_entities(st)
        _print({"relinked_exhibits": n})
    elif a.action == "set":
        return _cmd_exhibit_set(st, a, ex)
    elif a.action in ("lock", "unlock", "locks", "copy"):
        return _cmd_exhibit_lock(st, a, ex)
    elif a.action == "files":
        from fundus import slide_search
        _print(slide_search.slide_files(st, a.args, version=a.version))
    return 0


def _cmd_exhibit_lock(st: Store, a, ex) -> int:
    """`fundus exhibits lock|unlock|locks|copy …`: archive locks and copies (fundus.locks)."""
    from fundus import locks
    args = a.args
    if a.action == "locks":
        _print({"active": locks.states(st, args[0] if args else a.project), "history": locks.history(st, args[0] if args else a.project)})
        return 0
    if len(args) != 2 or args[0] not in ("set", "slide"):
        raise ValueError(f"{a.action} set PROJECT/SET | {a.action} slide REF")
    kw = {"set_ref": args[1]} if args[0] == "set" else {"exhibit": args[1]}
    if a.action == "lock":
        _print(locks.lock(st, project=a.project, note=a.note, **kw))
    elif a.action == "unlock":
        # Falk's explicit decision: only interactively, with the name typed back – never from a script or an agent
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise PermissionError("unlock only interactively in a terminal (or in the portal) – it is Falk's decision")
        word = args[1].rstrip("/").split("/")[-1]
        typed = input(f"Sperre von {args[0]} '{args[1]}' aufheben? Zum Bestätigen '{word}' eintippen: ").strip()
        if typed != word:
            print("abgebrochen – nichts geändert")
            return 1
        _print(locks.unlock(st, project=a.project, note=a.note, actor="falk:cli", **kw))
    elif args[0] == "slide":
        _print(locks.copy_exhibit(st, args[1], to_set=a.set, slug=a.slug, folder=(a.folder or [None])[0], project=a.project))
    else:
        if not a.title or not a.folder:
            raise ValueError("copy set REF --title 'Neuer Satz' --folder REL")
        _print(locks.copy_set(st, args[1], a.title, a.folder[0], slug=a.slug, project=a.project))
    return 0


def _cmd_exhibit_set(st: Store, a, ex) -> int:
    """`fundus exhibits set …`: slide sets (Foliensätze) of a project."""
    sub, args = (a.args[0] if a.args else "list"), a.args[1:]
    usage = ('set create PROJECT "TITLE" --folder REL [--folder …] [--slug S] [--note N] | set list [PROJECT] | '
             'set show PROJECT/SET | set folder add|remove PROJECT/SET REL | set rename PROJECT/SET "TITLE" [--slug S] | '
             'set sharing PROJECT/SET shared|project|inherit')
    if sub == "create" and len(args) == 2:
        _print(ex.create_set(st, args[0], args[1], a.folder or [], slug=a.slug, note=a.note))
    elif sub == "list" and len(args) <= 1:
        _print(ex.list_sets(st, args[0] if args else a.project))
    elif sub == "show" and len(args) == 1:
        _print(ex.show_set(st, args[0], project=a.project))
    elif sub == "folder" and len(args) == 3 and args[0] in ("add", "remove"):
        fn = ex.add_set_folder if args[0] == "add" else ex.remove_set_folder
        _print(fn(st, args[1], args[2], project=a.project))
    elif sub == "rename" and 1 <= len(args) <= 2:
        _print(ex.rename_set(st, args[0], title=args[1] if len(args) == 2 else None, slug=a.slug, note=a.note, project=a.project))
    elif sub == "sharing" and len(args) == 2:
        _print(ex.set_set_sharing(st, args[0], None if args[1] == "inherit" else args[1], project=a.project))
    else:
        raise ValueError(usage)
    return 0


def cmd_journals(st: Store, a) -> int:
    """`fundus journals`: fill the journal of documents with a DOI from Crossref (container title). Changes are logged."""
    import time
    from fundus import crossref
    rows = st.db.execute("SELECT id, doi FROM document WHERE coalesce(doi, '') <> '' AND coalesce(journal, '') = '' "
                         "ORDER BY id LIMIT ?", (a.limit,)).fetchall()
    done, missing = [], []
    for r in rows:
        meta = crossref.lookup(r["doi"])
        if meta and meta.get("journal"):
            done.append({"doc": r["id"], "doi": r["doi"], "journal": meta["journal"]})
            if not a.dry_run:
                with st.batch("cli:crossref"):
                    st.update("document", r["id"], {"journal": meta["journal"]})
        else:
            missing.append(r["id"])
        time.sleep(0.1)   # polite to the public API
    _print({"checked": len(rows), "filled": len(done), "dry_run": a.dry_run, "no_journal": missing, "documents": done})
    return 0


def cmd_tidy(st: Store, a) -> int:
    """`fundus tidy …`: the standard data-hygiene procedure (fundus/tidy.py, docs/DATA-STANDARD.md)."""
    from fundus import tidy
    c = a.tidy_cmd
    if c == "run":
        _print(tidy.run(st, units=not a.no_units, actor=a.actor, backup=not a.no_backup))
    elif c == "units":
        _print(tidy.apply_units(st, actor=a.actor) if a.apply else {"plan": tidy.unit_plan(st)})
    elif c == "review":
        out = tidy.review(st, limit=a.limit)
        text = json.dumps(out, ensure_ascii=False, indent=1, default=str)
        if a.out:
            Path(a.out).write_text(text)
            print(f"{len(out)} findings -> {a.out}", file=sys.stderr)
        else:
            print(text)
    elif c == "decide":
        if not a.file:
            raise ValueError("tidy decide FILE (JSON list of {id, action, keep?, why})")
        _print(tidy.decide(st, json.loads(Path(a.file).read_text()), actor=a.actor))
    elif c == "report":
        _print(tidy.report(st))
    return 0


def cmd_cleanup(st: Store, a) -> int:
    """`fundus cleanup …`: analyze (nightly timer), status, candidates, apply (automatic classes), decide, undo, runs."""
    from fundus import cleanup
    c = a.cleanup_cmd
    if c == "analyze":
        r = cleanup.analyze(st, actor="cli")
        _print({k: r[k] for k in ("run", "score", "affected", "total", "auto_records", "open_decisions", "classes")})
    elif c == "status":
        _print(cleanup.status(st))
    elif c == "candidates":
        _print(cleanup.candidates(st, a.cls, a.state, a.limit))
    elif c == "apply":
        s = cleanup.analyze(st, actor="cli")
        if not a.yes:
            print(f"{s['auto_records']} records in {sum(s['classes'][k]['groups'] for k in cleanup.AUTO)} findings "
                  "(variants, safe duplicates, orphans) would be changed. Run again with --yes.", file=sys.stderr)
            return 1
        _print(cleanup.apply(st, actor="cli", backup=not a.no_backup))
    elif c == "decide":
        if not a.ids or not a.action:
            raise ValueError("decide ID… --action merge|delete|dispute|reject|later|reopen [--keep ID]")
        _print(cleanup.decide(st, a.ids, a.action, keep=a.keep, actor="cli"))
    elif c == "undo":
        if len(a.ids) != 1:
            raise ValueError("undo RUN")
        _print(cleanup.undo(st, int(a.ids[0]), actor="cli"))
    elif c == "runs":
        _print(cleanup.runs(st, a.limit))
    return 0


def cmd_photos(st: Store, a) -> int:
    from fundus import photos
    if a.photos_cmd == "import":
        def progress(n, total, f):
            print(f"\r{n}/{total} {Path(f).name[:60]:60}", end="", file=sys.stderr, flush=True)
        res = photos.import_paths(st, a.paths, survey=a.survey, project=a.project, period=a.period, place=a.place,
                                  consent=a.consent, usage=a.usage, album=a.album, title=a.title, note=a.note,
                                  recursive=not a.flat, progress=progress)
        print(file=sys.stderr)
        res["ids"] = f"{len(res['ids'])} ids"
        res["pending_annotation"] = len(photos.pending(st))
        _print(res)
    elif a.photos_cmd == "annotate":
        def progress(n, total, pid):
            print(f"\r{n}/{total} photo {pid}", end="", file=sys.stderr, flush=True)
        if a.ids:
            _print([photos.annotate_one(st, i) for i in a.ids])
        else:
            _print(photos.annotate_pending(st, limit=a.limit, retry_failed=a.retry_failed, progress=progress))
    elif a.photos_cmd == "search":
        res = photos.search(st, " ".join(a.text), purpose=a.purpose, limit=a.limit, media=a.media)
        _print({"total": res["total"], "hidden_by_rights": res["hidden_by_rights"],
                "photos": [{k: p[k] for k in ("ref", "caption", "cleared_for", "slide_url")} for p in res["photos"]]})
    elif a.photos_cmd == "show":
        _print(photos.get(st, a.ref))
    elif a.photos_cmd == "stats":
        _print({**photos.stats(st), "groups": photos.groups(st)})
    elif a.photos_cmd == "embed":
        _print({"embedded": photos.embed_pending(st)})
    return 0


def cmd_review(st: Store, a) -> int:
    from fundus import reviews as rv
    act, args = a.action, a.args
    one = lambda: args[0] if len(args) == 1 else (_ for _ in ()).throw(ValueError(f"review {act} needs exactly one argument"))
    if act in ("share", "gemini"):
        if len(args) != 1:
            raise ValueError(f"review {act} PROJECT --slides … | --set … | --deck … [--person 'Name, Vorname']")
        out = rv.create(st, args[0], slides=a.slides, set_ref=a.set, deck=a.deck, title=a.title,
                        people=a.person if act == "share" else [], gemini=act == "gemini", focus=a.focus, model=a.model)
        if act == "gemini" and not a.no_run:
            from fundus import gemini as gm
            out["run"] = gm.run_review(st.path, out["id"])
        _print(out)
    elif act in ("loop", "loop-status", "loop-stop"):
        from fundus import review_loop as rl
        if act == "loop":
            if len(args) != 1:
                raise ValueError("review loop PROJECT --slides … | --set … | --deck … [--iterations N]")
            out = rl.start(st, args[0], slides=a.slides, set_ref=a.set, deck=a.deck, iterations=a.iterations, focus=a.focus,
                           model=a.model, by="cli")
            out["note"] = "queued – the Fundus server starts round 1 within 20 s"
            _print(out)
        elif act == "loop-stop":
            _print(rl.stop(st, int(one()) if args else rl.status(st, None, project=a.project)["loop"]))
        else:
            ref = int(args[0]) if args else None
            _print(rl.wait(st.path, ref or rl.status(st, None, project=a.project)["loop"]) if a.wait
                   else rl.status(st, ref, project=a.project))
    elif act == "models":
        from fundus import gemini as gm
        _print(gm.models())
    elif act == "sync":
        from fundus import review_sync as rs
        from fundus.firebase import Client
        if a.loop:
            rs.loop(st.path, interval=a.interval)
        _print(rs.cycle(st, Client()))
    elif act == "unpublish":
        from fundus import review_sync as rs
        from fundus.firebase import Client
        _print(rs.unpublish(st, Client(), st.db.execute("SELECT * FROM review WHERE id = ?", (int(one()),)).fetchone()))
    elif act == "run-gemini":
        from fundus import gemini as gm
        _print(gm.run_review(st.path, int(one()), retry=True))
    elif act == "list":
        _print(rv.list_reviews(st, args[0] if args else a.project))
    elif act == "show":
        _print(rv.get(st, one()))
    elif act == "invite":
        _print(rv.invite(st, one(), a.person))
    elif act == "revoke":
        _print(rv.revoke(st, one(), a.person[0] if a.person else None))
    elif act == "close":
        _print(rv.close(st, one()))
    elif act == "people":
        _print(rv.list_reviewers(st, include_ai=True))
    elif act == "feedback":
        _print(rv.inbox(st, project=a.project, set_ref=a.set, review=a.review, status=a.status))
    elif act == "resolve":
        _print(rv.resolve(st, args, a.outcome or "", a.note or "", version=a.version, by="cli", actor="cli"))
    elif act == "reopen":
        _print(rv.reopen(st, one(), a.note, actor="cli"))
    elif act in ("link", "unlink"):
        if len(args) < 2:
            raise ValueError(f"review {act} EXHIBIT KIND:ID …")
        _print(rv.link_items(st, args[0], args[1:], remove=act == "unlink"))
    elif act == "evidence":
        _print(rv.slide_evidence(st, one()))
    elif act == "check-links":
        _print(rv.check_links(st, rv.review_urls(st, int(one()))))
    return 0


def _redirect_output(log_file: str | None) -> None:
    """`serve --log-file`: append stdout/stderr to a file. Also when there is no console at all (pythonw.exe on
    Windows has sys.stdout = None, which logging would trip over): then into <data dir>/logs/server.log."""
    if not log_file and sys.stdout is not None and sys.stderr is not None:
        return
    from fundus import config
    path = Path(log_file) if log_file else config.data_dir() / "logs" / "server.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr = f


def cmd_platform(a) -> int:
    """serve, render, service, scheduler: the commands that make Fundus run on Linux, macOS and Windows."""
    if a.cmd == "serve":
        _redirect_output(a.log_file)
        from fundus import mcp_server
        argv = (["--db", a.db] if a.db else []) + (["--host", a.host] if a.host else []) + \
               (["--port", str(a.port)] if a.port else []) + ["--scheduler", a.scheduler]
        mcp_server.main(argv)
        return 0
    if a.cmd == "render":
        from fundus import render
        return render.run_cli(a, db=a.db)
    if a.cmd == "service":
        from fundus import service
        try:
            res = service.main(a.service_cmd, dry_run=a.dry_run, force=a.force, host=a.host, port=a.port,
                               with_review_sync=a.with_review_sync)
        except (PermissionError, ValueError, OSError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if a.dry_run:
            for path, content in res.pop("contents", {}).items():
                print(f"# ---- {path}\n{content}")
            for c in res["commands"]:
                print(f"$ {c}")
            for n in res["notes"]:
                print(f"# {n}")
            return 0
        _print(res)
        return 0 if res.get("ok", True) else 1
    from fundus import scheduler
    db = a.db or str(DEFAULT_DB)
    if a.sched_cmd == "status":
        _print(scheduler.status(db))
        return 0
    if not a.job:
        print("usage: fundus scheduler run import|cleanup|embed|snapshot", file=sys.stderr)
        return 2
    try:
        res = scheduler.run_now(a.job, db)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _print(res)
    return 0 if res["last_result"] == "success" else 1


def cmd_research(st: Store, a) -> int:
    """fundus research list|show|search|worklist|series|import|reindex (fundus/research.py, fundus/research_import.py)."""
    from fundus import research as rs
    c, args = a.research_cmd, a.args
    st.actor = "cli"
    if c == "import":
        from fundus import research_import as ri
        if not args:
            raise ValueError("research import PATH (e.g. ~/Projects/Software/Fundus-Data/_analysen) [--dry-run]")
        res = ri.run(st, args[0], dry_run=a.dry_run)
        if not a.verbose:
            res["warnings"] = res["warnings"][:20]
        _print(res)
        return 0
    if c == "reindex":
        _print({"indexed": rs.reindex(st)})
        return 0
    if c == "dedupe":
        res = rs.dedupe(st, apply=a.apply, actor="cli")
        if not a.verbose:
            res["details"] = [f"{g['code']}: keeper result:{g['keeper']} ({g['rule']}), duplicates "
                              f"{', '.join(str(d['id']) for d in g['duplicates'])}" for g in res["details"]]
        _print(res)
        return 0
    if c == "worklist":
        w = rs.worklist(st, limit=a.limit)
        if a.json:
            _print(w)
            return 0
        print(json.dumps(w["counts"], ensure_ascii=False))
        for key in ("interpretation_missing", "data_newer_than_result", "idea_without_prereg", "prereg_after_result"):
            if w[key]:
                print(f"\n{key} ({w['counts'][key]})")
                for b in w[key][:a.limit]:
                    print(f"  {b['code']:<12} {b['series'] or '-':<14} {(b.get('title') or b['statement'])[:90]}")
        return 0
    if c == "series":
        _print(rs.series_get(st, args[0]) if args else rs.series_list(st))
        return 0
    if c == "show":
        if not args:
            raise ValueError("research show REF (hypothesis:N, finding:N or a code like H8.1 / F4.2)")
        kind, row = rs._node(st, args[0])
        out = rs.get_hypothesis(st, row["id"]) if kind == "hypothesis" else rs.get_finding(st, row["id"])
        if not a.verbose:
            out.pop("history", None)
            for r in out.get("results", []):
                r.pop("method_doc", None)
                r["evidence"] = len(r.get("evidence") or [])
        _print(out)
        return 0
    res = rs.search(st, " ".join(args) if c == "search" else "", series=a.series, status=a.status, verdict=a.verdict,
                    kind=a.kind, flag=a.flag, include_internal=True, type=a.type, limit=a.limit)
    if a.json:
        _print(res)
        return 0
    print(f"{res['total']} ({res['hypotheses']} hypotheses, {res['findings']} findings)")
    for b in res["items"]:
        state = b.get("verdict") or b.get("status")
        print(f"  {b['code']:<12} {b['series'] or '-':<14} {state:<17} {(b.get('title') or b['statement'])[:90]}")
    return 0


def cmd_cloud(st: Store, a) -> int:
    """fundus cloud status|connect|share|poll|contacts|inbox (fundus/cloud.py)."""
    import getpass
    from fundus import cloud
    cdir = cloud.cloud_dir(st)
    try:
        if a.cloud_cmd == "status":
            _print(cloud.status(cdir))
        elif a.cloud_cmd == "connect":
            if not a.args:
                raise ValueError("fundus cloud connect <email>")
            if not sys.stdin.isatty():
                raise ValueError("connect braucht ein Terminal (das Passwort wird interaktiv abgefragt)")
            pw = getpass.getpass("Passwort für Fundus Cloud: ")
            r = cloud.connect(cdir, a.args[0], pw)
            if r.get("recovery_code"):
                print("Neues Konto angelegt. Wiederherstellungscode – EINMALIG, bitte sicher aufbewahren:\n\n    "
                      f"{r['recovery_code']}\n", file=sys.stderr)
            print(f"Fingerabdruck: {r['fingerprint']}", file=sys.stderr)
            _print({k: v for k, v in r.items() if k != "recovery_code"})
        elif a.cloud_cmd == "disconnect":
            _print(cloud.disconnect(cdir))
        elif a.cloud_cmd == "share":
            if not a.args or not a.to:
                raise ValueError("fundus cloud share <ref> … --to <email> [--to …] [--confirm-internal]")
            r = cloud.share(st, a.args, a.to, confirm_internal=a.confirm_internal, actor="cli",
                            progress=lambda ph, d, t: print(f"\r{ph} {d}/{t}" if t else f"\r{ph}", end="", file=sys.stderr))
            print(file=sys.stderr)
            if r.get("needs_confirmation"):
                print("Interne Folien – erneut mit --confirm-internal senden, wenn das gewollt ist:", file=sys.stderr)
            _print(r)
        elif a.cloud_cmd == "poll":
            _print(cloud.poll(st))
        elif a.cloud_cmd == "inbox":
            _print(cloud.inbox(cdir))
        elif a.cloud_cmd == "lookup":
            _print(cloud.lookup(cdir, a.args))
        elif a.cloud_cmd == "contacts":
            if a.args:      # contacts <email> confirm|unconfirm|accept_key|remove
                if len(a.args) != 2:
                    raise ValueError("fundus cloud contacts [<email> confirm|unconfirm|accept_key|remove]")
                _print(cloud.set_contact(cdir, a.args[0], a.args[1]))
            else:
                _print(cloud.contacts(cdir))
    except cloud.CloudError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0



def _set_changes(pairs) -> dict:
    """--set FIELD=VALUE pairs as dataset changes (JSON for code_systems, crosswalk, inputs, tags; keywords comma list)."""
    ch = {}
    for kv in pairs or []:
        k, _, v = kv.partition("=")
        k, v = k.strip(), v.strip()
        if v[:1] in "{[" or v in ("true", "false"):
            ch[k] = json.loads(v)
        else:
            ch[k] = [x.strip() for x in v.split(",")] if k == "keywords" else (v or None)
    return ch


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fundus", description="Fundus evidence archive")
    ap.add_argument("--db", help=f"database path (default $FUNDUS_DB or {DEFAULT_DB})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats", help="counts per table, project and industry")

    def filters(p, internal=True):
        p.add_argument("--industry")
        p.add_argument("--topic")
        if internal:
            p.add_argument("--include-internal", action="store_true", help="show internal (own/client) items")
            p.add_argument("--project")

    p = sub.add_parser("search", help="full-text search")
    p.add_argument("text", nargs="*")
    p.add_argument("--kind")
    filters(p)
    p.add_argument("--region")
    p.add_argument("--since")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--mode", choices=q.SEARCH_MODES, default="hybrid",
                   help="hybrid (default: full-text + semantic, fused) | lexical | semantic")
    p.add_argument("--entity", help="only items linked to this organisation/person/product (name or alias)")

    p = sub.add_parser("get", help="one item with source, tags and origin")
    p.add_argument("kind")
    p.add_argument("id")
    p.add_argument("--include-internal", action="store_true")

    p = sub.add_parser("series", help="a time series with all observations and forecasts")
    p.add_argument("id")
    p.add_argument("--include-internal", action="store_true")

    p = sub.add_parser("overview", help="what do we have on an industry/topic")
    filters(p)
    p.add_argument("--region")

    p = sub.add_parser("due", help="series due for an update check")
    filters(p, internal=False)
    p.add_argument("--older-than", type=int, default=180, help="days since last check (default 180)")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("forecasts", help="open forecasts; --due marks reached ones and proposes outcomes")
    p.add_argument("--due", action="store_true", help="only forecasts whose target period has been reached, with proposals")
    p.add_argument("--apply", action="store_true", help="write the proposed outcomes (implies --due)")
    p.add_argument("--today", help="evaluate as of this ISO date")

    p = sub.add_parser("resolve", help="set a forecast outcome")
    p.add_argument("id", type=int)
    p.add_argument("outcome", choices=fc.OUTCOMES)
    p.add_argument("--actual", type=float)
    p.add_argument("--observation", type=int)
    p.add_argument("--note")
    p.add_argument("--include-internal", action="store_true", help="allow resolving internal forecasts")
    p.add_argument("--project", help="project of an internal forecast")

    p = sub.add_parser("import", help="run importers.<name>.run (or 'all')")
    p.add_argument("name")
    p.add_argument("--root", help="source root for a single importer")

    sub.add_parser("reindex", help="rebuild the full-text index")

    p = sub.add_parser("embed", help="embed new/changed items for semantic search (Ollama); manage embedding spaces")
    p.add_argument("--all", action="store_true", help="re-embed every item (resumable: continues an interrupted run)")
    p.add_argument("--limit", type=int, help="embed at most N items in this run")
    p.add_argument("--model", help=f"embedding model (default: active space, else {embed_defaults()})")
    p.add_argument("--dim", type=int, help="dimensions to keep (Matryoshka truncation)")
    p.add_argument("--quant", choices=("float32", "float16", "int8"), help="storage of the rescoring vectors")
    p.add_argument("--status", action="store_true", help="show embedding spaces and counts, embed nothing")
    p.add_argument("--activate", action="store_true", help="make the given space the one search uses")
    p.add_argument("--rebuild-index", action="store_true", help="recreate the coarse vec0 index from stored vectors")
    p.add_argument("--drop", action="store_true", help="delete all vectors of the given (inactive) space")
    p.add_argument("--derive-from", metavar="SPACE", help="fill the given space from stored vectors of SPACE "
                   "('model|dim|quant'; fewer dims / other quant, no Ollama)")
    p.add_argument("--keep-log-days", type=int, default=180, help="delete search_log rows older than this (default 180)")

    p = sub.add_parser("eval-search", help="measure search quality on a judged query set")
    p.add_argument("--file", default=str(Path(__file__).resolve().parent.parent / "eval" / "search_eval.json"))
    p.add_argument("--modes", default="lexical,semantic,hybrid")
    p.add_argument("--k", type=int, default=10, help="cut-off for nDCG/MRR and the unjudged listing (default 10)")
    p.add_argument("--unjudged", action="store_true", help="list top results per mode that have no judgment (pooling)")
    p.add_argument("--space", help="evaluate against this embedding space ('model|dim|quant') instead of the active one")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("entities", help="entity layer: organisations, people, products and their links to items")
    p.add_argument("action", choices=("build", "link", "update", "merge", "unmerge", "status", "show"))
    p.add_argument("args", nargs="*", help="show NAME | merge SRC_ID DST_ID | unmerge CHANGELOG_ID")
    p.add_argument("--no-llm", action="store_true", help="build: rule seeds and linking only")
    p.add_argument("--limit", type=int, help="build: at most N LLM batches in this run (resumable)")
    p.add_argument("--full", action="store_true", help="link: rebuild all links (default: incremental)")
    p.add_argument("--include-internal", action="store_true")

    p = sub.add_parser("exhibits", help="slides/charts: import, sync, add, list, show, versions, share, policy, relink, set")
    p.add_argument("action", choices=("import", "sync", "add", "list", "show", "versions", "share", "unshare", "policy", "relink", "set",
                                         "fold-versions", "deck", "lock", "unlock", "locks", "copy", "files"))
    p.add_argument("args", nargs="*", help="import DIR | add HTML PNG | show/versions/share REF | policy set MODE|show | "
                                           "set create|list|show|folder|rename|sharing … (slide sets) | "
                                           "lock set|slide REF | unlock set|slide REF (interactive, Falk only) | locks [PROJECT] | "
                                           "copy slide REF [--set TARGET] [--slug S] [--folder REL] | copy set REF --title T --folder REL | "
                                           "files deck:P/D|set:P/S|search:N|REF|PNG… [--version auto|current|verified] (slide files in order)")
    p.add_argument("--set", help="add/import/list: slide set slug (default for add/import: the set whose folder contains the PNG)")
    p.add_argument("--folder", action="append", help="set create: project-relative folder of the new set (repeatable)")
    p.add_argument("--slug", help="set create/rename: slug of the set (default: from the title)")
    p.add_argument("--project", help="project slug (default: from the path)")
    p.add_argument("--deck", help="deck name for deck copies (default: deck folders are detected)")
    p.add_argument("--script", help="add: generating script (default: searched in the project's skripte/)")
    p.add_argument("--note")
    p.add_argument("--dom", help="add: file with the rendered DOM (fit/density, written by render.sh)")
    p.add_argument("--caller", help="add: the script that ran render.sh (taken as the generating script if it is this slide's)")
    p.add_argument("--hook", action="store_true", help="add: called from render.sh (one log line, skips files outside ~/Projects)")
    p.add_argument("--measure", action="store_true", help="import: measure fit/density of the HTML (headless, parallel)")
    p.add_argument("--kind", choices=("slide", "chart", "poster", "section"))
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--include-internal", action="store_true")
    p.add_argument("--no-script", action="store_true", help="show: without the script text")
    p.add_argument("--apply", action="store_true", help="fold-versions: change the archive (default: report only)")
    p.add_argument("--title", help="deck set: title shown in the portal")
    p.add_argument("--pdf", help="deck set: the PDF built from this order")
    p.add_argument("--name", help="deck pdf: deck name (default: file name)")
    p.add_argument("--force", action="store_true", help="deck pdf: read again even if unchanged or named by a manifest")
    p.add_argument("--verbose", action="store_true", help="import: list every skipped file")
    p.add_argument("--version", choices=("auto", "current", "verified"), default="auto",
                   help="files: which version of each slide (auto = a deck's own version, else the current one)")

    p = sub.add_parser("photos", help="photo archive: import files/folders/decks, annotate with the local vision model, search")
    ps = p.add_subparsers(dest="photos_cmd", required=True)
    pi = ps.add_parser("import", help="import image files, folders (recursive) and decks/PDFs (their embedded pictures)")
    pi.add_argument("paths", nargs="+")
    pi.add_argument("--survey", help="field study / source (created if new) – the main grouping")
    pi.add_argument("--project")
    pi.add_argument("--period", help="e.g. 2019 or 2019-05")
    pi.add_argument("--place")
    pi.add_argument("--album")
    pi.add_argument("--title")
    pi.add_argument("--note")
    pi.add_argument("--consent", choices=("yes", "no", "unknown"), default="yes")
    pi.add_argument("--usage", choices=("internal", "teaching", "publication"), default="publication")
    pi.add_argument("--flat", action="store_true", help="do not descend into sub-folders")
    pa = ps.add_parser("annotate", help="annotate new photos with gemma4 (about 20 s per photo; the server does this in the background too)")
    pa.add_argument("ids", nargs="*", type=int)
    pa.add_argument("--limit", type=int)
    pa.add_argument("--retry-failed", action="store_true")
    pq = ps.add_parser("search", help="search photos and videos")
    pq.add_argument("text", nargs="*")
    pq.add_argument("--purpose", default="all", choices=("external", "teaching", "internal", "all"))
    pq.add_argument("--limit", type=int, default=20)
    pq.add_argument("--media", choices=("photo", "video", "all"), default="all")
    pg = ps.add_parser("show", help="one photo")
    pg.add_argument("ref")
    ps.add_parser("stats", help="counts, rights, groups")
    ps.add_parser("embed", help="embed descriptions of annotated photos for semantic search")

    p = sub.add_parser("project", help="projects and their own display names (the slug stays the identity)")
    p.add_argument("action", choices=("list", "title"))
    p.add_argument("args", nargs="*", help='title: PROJECT "NAME" (empty NAME removes the name)')

    p = sub.add_parser("review", help="reviews and the feedback inbox: share, gemini, list, show, invite, revoke, close, people, "
                                      "feedback, resolve, reopen, link, evidence, check-links")
    p.add_argument("action", choices=("share", "gemini", "run-gemini", "models", "sync", "unpublish", "list", "show", "invite", "revoke", "close", "people", "feedback",
                                      "resolve", "reopen", "link", "unlink", "evidence", "check-links",
                                      "loop", "loop-status", "loop-stop"))
    p.add_argument("args", nargs="*", help="share/gemini: PROJECT; show/invite/revoke/close/check-links: REVIEW; "
                                           "resolve: F-IDS…; reopen: F-ID; link/unlink: EXHIBIT KIND:ID…; evidence: EXHIBIT")
    p.add_argument("--slides", nargs="+", help="share/gemini: exhibit refs (marked slides)")
    p.add_argument("--set", help="share/gemini: slide set (id or slug)")
    p.add_argument("--deck", help="share/gemini: ordered deck (name or id)")
    p.add_argument("--person", action="append", default=[], help="'Name, Vorname' (repeatable)")
    p.add_argument("--title")
    p.add_argument("--focus", nargs="+", choices=("sources", "numbers", "logic", "language"))
    p.add_argument("--project", help="feedback: filter")
    p.add_argument("--review", type=int, help="feedback: filter")
    p.add_argument("--status", default="open", choices=("open", "decision", "done", "all"))
    p.add_argument("--outcome", help="resolve: incorporated | answered | rejected | decision")
    p.add_argument("--note", help="resolve: the comment (required); reopen: instruction")
    p.add_argument("--version", type=int, help="resolve incorporated: the new version (default: current)")
    p.add_argument("--loop", action="store_true", help="sync: keep running (fundus-review-sync.service)")
    p.add_argument("--interval", type=int, default=30, help="sync --loop: seconds between cycles")
    p.add_argument("--no-run", action="store_true", help="gemini: only queue the check (the server runs it)")
    p.add_argument("--model", help="gemini: Vertex model id (default: the strongest; list with 'review models')")
    p.add_argument("--iterations", type=int, help="loop: Gemini rounds at most (default 5)")
    p.add_argument("--wait", action="store_true", help="loop-status: block until it is the session's turn or the loop ended")

    sub.add_parser("merge-publishers", help="fold publisher variants listed in fundus/publisher_aliases.py")

    sub.add_parser("normalise", help="one unit notation, values in series units, curated publisher types")

    p = sub.add_parser("metrics", help="statistics about the archive (+ operation on the Spark) as JSON for the dashboard")
    p.add_argument("--no-health", action="store_true", help="skip systemd/MCP checks")

    p = sub.add_parser("dedupe", help="fold duplicate observations (same source, period, value; alike wording)")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("datasets", help="datasets (fundus.datasets): add files/folders, describe, search, show, query, purge")
    p.add_argument("datasets_cmd", choices=("add", "describe", "search", "show", "preview", "query", "update", "checked", "due", "crosswalk",
                                            "remove", "restore", "purge", "ask", "catalog", "locations", "fixity", "orphans",
                                            "at", "version", "convert-text"))
    p.add_argument("args", nargs="*", help="add: files/folders · search: words · ask: a free-text question (local model) · "
                                           "show/preview/query/remove/restore: ref (query: ref SQL) · at: REF YYYY-MM-DD · "
                                           "version: REF N (with --valid-from/--valid-to/--version-label …) · convert-text: REF --spec FILE")
    for o in ("--title", "--description", "--publisher", "--source-title", "--url", "--license", "--published", "--project", "--note",
              "--dataset", "--table"):
        p.add_argument(o)
    p.add_argument("--visibility", default="public", choices=("public", "internal"))
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument("--internal", action="store_true", help="add: same as --visibility internal · ask: include internal datasets")
    p.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE", help="update: e.g. --set period='2008-Q1 – 2026-Q3'")
    p.add_argument("--next-expected", help="checked: ISO month of the next edition")
    p.add_argument("--reference", action="store_true", help="add: register ONE folder/file as a reference (nothing copied)")
    p.add_argument("--retrieved-at", help="add: ISO date the data was obtained (citation)")
    p.add_argument("--script", help="add: script that fetched or built the data (provenance)")
    p.add_argument("--all", action="store_true", help="fixity: check every file (default: within --budget-gb)")
    p.add_argument("--budget-gb", type=float, default=20, help="fixity: GB of originals to re-hash per run (default 20)")
    p.add_argument("--purge", action="store_true", help="orphans: DELETE the reported orphan files (only on Falk's instruction)")
    # texts with versions (fundus.dataset_texts): laws, regulations, guidelines, standards, terms, policies
    p.add_argument("--text", action="store_true", help="add: a text with versions – files kept as delivered (documents), "
                                                       "never parsed; needs --act-type (new text) and --valid-from")
    p.add_argument("--identifier", help="add --text: official identifier (ELI, CELEX, SR number, URL)")
    p.add_argument("--jurisdiction", help="add --text: EU, CH, DE, US, …")
    p.add_argument("--act-type", choices=("law", "regulation", "directive", "ordinance", "guideline", "standard", "terms", "policy", "other"))
    p.add_argument("--issuer", help="add --text: issuing body")
    p.add_argument("--valid-from", help="add --text / version: ISO date from which this version applies")
    p.add_argument("--valid-to", help="add --text / version: ISO date of its last day (empty: still valid)")
    p.add_argument("--version-label", help="add --text / version: e.g. 'konsolidierte Fassung vom 27.07.2026'")
    p.add_argument("--published-at", help="add --text / version: ISO date of publication")
    p.add_argument("--source-url", help="add --text / version: where this version was obtained")
    p.add_argument("--spec", help="convert-text: JSON file {identifier, jurisdiction, act_type, issuer, base, versions: […]}")
    p.add_argument("--dry-run", action="store_true", help="convert-text: show the plan, change nothing")

    p = sub.add_parser("research", help="research registry: hypotheses (pre-registered) and findings – list, show, search, "
                                        "worklist, series, import, reindex (fundus.research)")
    p.add_argument("research_cmd", choices=("list", "show", "search", "worklist", "series", "import", "reindex", "dedupe"))
    p.add_argument("--apply", action="store_true", help="dedupe: mark the duplicates (default: dry run, report only)")
    p.add_argument("args", nargs="*", help="show: ref · search: words · series: slug · import: folder (_analysen)")
    p.add_argument("--dry-run", action="store_true", help="import: count only, write nothing")
    p.add_argument("--series")
    p.add_argument("--status")
    p.add_argument("--verdict")
    p.add_argument("--kind")
    p.add_argument("--flag")
    p.add_argument("--type", choices=("hypothesis", "finding"))
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--json", action="store_true")
    p.add_argument("--verbose", action="store_true")

    p = sub.add_parser("cloud", help="Fundus Cloud: send slide packages end-to-end encrypted to other Fundus installations")
    p.add_argument("cloud_cmd", choices=("status", "connect", "disconnect", "share", "poll", "inbox", "lookup", "contacts"))
    p.add_argument("args", nargs="*", help="connect: email · share: refs (project:<p>, set:<p>/<set>, exhibit:<id>) · "
                                           "lookup: emails · contacts: [<email> confirm|unconfirm|accept_key|remove]")
    p.add_argument("--to", action="append", help="share: recipient email (repeat for several)")
    p.add_argument("--confirm-internal", action="store_true", help="share: send internal slides too (after checking the list)")

    p = sub.add_parser("favorites", help="favourite slides (fundus.favorites): list, add, remove")
    p.add_argument("fav_cmd", choices=("list", "add", "remove"))
    p.add_argument("refs", nargs="*", help="add/remove: slide ids or project/set/slug")
    p.add_argument("--project")
    p.add_argument("--set", dest="set_ref")

    p = sub.add_parser("package", help="slide packages between Fundus installations (fundus.package)")
    p.add_argument("pkg_cmd", choices=("export", "inspect", "receive", "plan", "accept", "reject", "list", "sent", "install-skill",
                                       "internal"))
    p.add_argument("args", nargs="*", help="export/internal: refs (ids, project/slug, set:p/s, deck:p/d, project:p) · "
                                           "inspect/receive: package file · plan/accept/reject/install-skill: received id")
    p.add_argument("--out", help="export: target file or folder (default: current folder)")
    p.add_argument("--no-skill", action="store_true", help="export: without the mck-exhibit snapshot")
    p.add_argument("--to", action="append", default=[], help="export: recipient (records the package as sent)")
    p.add_argument("--sender", help="receive: sender name (default: from the manifest)")
    p.add_argument("--project", help="accept: target project (existing slug or a new name)")
    p.add_argument("--slides", nargs="*", help="accept: only these global ids or slugs")
    p.add_argument("--decide", action="append", default=[], metavar="GID=ACTION",
                   help="accept: accept | new_version | variant | skip for one slide")
    p.add_argument("--force", action="store_true", help="install-skill: replace local changes or a newer version")

    p = sub.add_parser("archive", help="archive of slides (fundus.archive): findable, not listed in their project")
    p.add_argument("archive_cmd", choices=("list", "archive", "unarchive"))
    p.add_argument("refs", nargs="*", help="archive/unarchive: refs · list: search words")
    p.add_argument("--target", default="slide", choices=("slide", "set", "project"))
    p.add_argument("--project")

    p = sub.add_parser("trash", help="trash of slides, slide sets and projects (fundus.trash): list, trash, restore, purge")
    p.add_argument("trash_cmd", choices=("list", "trash", "restore", "purge", "move"))
    p.add_argument("refs", nargs="*", help="trash/move: refs · restore/purge: trash ids")
    p.add_argument("--target", choices=("slide", "set", "project"), help="trash/move: what the refs are")
    p.add_argument("--to", help="move: target set ('project/set' or id) for slides, target project for sets and projects")
    p.add_argument("--expired", action="store_true", help="purge: everything older than 30 days (nightly)")
    p.add_argument("--all", dest="purge_all", action="store_true", help="purge: empty the whole trash")
    p.add_argument("--state", default="trashed", choices=("trashed", "restored", "purged"))

    p = sub.add_parser("tidy", help="standard data hygiene: automatic fixes, unit standard, review with Claude (fundus.tidy)")
    p.add_argument("tidy_cmd", choices=("run", "units", "review", "decide", "report"))
    p.add_argument("file", nargs="?", help="decide: JSON file with decisions")
    p.add_argument("--apply", action="store_true", help="units: apply the plan (default: show it)")
    p.add_argument("--no-units", action="store_true", help="run: skip the unit standard")
    p.add_argument("--no-backup", action="store_true", help="run: no database backup before applying")
    p.add_argument("--out", help="review: write the findings to this file")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--actor", default="cli:tidy")

    p = sub.add_parser("journals", help="fill document journals from Crossref by DOI")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("cleanup", help="data hygiene: duplicates, spelling variants, contradictions, orphans (fundus.cleanup)")
    p.add_argument("cleanup_cmd", choices=("analyze", "status", "candidates", "apply", "decide", "undo", "runs"))
    p.add_argument("ids", nargs="*", help="decide: finding ids · undo: run id")
    p.add_argument("--class", dest="cls", choices=("variant", "safe", "orphan", "probable", "contradiction"))
    p.add_argument("--state", default="open", choices=("open", "applied", "rejected", "later", "gone"))
    p.add_argument("--action", choices=("merge", "delete", "dispute", "reject", "later", "reopen"), help="decide")
    p.add_argument("--keep", help="decide merge: the record to keep")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--yes", action="store_true", help="apply: carry out without asking")
    p.add_argument("--no-backup", action="store_true", help="apply: skip the database backup before the run")

    p = sub.add_parser("paths", help="rewrite stored absolute paths after a folder moved (fundus.relocate)")
    p.add_argument("paths_cmd", choices=("rewrite",))
    p.add_argument("--from", dest="src", required=True, help="old folder (absolute, or relative to ~/Projects)")
    p.add_argument("--to", dest="dst", required=True, help="new folder (absolute, or relative to ~/Projects)")
    p.add_argument("--dry-run", action="store_true", help="only count (opens the database read-only)")

    p = sub.add_parser("snapshot", help="consistent copy of the database for backups")
    p.add_argument("--out", help="target file (default data/backup/fundus.sqlite)")

    p = sub.add_parser("serve", help="start the server (MCP, portal, API) with the job scheduler – same as python -m fundus.mcp_server")
    p.add_argument("--host", help="bind address (default $FUNDUS_MCP_HOST, config file or 127.0.0.1)")
    p.add_argument("--port", type=int, help="port (default $FUNDUS_MCP_PORT, config file or 8010)")
    p.add_argument("--scheduler", choices=("auto", "on", "off"), default="auto",
                   help="jobs inside the server: auto = on unless the systemd timers are installed ($FUNDUS_SCHEDULER=1/0)")
    p.add_argument("--log-file", help="append the server's output to this file (Windows task, no console)")

    p = sub.add_parser("render", help="render slides HTML -> PNG with Playwright and register them (render.sh in Python)")
    from fundus import render as _render
    _render.add_arguments(p)

    p = sub.add_parser("service", help="start Fundus with the machine: systemd (Linux), launchd (macOS), Task Scheduler (Windows)")
    p.add_argument("service_cmd", choices=("install", "uninstall", "status"))
    p.add_argument("--dry-run", action="store_true", help="show the files and commands, change nothing")
    p.add_argument("--force", action="store_true", help="install: replace an existing installation with other content")
    p.add_argument("--host", help="install: bind address of the server (default as fundus serve)")
    p.add_argument("--port", type=int, help="install: port of the server")
    p.add_argument("--with-review-sync", action="store_true", help="Linux: also the Fundus Review sync service (E4)")

    p = sub.add_parser("scheduler", help="the in-process job scheduler: status (jobs, next and last runs) | run JOB now")
    p.add_argument("sched_cmd", choices=("status", "run"))
    p.add_argument("job", nargs="?", help="run: import | cleanup | embed | snapshot")

    p = sub.add_parser("llm", help="optional language model: check endpoint, models, image input and embedding dimension")
    p.add_argument("llm_cmd", choices=("check",))
    p.add_argument("--quick", action="store_true", help="only the endpoint and its model list, no test requests")
    p.add_argument("--timeout", type=float, default=600, help="seconds per test request (loading a big model takes a while)")
    p.add_argument("--json", action="store_true", help="the report as JSON")

    p = sub.add_parser("vertex", help="optional Gemini on Vertex AI (slide reviews): check project, region, credentials, model")
    p.add_argument("vertex_cmd", choices=("check",))
    p.add_argument("--model", help="model to test (default: the first of vertex_models)")
    p.add_argument("--no-call", action="store_true", help="do not send the test request (no cost), only credentials")
    p.add_argument("--json", action="store_true", help="the report as JSON")

    p = sub.add_parser("export", help="export items (public only by default)")
    p.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    p.add_argument("--out", help="file (default stdout)")
    p.add_argument("--kind", choices=ITEM_KINDS)
    p.add_argument("--include-internal", action="store_true")

    a = ap.parse_args(argv)
    if a.cmd in ("serve", "render", "service", "scheduler"):   # no Store here: they open their own (or none)
        return cmd_platform(a)
    if a.cmd in ("llm", "vertex"):   # checks of the optional services: no Store (they never write to the archive)
        if a.cmd == "llm":
            from fundus import llm
            rep = llm.check(a.db or None, quick=a.quick, timeout=a.timeout)
            print(json.dumps(rep, ensure_ascii=False, indent=2) if a.json else llm.format_check(rep))
        else:
            from fundus import gemini
            rep = gemini.check(a.model, call=not a.no_call)
            print(json.dumps(rep, ensure_ascii=False, indent=2) if a.json else gemini.format_check(rep))
        return 0 if rep["ok"] else 1
    if a.cmd == "paths":   # before opening the Store: the dry run must not write (Store() runs the schema migrations)
        from fundus.relocate import rewrite
        try:
            _print(rewrite(a.db or DEFAULT_DB, a.src, a.dst, dry_run=a.dry_run))
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0
    st = Store(a.db) if a.db else Store()
    st.actor = "cli"
    try:
        if a.cmd == "stats":
            _print(st.stats())
        elif a.cmd == "search":
            _print(q.search_logged(st, "cli", " ".join(a.text), mode=a.mode, kind=a.kind, industry=a.industry,
                                   topic=a.topic, region=a.region, since=a.since, status=a.status, limit=a.limit,
                                   include_internal=a.include_internal, project=a.project, entity=a.entity))
        elif a.cmd == "get":
            _print(q.get(st, a.kind, a.id, include_internal=a.include_internal))
        elif a.cmd == "series":
            _print(q.series(st, a.id, include_internal=a.include_internal))
        elif a.cmd == "overview":
            _print(q.overview(st, industry=a.industry, topic=a.topic, region=a.region,
                              include_internal=a.include_internal, project=a.project))
        elif a.cmd == "due":
            _print(q.update_targets(st, industry=a.industry, topic=a.topic, older_than_days=a.older_than, limit=a.limit))
        elif a.cmd == "forecasts":
            today = date.fromisoformat(a.today) if a.today else None
            if a.due or a.apply:
                _print(fc.review(st, today=today, apply=a.apply, actor="cli"))
            else:
                rows = st.db.execute("SELECT * FROM forecast WHERE outcome IN ('open','due')").fetchall()
                rows = sorted(rows, key=lambda r: q._period_key(r["target_period"]))
                _print([q.summary(st, "forecast", r, include_internal=True, with_tags=False) for r in rows])
        elif a.cmd == "resolve":
            _print(fc.resolve(st, a.id, a.outcome, actual_value=a.actual, observation_id=a.observation, note=a.note,
                              actor="cli", include_internal=a.include_internal, project=a.project))
        elif a.cmd == "import":
            counts = run_import(st, a.name, a.root)
            # keep the archive clean after every import (counts as import, not curation); incremental: only what
            # changed since the last maintenance run (imports and session writes), full on the first run or when forced
            from fundus.fingerprint import forced
            since = None if forced() else st.maintenance_marker()
            with st.batch("importer:maintenance"):
                counts["_maintenance"] = {"scope": "full" if since is None else f"changes after changelog #{since}",
                                          "normalise": st.normalise(since=since),
                                          "deduplicated": len(st.dedupe(since=since))}
                st.set_maintenance_marker()
            try:   # entity links for new/changed items (rule seeds + gazetteer; no LLM); must not fail the import
                from fundus import entities
                counts["_entities"] = entities.update(st)
            except Exception as e:
                st.db.rollback()
                counts["_entities"] = {"error": f"{type(e).__name__}: {e}"}
            try:   # keep semantic search fresh; Ollama being down or busy must not fail the import
                from fundus import embed
                counts["_embed"] = embed.embed_pending(st)
            except Exception as e:
                counts["_embed"] = {"error": f"{type(e).__name__}: {e}"}
            _print({"imported": counts, "stats": st.stats()})
        elif a.cmd == "photos":
            return cmd_photos(st, a)
        elif a.cmd == "cleanup":
            return cmd_cleanup(st, a)
        elif a.cmd == "journals":
            return cmd_journals(st, a)
        elif a.cmd == "tidy":
            return cmd_tidy(st, a)
        elif a.cmd == "research":
            return cmd_research(st, a)
        elif a.cmd == "datasets":
            from fundus import datasets as dsm
            c, args = a.datasets_cmd, a.args
            from fundus import dataset_catalog as cat

            def _after_add(res):
                """add/--reference: apply --set FIELD=VALUE and --next-expected right away (no separate update needed)."""
                ch = _set_changes(a.set)
                if a.next_expected:
                    ch["next_expected"] = a.next_expected
                if ch and isinstance(res, dict) and res.get("ref"):
                    res["updated"] = dsm.update(st, res["ref"], ch, actor="cli").get("changed", sorted(ch))
                return res
            if c == "add" and a.internal:      # `add --internal` means visibility internal (it was silently ignored before)
                a.visibility = "internal"
            if c == "add" and a.reference:
                if len(args) != 1:
                    raise ValueError("datasets add --reference PATH (one folder or file)")
                _print(_after_add(cat.add_reference(st, args[0], title=a.title, description=a.description, source_title=a.source_title,
                                         publisher=a.publisher, url_=a.url, license=a.license, published=a.published,
                                         project=a.project, visibility=a.visibility, note=a.note, retrieved_at=a.retrieved_at,
                                         script=a.script, actor="cli")))
            elif c == "add":
                prog = lambda n, t, f: print(f"\r{n}/{t} {Path(f).name[:60]:60}", end="", file=sys.stderr, flush=True)
                _print(_after_add(dsm.add(st, args, title=a.title, description=a.description, source_title=a.source_title, publisher=a.publisher,
                               url_=a.url, license=a.license, published=a.published, project=a.project, visibility=a.visibility,
                               dataset=a.dataset, note=a.note, actor="cli", progress=prog, retrieved_at=a.retrieved_at,
                               script=a.script, text=a.text, identifier=a.identifier, jurisdiction=a.jurisdiction,
                               act_type=a.act_type, issuer=a.issuer, valid_from=a.valid_from, valid_to=a.valid_to,
                               version_label=a.version_label, published_at=a.published_at, source_url=a.source_url)))
            elif c == "describe":
                _print(dsm.describe_pending(st, retry_failed=a.retry_failed))
            elif c in ("at", "version", "convert-text"):      # texts with versions
                from fundus import dataset_texts as tx
                if c == "at":
                    if len(args) != 2:
                        raise ValueError("datasets at REF YYYY-MM-DD")
                    _print(tx.version_at(st, args[0], args[1]))
                elif c == "version":
                    if len(args) != 2:
                        raise ValueError("datasets version REF N --valid-from … [--valid-to …] [--version-label …]")
                    ch = {k: getattr(a, k) for k in tx.VERSION_FIELDS if getattr(a, k) is not None}
                    _print(tx.set_version(st, args[0], int(args[1]), ch, actor="cli"))
                else:
                    if len(args) != 1 or not a.spec:
                        raise ValueError("datasets convert-text REF --spec FILE [--dry-run]")
                    _print(tx.convert(st, args[0], json.loads(Path(a.spec).read_text()), actor="cli", dry_run=a.dry_run))
            elif c == "search":
                _print(dsm.search(st, " ".join(args), project=a.project, include_internal=True))
            elif c == "ask":                  # free-text question to the local model, from the descriptions only
                from fundus import dataset_ask
                _print(dataset_ask.ask(st, " ".join(args), include_internal=a.internal, project=a.project))
            elif c == "show":
                out = dsm.get(st, args[0])
                cat.log_use(st, out["id"], "show", actor="cli", project=a.project)
                _print(out)
            elif c == "preview":
                out = dsm.preview(st, args[0], table=a.table)
                cat.log_use(st, out["dataset"], "preview", actor="cli", project=a.project, detail={"table": out.get("table")})
                _print(out)
            elif c == "query":
                out = dsm.query(st, args[0], " ".join(args[1:]))
                cat.log_use(st, out["dataset"], "query", actor="cli", project=a.project, detail=" ".join(args[1:])[:300])
                _print(out)
            elif c == "catalog":
                _print(cat.catalog(st, removed=bool(args and args[0] == "removed"))["stats"])
            elif c == "locations":          # nightly: re-check the folders of reference datasets
                _print(cat.refresh_locations(st, actor="cli"))
            elif c == "fixity":             # nightly: re-hash originals within a budget, report orphans
                res = cat.fixity(st, budget_gb=a.budget_gb, all=a.all, actor="cli")
                o = res["orphans"]
                print(f"fixity: {res['checked']} files checked, {res['ok']} ok, {len(res['failed'])} failed, "
                      f"{res['bytes_hashed'] / 1024 ** 3:.1f} GB hashed, {res['left']} left for later runs", file=sys.stderr)
                print(f"orphans: {o['files']} files/folders, {o['bytes'] / 1024 ** 2:.1f} MB (report only; delete with "
                      f"`datasets orphans --purge` on Falk's instruction)", file=sys.stderr)
                _print(res)
            elif c == "orphans":            # report; --purge deletes – only when Falk asks, never nightly
                _print(cat.purge_orphans(st, actor="cli") if a.purge else cat.orphans(st))
            elif c == "update":
                ch = _set_changes(a.set)
                if not args or not ch:
                    raise ValueError("datasets update REF --set field=value [--set …]")
                _print(dsm.update(st, args[0], ch, actor="cli"))
            elif c == "crosswalk":            # datasets crosswalk [FROM TO [CODE…]]
                if len(args) >= 2:
                    _print(dsm.crosswalk_lookup(st, args[0], args[1], codes=args[2:] or None, include_internal=True))
                else:
                    _print({"code_systems": dsm.code_system_registry(st, include_internal=True),
                            "crosswalks": dsm.crosswalks(st, include_internal=True)})
            elif c == "checked":
                _print(dsm.mark_checked(st, args[0], next_expected=a.next_expected, actor="cli"))
            elif c == "due":
                _print(dsm.due(st, include_internal=True))
            elif c in ("remove", "restore"):
                _print(dsm.remove(st, args[0], restore=c == "restore", actor="cli"))
            elif c == "purge":
                _print(dsm.purge_removed(st))
        elif a.cmd == "favorites":
            from fundus import favorites
            if a.fav_cmd == "list":
                _print(favorites.listing(st, a.project, a.set_ref))
            else:
                _print(favorites.mark(st, a.refs, a.fav_cmd == "add", actor="cli"))
        elif a.cmd == "package":
            from fundus import package as pk
            if a.pkg_cmd not in ("list", "sent") and not a.args:
                raise ValueError(f"package {a.pkg_cmd}: give " + ("refs" if a.pkg_cmd in ("export", "internal") else
                                                                  "the package file" if a.pkg_cmd in ("inspect", "receive") else "the received id"))
            if a.pkg_cmd == "export":
                out = pk.export(st, a.args, a.out or ".", actor="cli", include_skill=not a.no_skill)
                if a.to:
                    pk.record_sent(st, out["manifest"], a.to, transport="file", path=out["path"])
                man = out["manifest"]
                _print({"path": out["path"], "title": man["title"], "slides": len(man["slides"]), "files": len(man["files"]),
                        "bytes": sum(man["files"].values()), "references": man["references"], "internal": man["internal"],
                        "skill": man["skill"] and man["skill"]["version"]})
            elif a.pkg_cmd == "internal":
                _print(pk.internal_report(st, a.args))
            elif a.pkg_cmd == "inspect":
                _print(pk.inspect(a.args[0]))
            elif a.pkg_cmd == "receive":
                _print(pk.receive(st, a.args[0], sender=a.sender))
            elif a.pkg_cmd == "plan":
                _print(pk.plan(st, a.args[0]))
            elif a.pkg_cmd == "accept":
                if not a.project:
                    raise ValueError("give --project (existing slug or a new name)")
                dec = dict(x.split("=", 1) for x in a.decide)
                _print(pk.accept(st, a.args[0], a.project, slides=a.slides or None, decisions=dec, actor="cli"))
            elif a.pkg_cmd == "reject":
                _print(pk.reject(st, a.args[0], actor="cli"))
            elif a.pkg_cmd == "install-skill":
                _print(pk.install_skill(st, a.args[0], force=a.force, actor="cli"))
            elif a.pkg_cmd == "sent":
                _print(pk.sent_list(st))
            else:
                _print(pk.received_list(st))
        elif a.cmd == "archive":
            from fundus import archive
            if a.archive_cmd == "list":
                _print(archive.listing(st, " ".join(a.refs), a.project))
            else:
                fn = archive.archive if a.archive_cmd == "archive" else archive.unarchive
                _print(fn(st, a.target, a.refs, project=a.project, actor="cli"))
        elif a.cmd == "trash":
            from fundus import trash
            c = a.trash_cmd
            if c == "list":
                _print(trash.listing(st, a.state))
            elif c == "trash":
                _print(trash.trash(st, a.target or "slide", a.refs, actor="cli"))
            elif c == "restore":
                _print([trash.restore(st, i, actor="cli") for i in a.refs])
            elif c == "purge":
                if not (a.expired or a.purge_all or a.refs):
                    raise ValueError("purge ID… | --expired | --all")
                _print(trash.purge(st, ids=a.refs, expired=a.expired, everything=a.purge_all, actor="cli"))
            elif c == "move":
                if not a.to or not a.target:
                    raise ValueError("move REF… --target slide|set|project --to TARGET")
                _print(trash.move(st, a.target, a.refs, a.to, actor="cli"))
        elif a.cmd == "reindex":
            _print({"indexed": st.reindex()})
        elif a.cmd == "embed":
            return cmd_embed(st, a)
        elif a.cmd == "entities":
            return cmd_entities(st, a)
        elif a.cmd == "review":
            return cmd_review(st, a)
        elif a.cmd == "project":
            from fundus import projects
            if a.action == "list":
                _print(projects.list_projects(st))
            elif len(a.args) not in (1, 2):
                print('usage: fundus project title PROJECT "NAME"', file=sys.stderr)
                return 2
            else:
                _print(projects.set_title(st, a.args[0], a.args[1] if len(a.args) > 1 else None))
        elif a.cmd == "cloud":
            return cmd_cloud(st, a)
        elif a.cmd == "exhibits":
            return cmd_exhibits(st, a)
        elif a.cmd == "eval-search":
            from fundus import eval_search
            data = eval_search.load(a.file)
            modes = [m.strip() for m in a.modes.split(",") if m.strip()]
            if a.unjudged:
                res = eval_search.unjudged(st, data, modes, k=a.k)
                print(json.dumps(res, ensure_ascii=False, indent=2) if a.json else eval_search.format_unjudged(res))
            else:
                from fundus import embed
                res = eval_search.evaluate(st, data, modes, k=a.k, space=embed.Space.parse(a.space) if a.space else None)
                print(json.dumps(res, ensure_ascii=False, indent=2) if a.json else eval_search.format_report(res))
        elif a.cmd == "metrics":
            from fundus import metrics
            out = metrics.compute(st)
            if not a.no_health:
                out["health"] = metrics.health(st)
            print(json.dumps(out, ensure_ascii=False, default=str))
        elif a.cmd == "normalise":
            with st.batch("importer:maintenance"):
                _print(st.normalise())
        elif a.cmd == "dedupe":
            with st.batch("importer:maintenance"):
                merges = st.dedupe(dry_run=a.dry_run)
            _print({"merged" if not a.dry_run else "would_merge": len(merges), "pairs": merges})
        elif a.cmd == "merge-publishers":
            with st.batch("cli"):
                _print({"merged": st.merge_aliases()})
        elif a.cmd == "snapshot":
            _print(snapshot(st, a.out))
        elif a.cmd == "export":
            kinds = (a.kind,) if a.kind else ITEM_KINDS
            out = open(a.out, "w", newline="", encoding="utf-8") if a.out else sys.stdout
            try:
                n = 0
                if a.format == "jsonl":
                    for item in export_rows(st, a.include_internal, kinds):
                        out.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
                        n += 1
                else:
                    w = csv.DictWriter(out, fieldnames=EXPORT_COLS)
                    w.writeheader()
                    for item in export_rows(st, a.include_internal, kinds):
                        w.writerow(_csv_row(item))
                        n += 1
            finally:
                if a.out:
                    out.close()
            if a.out:
                print(f"exported {n} items to {a.out}", file=sys.stderr)
    except (LookupError, PermissionError, ValueError) as e:
        print(f"error: {e.args[0] if e.args else e}", file=sys.stderr)
        return 1
    finally:
        st.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
