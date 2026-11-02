"""Review sync Spark ⇄ Firebase with an in-memory Firestore/Storage: publish, links, pull of reviewers' contributions,
answers back, Gemini findings, re-invite, close and expiry; plus the Firestore value encoding."""
import hashlib
from datetime import datetime, timezone

from fundus import firebase as fb
from fundus import review_sync as rs
from fundus import reviews as rv
from test_exhibits import page, png_bytes, reg, write, projects as projects_root  # noqa: F401  (fixture)


class Fake:
    """Firestore/Storage stand-in with the Client's interface (paths relative to the database root)."""
    def __init__(self):
        self.docs, self.files = {}, {}

    def w_set(self, path, data): return ("set", path, dict(data))
    def w_merge(self, path, data): return ("merge", path, dict(data))
    def w_delete(self, path): return ("delete", path, None)

    def commit(self, writes):
        for op, path, data in writes:
            if op == "set":
                self.docs[path] = data
            elif op == "merge":
                self.docs.setdefault(path, {}).update(data)
            else:
                self.docs.pop(path, None)

    def get(self, path): return self.docs.get(path)

    def list(self, path):
        pre = path + "/"
        return [{"_id": p[len(pre):], **d} for p, d in self.docs.items() if p.startswith(pre) and "/" not in p[len(pre):]]

    def query(self, parent, collection, field, op, value):
        return [d for d in self.list(f"{parent}/{collection}") if d.get(field) == value]

    def upload(self, name, data, ctype):
        self.files[name] = data
        return f"https://files.test/{name}"

    def delete_prefix(self, prefix):
        ks = [k for k in self.files if k.startswith(prefix)]
        for k in ks:
            del self.files[k]
        return len(ks)

    def add_item(self, rid, docid, **d):
        self.docs[f"reviews/{rid}/items/{docid}"] = {"synced": False, **d}


def setup(store, root):
    doc = store.document("Cox AI tracker", url="https://www.coxautoinc.com/tracker", publisher="Cox Automotive")
    obs = store.add("observation", {"metric": "US dealers using AI", "value": 82, "unit": "%", "period": "2026-Q2", "document_id": doc})
    store.db.commit()
    html = page("Dealers").replace("<head>", f'<head><meta name="fundus-refs" content="document:{doc} observation:{obs}">', 1)
    s = reg(store, write(root, "Acme-Dealerships/exhibits/kpi-1", html, png=png_bytes(rgb=(9, 9, 90))))
    s2 = reg(store, write(root, "Acme-Dealerships/exhibits/kpi-2", page("Adoption"), png=png_bytes(rgb=(9, 90, 9))))
    return doc, obs, s["exhibit_id"], s2["exhibit_id"]


def test_encoding_roundtrip():
    t = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    v = {"a": 1, "b": 2.5, "c": "x", "d": True, "e": None, "f": [1, "y"], "g": {"h": t}}
    assert {k: fb.dec(fb.enc(x)) for k, x in v.items()} == v
    assert fb.enc(3) == {"integerValue": "3"} and fb.enc(False) == {"booleanValue": False}


def test_publish_pull_answer_revoke_close(store, projects_root):
    doc, obs, e1, e2 = setup(store, projects_root)
    r = rv.create(store, "acme-dealerships", slides=[e1, e2], people=["Keller, Anna", "Meier, Ben"])
    fc = Fake()
    out = rs.cycle(store, fc)
    assert out["published"] == [{"published": r["id"], "slides": 2, "links": 2}] and not out["errors"]
    rid = str(r["id"])
    assert fc.docs[f"reviews/{rid}"]["title"] == r["title"] and fc.docs[f"reviews/{rid}"]["slideCount"] == 2
    s1 = fc.docs[f"reviews/{rid}/slides/1"]
    assert s1["exhibit"] == e1 and s1["image"].endswith(".jpg") and s1["sources"][0]["id"] == doc and s1["data"][0]["id"] == str(obs)
    assert len(fc.files) == 4 and all(k.startswith("r/") for k in fc.files)
    anna = next(p for p in rv.get(store, r["id"])["reviewers"] if p["name"] == "Anna Keller")
    h = hashlib.sha256(anna["link"].rsplit("/", 1)[1].encode()).hexdigest()
    assert fc.docs[f"access/{h}"] == {"review": rid, "reviewer": str(anna["id"]), "name": "Anna Keller", "active": True,
                                       "expires": rs._ts(store.db.execute("SELECT expires_at FROM review").fetchone()[0]), "finished": False}
    assert rv.get(store, r["id"])["status"] == "open" and rv.get(store, r["id"])["published"]
    # reviewers write: a marked comment, a doubt about the source, an ok on the data point, garbage
    fc.add_item(rid, "a1", slide=1, exhibit=e1, version=1, kind="shape", text="Zeitraum fehlt", author=str(anna["id"]),
                authorName="Anna Keller", shape={"tool": "rect", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1})
    fc.add_item(rid, "a2", slide=1, exhibit=e1, version=1, kind="check", text="", verdict="doubt",
                target={"kind": "document", "id": str(doc)}, note="Ausgabe 2025 nehmen", author=str(anna["id"]), authorName="Anna Keller")
    fc.add_item(rid, "a3", slide=1, exhibit=e1, version=1, kind="check", text="", verdict="ok",
                target={"kind": "observation", "id": str(obs)}, author=str(anna["id"]), authorName="Anna Keller")
    fc.add_item(rid, "x1", slide=9, exhibit=e1, version=1, kind="comment", text="?", author=str(anna["id"]), authorName="Anna Keller")
    fc.add_item(rid, "x2", slide=2, exhibit=e2, version=1, kind="check", text="", verdict="ok",
                target={"kind": "document", "id": str(doc)}, author=str(anna["id"]), authorName="Anna Keller")   # not on slide 2
    out = rs.cycle(store, fc)
    assert out["pulled"] == 3 and not out["errors"]
    items = {k.rsplit("/", 1)[1]: v for k, v in fc.docs.items() if "/items/" in k}
    assert items["a1"]["synced"] and items["a1"]["fundusId"].startswith("F-") and items["a3"]["status"] == "recorded"
    assert items["x1"]["status"] == "rejected" and "Folie" in items["x1"]["reply"] and items["x2"]["status"] == "rejected"
    assert rv.verification(store, "observation", obs)["stage"] == "verified"
    box = rv.inbox(store, review=r["id"])
    assert sorted(i["kind"] for s in box["slides"] for i in s["items"]) == ["check", "shape"]
    # the new stage reaches the published slide on the next cycle
    rs.cycle(store, fc)
    assert fc.docs[f"reviews/{rid}/slides/1"]["data"][0]["stage"] == "verified"
    # a session answers → the reviewer sees it
    ref = items["a2"]["fundusId"]
    rv.resolve(store, [ref], "rejected", "Ausgabe 2026 ist die aktuelle")
    rs.cycle(store, fc)
    assert fc.docs[f"reviews/{rid}/items/a2"]["status"] == "rejected" and fc.docs[f"reviews/{rid}/items/a2"]["reply"] == "Ausgabe 2026 ist die aktuelle"
    # revoke Ben, re-invite: old link inactive → replaced by a new access document
    ben = next(p for p in rv.get(store, r["id"])["reviewers"] if p["name"] == "Ben Meier")
    hb = hashlib.sha256(ben["link"].rsplit("/", 1)[1].encode()).hexdigest()
    rv.revoke(store, r["id"], "Meier, Ben")
    rs.cycle(store, fc)
    assert fc.docs[f"access/{hb}"]["active"] is False
    new = next(p for p in rv.invite(store, r["id"], ["Meier, Ben"])["reviewers"] if p["name"] == "Ben Meier")
    rs.cycle(store, fc)
    hn = hashlib.sha256(new["link"].rsplit("/", 1)[1].encode()).hexdigest()
    assert f"access/{hb}" not in fc.docs and fc.docs[f"access/{hn}"]["active"] is True
    # close: last pull, then everything leaves Firebase; the feedback stays in Fundus
    fc.add_item(rid, "a4", slide=2, exhibit=e2, version=1, kind="comment", text="Letzter Punkt", author=str(anna["id"]), authorName="Anna Keller")
    rv.close(store, r["id"])                                                  # closing: read-only, still visible
    out = rs.cycle(store, fc)
    assert out["pulled"] == 1 and out["unpublished"] == []
    assert all(v["finished"] for k, v in fc.docs.items() if k.startswith("access/")) and f"reviews/{rid}" in fc.docs
    rv.purge(store, r["id"])                                                  # deleting: gone from Firebase
    out = rs.cycle(store, fc)
    assert out["unpublished"][0]["unpublished"] == r["id"]
    assert fc.docs == {} and fc.files == {}
    assert any(i["text"] == "Letzter Punkt" for s in rv.inbox(store, review=r["id"], status="all")["slides"] for i in s["items"])
    assert rs.cycle(store, fc)["unpublished"] == []


def test_gemini_first_then_publish_with_findings(store, projects_root):
    doc, obs, e1, e2 = setup(store, projects_root)
    r = rv.create(store, "acme-dealerships", slides=[e1], people=["Keller, Anna"], gemini=True)
    fc = Fake()
    assert rs.cycle(store, fc)["published"] == []                     # Gemini pre-check still queued
    rv.add(store, e1, "Basis der 41 % nennen", source="ai", review_id=r["id"], severity="medium",
           shape={"tool": "rect", "x": 0.5, "y": 0.2, "w": 0.3, "h": 0.2}, kind="shape")
    store.db.execute("UPDATE review SET gemini_state = 'done' WHERE id = ?", (r["id"],))
    store.db.commit()
    assert rs.cycle(store, fc)["published"]
    rs.cycle(store, fc)
    ai = [v for k, v in fc.docs.items() if "/items/f" in k]
    assert len(ai) == 1 and ai[0]["author"] == rs.AI_AUTHOR and ai[0]["synced"] and ai[0]["shape"]["x"] == 0.5
    assert fc.docs[f"reviews/{r['id']}/reviewers/{rs.AI_AUTHOR}"]["kind"] == "ai"
    fid = int(ai[0]["fundusId"][2:])
    from fundus import exhibits as ex  # noqa: F401
    fix = lambda: reg(store, write(projects_root, "Acme-Dealerships/exhibits/kpi-1", page("Dealers – Basis genannt"), png=png_bytes(rgb=(200, 9, 9))))
    assert fix()["status"] == "locked"                                 # Anna is still reviewing: no change under her
    store.db.execute("UPDATE review_access SET finished_at = '2026-10-27 08:00:00' WHERE review_id = ?", (r["id"],))
    store.db.commit()
    assert fix()["status"] == "new_version"
    rv.resolve(store, [fid], "incorporated", "Basis 'unter KI-Nutzern' ergänzt")
    rs.cycle(store, fc)
    d = fc.docs[f"reviews/{r['id']}/items/f{fid}"]
    assert d["status"] == "incorporated" and d["resolvedVersion"] == 2 and "Basis" in d["reply"]


def test_expired_review_is_removed(store, projects_root):
    doc, obs, e1, e2 = setup(store, projects_root)
    r = rv.create(store, "acme-dealerships", slides=[e1], people=["Keller, Anna"])
    fc = Fake()
    rs.cycle(store, fc)
    store.db.execute("UPDATE review SET expires_at = '2020-01-01 00:00:00' WHERE id = ?", (r["id"],))
    store.db.commit()
    out = rs.cycle(store, fc)
    assert out["unpublished"] and fc.docs == {}
    # a never-published expired draft is left alone
    r2 = rv.create(store, "acme-dealerships", slides=[e1], people=["Meier, Ben"])
    store.db.execute("UPDATE review SET expires_at = '2020-01-01 00:00:00' WHERE id = ?", (r2["id"],))
    store.db.commit()
    assert rs.cycle(store, fc)["published"] == []


def test_progress_from_the_app(store, projects_root):
    doc, obs, e1, e2 = setup(store, projects_root)
    r = rv.create(store, "acme-dealerships", slides=[e1, e2], people=["Keller, Anna", "Meier, Ben"])
    fc = Fake()
    rs.cycle(store, fc)
    people = {p["name"]: p for p in rv.get(store, r["id"])["reviewers"]}
    anna, ben = str(people["Anna Keller"]["id"]), str(people["Ben Meier"]["id"])
    fc.docs[f"reviews/{r['id']}/progress/{anna}"] = {"slides": [1, 2, 99]}       # 99: not a slide of the review
    fc.docs[f"reviews/{r['id']}/progress/{ben}"] = {"slides": [2]}
    fc.docs[f"reviews/{r['id']}/progress/12345"] = {"slides": [1]}              # not invited: ignored
    rs.cycle(store, fc)
    pr = {p["name"]: p for p in rv.progress(store, r["id"])["people"]}
    assert (pr["Anna Keller"]["done"], pr["Anna Keller"]["percent"], pr["Anna Keller"]["state"]) == (2, 100, "done")
    h = next(k for k, v in fc.docs.items() if k.startswith("access/") and v["reviewer"] == anna)
    assert fc.docs[h]["finished"] is True                                    # Anna is through: her link is read-only now
    assert next(v for k, v in fc.docs.items() if k.startswith("access/") and v["reviewer"] == ben)["finished"] is False
    assert rv.get(store, r["id"])["completed_at"] is None                    # Ben is still reviewing
    assert (pr["Ben Meier"]["done"], pr["Ben Meier"]["percent"], pr["Ben Meier"]["state"]) == (1, 50, "active")
    g = rv.get(store, r["id"])
    assert g["slides_list"][1]["reviewed_by"] == ["Anna Keller", "Ben Meier"] and {p["percent"] for p in g["progress"]} == {100, 50}
    st = rv.slide_status(store, ids=[e2])[e2]
    assert st["stage"] == "confirmed" and st["reviewed_by"] == ["Anna Keller", "Ben Meier"]
    # unmarking is synced too
    fc.docs[f"reviews/{r['id']}/progress/{anna}"] = {"slides": [1]}
    rs.cycle(store, fc)
    assert {p["name"]: p["done"] for p in rv.progress(store, r["id"])["people"]}["Anna Keller"] == 1
    rv.purge(store, r["id"])
    rs.cycle(store, fc)
    assert not any("/progress/" in k for k in fc.docs)
    assert rv.list_reviews(store, "acme-dealerships")[0]["progress"][0]["name"] == "Anna Keller"


def test_drawings_pass_through(store, projects_root):
    doc, obs, e1, e2 = setup(store, projects_root)
    r = rv.create(store, "acme-dealerships", slides=[e1], people=["Keller, Anna"])
    fc = Fake()
    rs.cycle(store, fc)
    anna = str(rv.get(store, r["id"])["reviewers"][0]["id"])
    shapes = {"a": {"tool": "arrow", "x1": 0.2, "y1": 0.75, "x2": 0.42, "y2": 0.45},
              "p": {"tool": "pen", "pts": [0.6, 0.3, 0.65, 0.32, 0.7, 0.4]},
              "e": {"tool": "ellipse", "x": 0.05, "y": 0.85, "w": 0.25, "h": 0.12}}
    for k, sh in shapes.items():
        fc.add_item(r["id"], k, slide=1, exhibit=e1, version=1, kind="shape", text=f"{k} note", author=anna, authorName="Anna Keller", shape=sh)
    assert rs.cycle(store, fc)["pulled"] == 3
    got = {i["text"][0]: i["shape"] for s in rv.inbox(store, review=r["id"])["slides"] for i in s["items"]}
    assert got == shapes


def test_pull_number_and_genuine_checks(store, projects_root):
    doc, obs, e1, e2 = setup(store, projects_root)
    store.db.execute("INSERT INTO exhibit_number(exhibit_id, version, number, claim, verdict) VALUES (?, 1, '82 %', 'dealers', 'sourced')", (e1,))
    store.db.commit()
    r = rv.create(store, "acme-dealerships", slides=[e1], people=["Keller, Anna"])
    fc = Fake()
    rs.cycle(store, fc)
    nums = fc.docs[f"reviews/{r['id']}/slides/1"]["numbers"]
    assert nums[0]["number"] == "82 %" and nums[0]["stage"] == "unchecked"
    anna = str(rv.get(store, r["id"])["reviewers"][0]["id"])
    base = dict(slide=1, exhibit=e1, version=1, kind="check", text="", author=anna, authorName="Anna Keller")
    fc.add_item(r["id"], "n1", **base, verdict="ok", target={"kind": "number", "id": str(nums[0]["id"])})
    fc.add_item(r["id"], "g1", **base, verdict="ok", target={"kind": "document", "id": str(doc)}, aspect="source")
    fc.add_item(r["id"], "n2", **base, verdict="ok", target={"kind": "number", "id": "999999"})          # not on this slide
    out = rs.cycle(store, fc)
    assert out["pulled"] == 2 and fc.docs[f"reviews/{r['id']}/items/n2"]["status"] == "rejected"
    assert rv.verification(store, "document", doc)["source"]["stage"] == "verified"
    rs.cycle(store, fc)
    assert fc.docs[f"reviews/{r['id']}/slides/1"]["numbers"][0]["stage"] == "verified"
    assert fc.docs[f"reviews/{r['id']}/slides/1"]["sources"][0]["source_stage"] == "verified"
