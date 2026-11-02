"""Fundus Cloud transport with a fake Firebase (Auth, Firestore, Storage REST) – never network. fundus.package is faked."""
import json
import sys
import types
import urllib.parse
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from fundus import cloud
from fundus import cloud_crypto as cc
from fundus import firebase as fb

FAST = {"alg": "argon2id", "v": 1, "t": 1, "m": 8192, "p": 1}


class FakeFirebase:
    """Just enough of identitytoolkit, securetoken, Firestore REST and Firebase Storage v0 – plus the few rules that
    matter for the flow (own documents only, packages visible to sender and recipients)."""

    def __init__(self):
        self.users, self.docs, self.blobs, self.calls = {}, {}, {}, []

    def _uid(self, headers):
        auth = (headers or {}).get("Authorization", "")
        tok = auth.split(" ", 1)[1] if " " in auth else ""
        assert tok.startswith("tok-"), "request without user token"
        return tok[4:]

    def _err(self, code, msg):
        raise cloud.HttpError(code, msg)

    def __call__(self, method, url, *, body=None, data=None, headers=None, out=None, timeout=120):
        self.calls.append((method, url.split("?")[0]))
        u = urllib.parse.urlsplit(url)
        qs = urllib.parse.parse_qs(u.query)
        if url.startswith(cloud.ID_TK):
            op = u.path.rsplit(":", 1)[1]
            email, pw = body["email"], body["password"]
            if op == "signUp":
                if email in self.users:
                    self._err(400, "EMAIL_EXISTS")
                self.users[email] = {"uid": f"U{len(self.users) + 1}x", "pw": pw}
            elif email not in self.users or self.users[email]["pw"] != pw:
                self._err(400, "INVALID_LOGIN_CREDENTIALS")
            uid = self.users[email]["uid"]
            return {"localId": uid, "email": email, "idToken": f"tok-{uid}", "refreshToken": f"ref-{uid}", "expiresIn": "3600"}
        if url.startswith(cloud.SECURE_TOKEN):
            ref = urllib.parse.parse_qs(data.decode())["refresh_token"][0]
            return {"id_token": "tok-" + ref[4:], "refresh_token": ref, "expires_in": "3600"}
        if url.startswith(cloud.FS):
            uid = self._uid(headers)
            path = u.path.split("/documents", 1)[1].lstrip("/")
            if path.endswith(":runQuery") or u.path.endswith("documents:runQuery"):
                q = body["structuredQuery"]
                coll, f = q["from"][0]["collectionId"], q["where"]["fieldFilter"]
                val = fb.dec(f["value"])
                rows = []
                for p, d in self.docs.items():
                    if not p.startswith(coll + "/"):
                        continue
                    v = d.get(f["field"]["fieldPath"])
                    ok = (val in (v or [])) if f["op"] == "ARRAY_CONTAINS" else v == val
                    if ok:
                        assert uid == d["from_uid"] or uid in d["to_uids"], "rules: list only own packages"
                        rows.append({"document": {"name": f"x/documents/{p}", "fields": fb.fields(d)}})
                return rows
            d = self.docs.get(path)
            if method == "GET":
                if d is None:
                    self._err(404, "NOT_FOUND")
                self._read_rule(uid, path, d)
                return {"name": f"projects/p/databases/(default)/documents/{path}", "fields": fb.fields(d)}
            if method == "DELETE":
                if d is not None:
                    assert path.startswith("cloud_packages/") and d["from_uid"] == uid
                    del self.docs[path]
                return {}
            if method == "PATCH":
                new = {k: fb.dec(v) for k, v in body["fields"].items()}
                if qs.get("currentDocument.exists") == ["false"] and d is not None:
                    self._err(409, "ALREADY_EXISTS")
                mask = qs.get("updateMask.fieldPaths")
                self._write_rule(uid, path, d, new, mask)
                if mask:
                    d = dict(d)
                    for m in mask:
                        if "." in m:
                            top, key = m.split(".", 1)
                            d[top] = {**(d.get(top) or {}), key.strip("`"): new[top][key.strip("`")]}
                        else:
                            d[m] = new[m]
                    self.docs[path] = d
                else:
                    self.docs[path] = new
                return {}
        if url.startswith(cloud.ST):
            uid = self._uid({"Authorization": headers["Authorization"]})
            if method == "POST":
                name = qs["name"][0]
                pid = name.split("/")[1].split(".")[0]
                assert self.docs[f"cloud_packages/{pid}"]["from_uid"] == uid
                chunks = []
                while True:
                    b = data.read(1 << 16)
                    if not b:
                        break
                    chunks.append(b)
                self.blobs[name] = b"".join(chunks)
                return {}
            name = urllib.parse.unquote(u.path.split("/o/", 1)[1])
            if name not in self.blobs:
                self._err(404, "Not Found")
            if method == "GET":
                with open(out, "wb") as f:
                    f.write(self.blobs[name])
                return None
            if method == "DELETE":
                del self.blobs[name]
                return {}
        raise AssertionError(f"unexpected {method} {url}")

    def _read_rule(self, uid, path, d):
        coll, did = path.split("/", 1)
        if coll == "cloud_users":
            assert did == uid
        elif coll == "cloud_packages":
            assert uid == d["from_uid"] or uid in d["to_uids"]

    def _write_rule(self, uid, path, old, new, mask):
        coll, did = path.split("/", 1)
        if coll == "cloud_users":
            assert did == uid
        elif coll == "cloud_directory":
            assert new["uid"] == uid and (old is None or old["uid"] == uid)
        elif coll == "cloud_packages":
            if old is None:
                assert new["from_uid"] == uid
            elif uid == old["from_uid"]:
                assert mask == ["ready"]
            else:
                assert uid in old["to_uids"] and mask == [f"picked_up.`{uid}`"]


@pytest.fixture
def fake(monkeypatch):
    f = FakeFirebase()
    monkeypatch.setattr(cloud, "http", f)
    monkeypatch.setattr(cc, "KDF_VERSIONS", {1: FAST})
    monkeypatch.delenv("FUNDUS_CLOUD_DIR", raising=False)
    return f


@pytest.fixture
def pkg(monkeypatch):
    """Fake fundus.package (team member B's module) with the agreed interface."""
    m = types.ModuleType("fundus.package")
    m.internal = []
    m.received, m.sent = [], []

    def export(store, refs, out_path, actor="cli", include_skill=True):
        with zipfile.ZipFile(out_path, "w") as z:
            z.writestr("manifest.json", json.dumps({"refs": refs}))
            z.writestr("dummy.txt", "Testinhalt " * 5000)
        return {"path": out_path, "manifest": {"title": "Testpaket", "slides": [{"ref": r} for r in refs], "refs": refs}}

    def internal_report(store, refs):
        return {"internal": list(m.internal), "count": len(m.internal)}

    def receive(store, path, sender=None, sender_fingerprint=None):
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
        m.received.append({"sender": sender, "fp": sender_fingerprint, "names": names})
        return {"received_id": len(m.received)}

    def record_sent(store, manifest, recipients, transport="cloud"):
        m.sent.append({"manifest": manifest, "recipients": recipients, "transport": transport})
        return len(m.sent)

    m.export, m.internal_report, m.receive, m.record_sent = export, internal_report, receive, record_sent
    monkeypatch.setitem(sys.modules, "fundus.package", m)
    import fundus
    monkeypatch.setattr(fundus, "package", m, raising=False)
    return m


def _store(tmp_path, name):
    from fundus import Store
    d = tmp_path / name
    d.mkdir()
    return Store(d / "fundus.sqlite")


def test_connect_register_then_second_machine(tmp_path, fake):
    a1 = tmp_path / "m1" / "cloud"
    r = cloud.connect(a1, "Alice@Example.invalid", "ein langes Passwort 1")
    assert r["created"] and r["recovery_code"] and len(r["fingerprint"].split()) == 6
    assert oct((a1 / "identity.json").stat().st_mode & 0o777) == "0o600"
    assert oct((a1 / "session.json").stat().st_mode & 0o777) == "0o600"
    rec = fake.docs[f"cloud_users/{r['uid']}"]
    assert rec["email"] == "alice@example.invalid" and rec["kdf"] == FAST
    assert "box_sk" not in json.dumps(rec, default=str)                    # only wrapped private keys in the cloud
    # Firebase never sees the real password
    assert fake.users["alice@example.invalid"]["pw"] != "ein langes Passwort 1"
    assert fake.docs[f"cloud_directory/{cc.email_hash('alice@example.invalid')}"]["uid"] == r["uid"]
    # the recovery code unwraps the same identity
    ident = cc.unwrap(rec["wrapped_recovery"], cc.recovery_key(r["recovery_code"]))
    assert ident["fingerprint"] == r["fingerprint"]
    # second machine: same identity, no new recovery code
    r2 = cloud.connect(tmp_path / "m2" / "cloud", "alice@example.invalid", "ein langes Passwort 1")
    assert not r2["created"] and "recovery_code" not in r2 and r2["fingerprint"] == r["fingerprint"]
    with pytest.raises(PermissionError):
        cloud.connect(tmp_path / "m3" / "cloud", "alice@example.invalid", "falsches Passwort 1")
    with pytest.raises(ValueError):
        cloud.connect(tmp_path / "m3" / "cloud", "alice@example.invalid", "kurz")
    st = cloud.status(a1)
    assert st["connected"] and st["email"] == "alice@example.invalid"
    cloud.disconnect(a1)
    assert not cloud.status(a1)["connected"] and not (a1 / "identity.json").exists()


def test_share_poll_round_trip_and_cleanup(tmp_path, fake, pkg):
    sa, sb = _store(tmp_path, "a"), _store(tmp_path, "b")
    ra = cloud.connect(cloud.cloud_dir(sa), "a@example.invalid", "passwort-alice-1")
    rb = cloud.connect(cloud.cloud_dir(sb), "b@example.invalid", "passwort-bob-12")
    look = cloud.lookup(cloud.cloud_dir(sa), ["b@example.invalid", "niemand@example.invalid"])["recipients"]
    assert look[0]["registered"] and look[0]["fingerprint"] == rb["fingerprint"] and not look[0]["confirmed"]
    assert not look[1]["registered"]
    phases = []
    res = cloud.share(sa, ["set:proj/1"], ["B@example.invalid"], progress=lambda p, d, t: phases.append(p))
    assert res["package_id"] and res["sent_id"] == 1 and pkg.sent[0]["recipients"] == ["b@example.invalid"]
    assert {"export", "encrypt", "upload", "done"} <= set(phases)
    doc = fake.docs[f"cloud_packages/{res['package_id']}"]
    assert doc["ready"] and doc["to_uids"] == [rb["uid"]] and doc["expires"] - doc["created"] == timedelta(days=30)
    blob = fake.blobs[f"cloud_packages/{res['package_id']}.bin"]
    assert b"Testinhalt" not in blob                           # Google sees no content
    # recipient picks up
    out = cloud.poll(sb)
    assert len(out["received"]) == 1 and not out["errors"]
    got = out["received"][0]
    assert got["confirmed"] is False and "nicht bestätigt" in got["warning"]
    assert pkg.received[0]["sender"] == "a@example.invalid" and pkg.received[0]["fp"] == ra["fingerprint"]
    assert "dummy.txt" in pkg.received[0]["names"]
    assert rb["uid"] in fake.docs[f"cloud_packages/{res['package_id']}"]["picked_up"]
    assert cloud.poll(sb)["received"] == []                    # not twice
    # sender cleans up after pickup
    assert cloud.poll(sa)["cleaned"] == 1
    assert f"cloud_packages/{res['package_id']}" not in fake.docs and not fake.blobs
    log = cloud.inbox(cloud.cloud_dir(sa))["packages"]
    assert log[0]["state"] == "delivered"
    inb = cloud.inbox(cloud.cloud_dir(sb))["packages"]
    assert inb[0]["state"] == "received" and inb[0]["confirmed"] is False and inb[0]["title"] == "Testpaket"
    # confirm the contact → next packages are marked confirmed
    cloud.set_contact(cloud.cloud_dir(sb), "a@example.invalid", "confirm")
    cloud.share(sa, ["exhibit:5"], ["b@example.invalid"])
    assert cloud.poll(sb)["received"][0]["confirmed"] is True
    assert cloud.contacts(cloud.cloud_dir(sb))["contacts"][0]["confirmed"] is True


def test_internal_needs_confirmation(tmp_path, fake, pkg):
    sa, sb = _store(tmp_path, "a"), _store(tmp_path, "b")
    cloud.connect(cloud.cloud_dir(sa), "a@example.invalid", "passwort-alice-1")
    cloud.connect(cloud.cloud_dir(sb), "b@example.invalid", "passwort-bob-12")
    pkg.internal = [{"ref": "exhibit:7", "title": "Kundenfolie", "reasons": ["visibility internal"]}]
    r = cloud.share(sa, ["exhibit:7"], ["b@example.invalid"])
    assert r["needs_confirmation"] and r["internal_count"] == 1 and not pkg.sent and not fake.blobs
    r = cloud.share(sa, ["exhibit:7"], ["b@example.invalid"], confirm_internal=True)
    assert r["package_id"] and r["internal_count"] == 1


def test_unregistered_recipient_and_self(tmp_path, fake, pkg):
    sa = _store(tmp_path, "a")
    cloud.connect(cloud.cloud_dir(sa), "a@example.invalid", "passwort-alice-1")
    with pytest.raises(LookupError):
        cloud.share(sa, ["exhibit:1"], ["x@example.invalid"])
    with pytest.raises(ValueError):
        cloud.share(sa, ["exhibit:1"], ["a@example.invalid"])
    with pytest.raises(cloud.CloudError):
        cloud.share(_store(tmp_path, "c"), ["exhibit:1"], ["a@example.invalid"])   # not connected


def test_key_change_holds_package_until_accepted(tmp_path, fake, pkg):
    sa, sb = _store(tmp_path, "a"), _store(tmp_path, "b")
    cloud.connect(cloud.cloud_dir(sa), "a@example.invalid", "passwort-alice-1")
    cloud.connect(cloud.cloud_dir(sb), "b@example.invalid", "passwort-bob-12")
    cloud.lookup(cloud.cloud_dir(sb), ["a@example.invalid"])          # b knows a's first key
    cloud.set_contact(cloud.cloud_dir(sb), "a@example.invalid", "confirm")
    # a's keys change (e.g. someone re-created the identity): new keys in the directory and locally
    ca = cloud.cloud_dir(sa)
    ident = json.loads((ca / "identity.json").read_text())
    new = cc.new_identity()
    ident.update({k: new[k] for k in ("box_sk", "sign_seed", "box_pk", "sign_pk", "fingerprint")})
    cc.write_private(ca / "identity.json", ident)
    key = f"cloud_directory/{cc.email_hash('a@example.invalid')}"
    fake.docs[key].update({k: new[k] for k in ("box_pk", "sign_pk", "fingerprint")})
    cloud.share(sa, ["exhibit:1"], ["b@example.invalid"])
    out = cloud.poll(sb)
    assert out["received"] == [] and len(out["held"]) == 1 and "geändert" in out["held"][0]["warning"]
    assert not pkg.received
    c = cloud.contacts(cloud.cloud_dir(sb))["contacts"][0]
    assert c["key_changed"] and c["new_fingerprint"] == new["fingerprint"]
    with pytest.raises(ValueError):
        cloud.set_contact(cloud.cloud_dir(sb), "a@example.invalid", "confirm")
    # sending to a contact whose key changed is refused too
    with pytest.raises(PermissionError):
        cloud.share(sb, ["exhibit:2"], ["a@example.invalid"])
    cloud.set_contact(cloud.cloud_dir(sb), "a@example.invalid", "accept_key")
    out = cloud.poll(sb)
    assert len(out["received"]) == 1 and out["received"][0]["confirmed"] is False


def test_tampered_blob_is_rejected(tmp_path, fake, pkg):
    sa, sb = _store(tmp_path, "a"), _store(tmp_path, "b")
    cloud.connect(cloud.cloud_dir(sa), "a@example.invalid", "passwort-alice-1")
    cloud.connect(cloud.cloud_dir(sb), "b@example.invalid", "passwort-bob-12")
    res = cloud.share(sa, ["exhibit:1"], ["b@example.invalid"])
    name = f"cloud_packages/{res['package_id']}.bin"
    raw = bytearray(fake.blobs[name])
    raw[-50] ^= 0xFF
    fake.blobs[name] = bytes(raw)
    for _ in range(4):
        out = cloud.poll(sb)
    assert not pkg.received
    log = cloud.inbox(cloud.cloud_dir(sb))["packages"][0]
    assert log["state"] == "failed" and log["attempts"] == 3
    assert not list((cloud.cloud_dir(sb) / "tmp").glob("*/*"))       # no plaintext left behind
    assert out["errors"] == []                                        # gave up after three attempts


def test_expired_packages_are_cleaned(tmp_path, fake, pkg):
    sa, sb = _store(tmp_path, "a"), _store(tmp_path, "b")
    cloud.connect(cloud.cloud_dir(sa), "a@example.invalid", "passwort-alice-1")
    cloud.connect(cloud.cloud_dir(sb), "b@example.invalid", "passwort-bob-12")
    res = cloud.share(sa, ["exhibit:1"], ["b@example.invalid"])
    fake.docs[f"cloud_packages/{res['package_id']}"]["expires"] = datetime.now(timezone.utc) - timedelta(days=1)
    assert cloud.poll(sb)["received"] == []
    assert cloud.poll(sa)["cleaned"] == 1 and not fake.blobs


def test_mcp_tool_and_rest(tmp_path, fake, pkg, monkeypatch):
    from fundus import mcp_server as m
    from test_reviews import call
    sa = _store(tmp_path, "a")
    m.configure(sa.path)
    try:
        assert json.loads(m.fundus_cloud("status"))["connected"] is False
        assert "error" in json.loads(m.fundus_cloud("share", refs=["exhibit:1"], recipients=["b@example.invalid"]))
        code, r = call(m, m.cloud_api, "POST", "/api/cloud/connect", path_params={"sub": "connect"},
                       body={"email": "a@example.invalid", "password": "passwort-alice-1"})
        assert code == 200 and r["created"] and r["recovery_code"]
        code, r = call(m, m.cloud_api, "POST", "/api/cloud/connect", path_params={"sub": "connect"},
                       body={"email": "a@example.invalid", "password": "passwort-alice-1"}, origin="http://evil.example")
        assert code == 403
        code, s = call(m, m.cloud_api, path="/api/cloud/status", path_params={"sub": "status"})
        assert code == 200 and s["connected"] and s["fingerprint"] == r.get("fingerprint", s["fingerprint"])
        sb = _store(tmp_path, "b")
        cloud.connect(cloud.cloud_dir(sb), "b@example.invalid", "passwort-bob-12")
        code, lk = call(m, m.cloud_api, "POST", "/api/cloud/lookup", path_params={"sub": "lookup"},
                        body={"emails": ["b@example.invalid", "nein@example.invalid"]})
        assert code == 200 and [x["registered"] for x in lk["recipients"]] == [True, False]
        code, chk = call(m, m.cloud_api, "POST", "/api/cloud/share", path_params={"sub": "share"},
                         body={"action": "check", "refs": ["exhibit:1"]})
        assert code == 200 and chk["internal_count"] == 0
        out = json.loads(m.fundus_cloud("share", refs=["exhibit:1"], recipients=["b@example.invalid"]))
        assert out["package_id"]
        code, job = call(m, m.cloud_api, "POST", "/api/cloud/share", path_params={"sub": "share"},
                         body={"action": "start", "refs": ["exhibit:2"], "recipients": ["b@example.invalid"]})
        assert code == 200 and job["job"]
        import time
        for _ in range(100):
            code, j = call(m, m.cloud_api, path="/api/cloud/share", query=f"job={job['job']}".encode(), path_params={"sub": "share"})
            if j["state"] != "running":
                break
            time.sleep(0.05)
        assert j["state"] == "done" and j["result"]["package_id"]
        code, c = call(m, m.cloud_api, path="/api/cloud/contacts", path_params={"sub": "contacts"})
        assert code == 200 and c["contacts"][0]["email"] == "b@example.invalid"
        code, c = call(m, m.cloud_api, "POST", "/api/cloud/contacts", path_params={"sub": "contacts"},
                       body={"email": "b@example.invalid", "action": "confirm"})
        assert code == 200 and c["confirmed"]
        inbox = json.loads(m.fundus_cloud("inbox"))
        assert len(inbox["packages"]) == 2
        code, _ = call(m, m.cloud_api, "POST", "/api/cloud/disconnect", path_params={"sub": "disconnect"}, body={})
        assert code == 200 and json.loads(m.fundus_cloud("status"))["connected"] is False
    finally:
        m.configure(m.DEFAULT_DB)
