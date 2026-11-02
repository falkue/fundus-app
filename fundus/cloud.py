"""Fundus Cloud: local Fundus installations exchange slide packages end-to-end encrypted (one package per send).

The Firebase project fundus-review is only a mailbox and an address book; it never sees content or real passwords.
Only end-user auth is used here (Firebase Auth REST with the public web API key, then Firestore/Storage REST under the
security rules) – never the service account, so other people's Fundus installations can use the same code.

Local state lives in <data dir>/cloud/ (next to the database; FUNDUS_CLOUD_DIR overrides), never in git:
  identity.json   own key pairs and kdf parameters (0600)
  session.json    Firebase refresh token (0600)
  cloud.sqlite    contacts (trust on first use, confirmed by comparing the 6 fingerprint words by phone), package log, meta
  tmp/            working files of a running send or pickup

Firestore (rules in review-app/firestore.rules, all require a password sign-in):
  cloud_users/{uid}                 email, public keys, private keys wrapped twice (password, recovery code), kdf params
  cloud_directory/{sha256(email)}   uid and public keys – get only (no listing), to find a recipient by email
  cloud_packages/{id}               from_uid/from_email, to_uids, created, expires (+30 days, TTL), size, path, sealed keys
                                    per uid, ready, picked_up {uid: time}
Storage: cloud_packages/{id}.bin (the encrypted package; lifecycle deletes after 30 days).

Crypto: fundus/cloud_crypto.py. Packages: fundus/package.py (export/receive/record_sent; imported lazily).
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fundus import cloud_crypto as cc
from fundus import firebase as fb

# Public Firebase web API key of fundus-review (same as review-app/public/config.js; public by design – it only names the
# project, access is governed by the security rules).
API_KEY = os.environ.get("FUNDUS_CLOUD_API_KEY", "AIzaSyB-xNgMJxcCWYo1ogcL0sUvqKkp89hSac8")
PROJECT = os.environ.get("FUNDUS_CLOUD_PROJECT", fb.PROJECT)
BUCKET = os.environ.get("FUNDUS_CLOUD_BUCKET", fb.BUCKET)
TTL_DAYS = 30
MAX_BYTES = 500 * 1024 * 1024
MAX_RECIPIENTS = 20
POLL_SECONDS = int(os.environ.get("FUNDUS_CLOUD_POLL_SECONDS", "180"))
ID_TK = "https://identitytoolkit.googleapis.com/v1/accounts:"
SECURE_TOKEN = "https://securetoken.googleapis.com/v1/token"
FS = "https://firestore.googleapis.com/v1/"
ST = "https://firebasestorage.googleapis.com/v0/b/"


class CloudError(RuntimeError):
    pass


class HttpError(CloudError):
    def __init__(self, code: int, message: str, payload=None):
        super().__init__(f"HTTP {code}: {message}")
        self.code, self.message, self.payload = code, message, payload


# ---------------------------------------------------------------- HTTP (the one place tests replace)
def http(method: str, url: str, *, body=None, data=None, headers: dict | None = None, out=None, timeout: int = 120):
    """One request. body → JSON; data: bytes or a file object (then pass Content-Length); out: path to stream the response
    into. Returns the parsed JSON (or {} / None for out). Raises HttpError with the server's error message."""
    h = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if out is not None:
                with open(out, "wb") as f:
                    shutil.copyfileobj(r, f, 1 << 20)
                return None
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        err = payload.get("error") if isinstance(payload, dict) else None
        msg = (err.get("message") if isinstance(err, dict) else err) or raw[:300]
        raise HttpError(e.code, str(msg), payload) from None
    except urllib.error.URLError as e:
        raise CloudError(f"keine Verbindung zu {urllib.parse.urlsplit(url).netloc}: {e.reason}") from None
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------- local state
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(cdir: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(cdir), threading.Lock())


def cloud_dir(store_or_path) -> Path:
    if os.environ.get("FUNDUS_CLOUD_DIR"):
        return Path(os.environ["FUNDUS_CLOUD_DIR"])
    p = getattr(store_or_path, "path", store_or_path)
    return Path(p).parent / "cloud"


SCHEMA = """
CREATE TABLE IF NOT EXISTS contact (
  email TEXT PRIMARY KEY, uid TEXT NOT NULL, box_pk TEXT NOT NULL, sign_pk TEXT NOT NULL, fingerprint TEXT NOT NULL,
  first_seen TEXT NOT NULL, confirmed_at TEXT,
  new_box_pk TEXT, new_sign_pk TEXT, new_fingerprint TEXT, new_uid TEXT, key_changed_at TEXT,   -- directory shows other keys
  note TEXT
);
CREATE TABLE IF NOT EXISTS package (
  id TEXT PRIMARY KEY, direction TEXT NOT NULL,          -- 'out' | 'in'
  peer TEXT NOT NULL,                                    -- sender email (in) or JSON list of recipient emails (out)
  created TEXT, size INTEGER, state TEXT NOT NULL,       -- out: uploading|sent|delivered|expired|failed; in: received|held_key|failed
  confirmed INTEGER, title TEXT, slides INTEGER, sent_id INTEGER, received_id INTEGER, error TEXT, attempts INTEGER DEFAULT 0,
  updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _db(cdir: Path) -> sqlite3.Connection:
    cdir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(cdir, 0o700)
    except OSError:
        pass
    con = sqlite3.connect(cdir / "cloud.sqlite", timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _meta(cdir: Path, key: str, value=None):
    with _db(cdir) as con:
        if value is None:
            r = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return r[0] if r else None
        con.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value)))


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _log(cdir: Path, pid: str, direction: str, peer: str, state: str, **kw) -> None:
    cols = {"created": None, "size": None, "confirmed": None, "title": None, "slides": None, "sent_id": None,
            "received_id": None, "error": None}
    cols.update({k: v for k, v in kw.items() if k in cols})
    with _db(cdir) as con:
        old = con.execute("SELECT * FROM package WHERE id = ?", (pid,)).fetchone()
        if old:
            sets = {k: v for k, v in kw.items() if k in cols}
            sets["state"], sets["updated"] = state, _now()
            if kw.get("attempt"):
                con.execute("UPDATE package SET attempts = attempts + 1 WHERE id = ?", (pid,))
            con.execute(f"UPDATE package SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", (*sets.values(), pid))
        else:
            con.execute("INSERT INTO package(id, direction, peer, state, updated, attempts, " + ", ".join(cols) + ") VALUES ("
                        + ", ".join("?" * (6 + len(cols))) + ")",
                        (pid, direction, peer, state, _now(), 1 if kw.get("attempt") else 0, *cols.values()))


# ---------------------------------------------------------------- Firebase Auth
def _auth(op: str, body: dict) -> dict:
    try:
        return http("POST", f"{ID_TK}{op}?key={API_KEY}", body=body)
    except HttpError as e:
        raise HttpError(e.code, e.message.split(" : ")[0].strip(), e.payload) from None


class Session:
    """Signed-in Firebase user; refreshes the ID token from the stored refresh token."""

    def __init__(self, cdir: Path, uid: str, email: str, refresh_token: str, id_token: str | None = None, expires_in=0):
        self.cdir, self.uid, self.email, self.refresh_token = cdir, uid, email, refresh_token
        self.id_token, self.exp = id_token, time.time() + int(expires_in or 0)

    @classmethod
    def from_auth(cls, cdir: Path, r: dict) -> "Session":
        s = cls(cdir, r["localId"], r.get("email", ""), r["refreshToken"], r["idToken"], r.get("expiresIn", 3600))
        s.save()
        return s

    @classmethod
    def load(cls, cdir: Path) -> "Session | None":
        d = _read_json(cdir / "session.json")
        return cls(cdir, d["uid"], d["email"], d["refresh_token"]) if d and d.get("refresh_token") else None

    def save(self) -> None:
        cc.write_private(self.cdir / "session.json", {"uid": self.uid, "email": self.email, "refresh_token": self.refresh_token})

    def token(self) -> str:
        if not self.id_token or time.time() > self.exp - 120:
            try:
                r = http("POST", f"{SECURE_TOKEN}?key={API_KEY}",
                         data=urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": self.refresh_token}).encode(),
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            except HttpError as e:
                raise CloudError(f"Anmeldung abgelaufen ({e.message}) – bitte neu verbinden") from None
            self.id_token, self.exp = r["id_token"], time.time() + int(r.get("expires_in", 3600))
            if r.get("refresh_token") and r["refresh_token"] != self.refresh_token:
                self.refresh_token = r["refresh_token"]
                self.save()
        return self.id_token

    # ------------------------------------------------------------ Firestore REST (rules apply)
    def _doc_url(self, path: str) -> str:
        return f"{FS}projects/{PROJECT}/databases/(default)/documents/{path}"

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}"}

    def get(self, path: str) -> dict | None:
        try:
            d = http("GET", self._doc_url(path), headers=self._h())
        except HttpError as e:
            if e.code == 404:
                return None
            raise
        return fb.from_doc(d)

    def put(self, path: str, data: dict, must_exist: bool | None = None, mask: list[str] | None = None) -> None:
        q = []
        if must_exist is not None:
            q.append(f"currentDocument.exists={'true' if must_exist else 'false'}")
        for f in mask or []:
            q.append("updateMask.fieldPaths=" + urllib.parse.quote(f, safe=""))
        http("PATCH", self._doc_url(path) + ("?" + "&".join(q) if q else ""), body={"fields": fb.fields(data)}, headers=self._h())

    def delete(self, path: str) -> None:
        try:
            http("DELETE", self._doc_url(path), headers=self._h())
        except HttpError as e:
            if e.code != 404:
                raise

    def query(self, collection: str, field: str, op: str, value) -> list[dict]:
        body = {"structuredQuery": {"from": [{"collectionId": collection}],
                                    "where": {"fieldFilter": {"field": {"fieldPath": field}, "op": op, "value": fb.enc(value)}}}}
        rows = http("POST", f"{FS}projects/{PROJECT}/databases/(default)/documents:runQuery", body=body, headers=self._h()) or []
        return [fb.from_doc(r["document"]) for r in rows if r.get("document")]

    # ------------------------------------------------------------ Storage REST (rules apply)
    def _obj(self, name: str) -> str:
        return f"{ST}{BUCKET}/o/{urllib.parse.quote(name, safe='')}"

    def _sh(self) -> dict:
        return {"Authorization": f"Firebase {self.token()}"}

    def upload(self, name: str, path: Path, progress=None) -> None:
        size = path.stat().st_size

        class Reader:                       # file object that reports progress while urllib reads it
            def __init__(self, f):
                self.f, self.n = f, 0

            def read(self, k=-1):
                b = self.f.read(1 << 20 if k is None or k < 0 else k)
                self.n += len(b)
                if progress:
                    progress(self.n, size)
                return b

        with open(path, "rb") as f:
            http("POST", f"{ST}{BUCKET}/o?name={urllib.parse.quote(name, safe='')}", data=Reader(f), timeout=1800,
                 headers={**self._sh(), "Content-Type": "application/octet-stream", "Content-Length": str(size)})

    def download(self, name: str, dst: Path) -> None:
        http("GET", self._obj(name) + "?alt=media", headers=self._sh(), out=dst, timeout=1800)

    def remove(self, name: str) -> None:
        try:
            http("DELETE", self._obj(name), headers=self._sh())
        except HttpError as e:
            if e.code != 404:
                raise


# ---------------------------------------------------------------- connect / disconnect / status
def _identity(cdir: Path) -> dict | None:
    return _read_json(cdir / "identity.json")


def _require(cdir: Path) -> tuple[Session, dict]:
    sess, ident = Session.load(cdir), _identity(cdir)
    if not sess or not ident or ident.get("uid") != sess.uid:
        raise CloudError("nicht mit Fundus Cloud verbunden – im Portal unter Verwaltung verbinden")
    return sess, ident


def _ts(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def connect(cdir, email: str, password: str) -> dict:
    """Sign in with email + password, or create the account if it does not exist. Returns email, uid, fingerprint, created
    and – only for a new account – the recovery code (show it once, it is not stored anywhere)."""
    cdir = Path(cdir)
    email = cc.norm_email(email)
    if not password or len(password) < 10:
        raise ValueError("Passwort: mindestens 10 Zeichen")
    with _lock(cdir):
        derived = {}
        r = None
        for ver in sorted(cc.KDF_VERSIONS, reverse=True):
            derived[ver] = cc.derive(password, email, cc.KDF_VERSIONS[ver])
            try:
                r = _auth("signInWithPassword", {"email": email, "password": derived[ver][0], "returnSecureToken": True})
                kdf_ver = ver
                break
            except HttpError as e:
                if e.message not in ("INVALID_LOGIN_CREDENTIALS", "INVALID_PASSWORD", "EMAIL_NOT_FOUND"):
                    raise CloudError(f"Anmeldung fehlgeschlagen: {e.message}") from None
        created = False
        if r is None:
            kdf_ver = cc.KDF_CURRENT
            try:
                r = _auth("signUp", {"email": email, "password": derived[kdf_ver][0], "returnSecureToken": True})
            except HttpError as e:
                if e.message == "EMAIL_EXISTS":
                    raise PermissionError("Für diese E-Mail gibt es schon ein Konto – das Passwort stimmt nicht") from None
                raise CloudError(f"Registrierung fehlgeschlagen: {e.message}") from None
            created = True
        wrap_key = derived[kdf_ver][1]
        sess = Session.from_auth(cdir, r)
        rec = sess.get(f"cloud_users/{sess.uid}")
        code = None
        if rec and rec.get("wrapped_pw"):
            ident = cc.unwrap(rec["wrapped_pw"], wrap_key)
            if ident["box_pk"] != rec.get("box_pk") or ident["sign_pk"] != rec.get("sign_pk"):
                raise CloudError("Kontodaten in der Cloud widersprüchlich (öffentliche Schlüssel passen nicht)")
        else:
            ident = cc.new_identity()
            code = cc.new_recovery_code()
            sess.put(f"cloud_users/{sess.uid}", {
                "email": email, "box_pk": ident["box_pk"], "sign_pk": ident["sign_pk"], "fingerprint": ident["fingerprint"],
                "wrapped_pw": cc.wrap(ident, wrap_key), "wrapped_recovery": cc.wrap(ident, cc.recovery_key(code)),
                "kdf": cc.KDF_VERSIONS[kdf_ver], "created": _ts(datetime.now(timezone.utc)), "app": "fundus"})
        sess.put(f"cloud_directory/{cc.email_hash(email)}", {
            "uid": sess.uid, "box_pk": ident["box_pk"], "sign_pk": ident["sign_pk"], "fingerprint": ident["fingerprint"],
            "updated": _ts(datetime.now(timezone.utc))})
        cc.write_private(cdir / "identity.json", {**ident, "email": email, "uid": sess.uid, "kdf": cc.KDF_VERSIONS[kdf_ver],
                                                  "connected_at": _now()})
        _meta(cdir, "last_error", "")
    out = {"connected": True, "created": created, "email": email, "uid": sess.uid, "fingerprint": ident["fingerprint"]}
    if code:
        out["recovery_code"] = code
    return out


def disconnect(cdir) -> dict:
    """Forget the session and the local copy of the keys (they stay wrapped in the cloud; connecting again restores them).
    Contacts and the package log stay."""
    cdir = Path(cdir)
    with _lock(cdir):
        for n in ("session.json", "identity.json"):
            (cdir / n).unlink(missing_ok=True)
    return {"connected": False}


def status(cdir) -> dict:
    cdir = Path(cdir)
    ident, sess = _identity(cdir), Session.load(cdir)
    connected = bool(ident and sess and ident.get("uid") == sess.uid)
    out = {"connected": connected, "project": PROJECT}
    if connected:
        out.update({k: ident.get(k) for k in ("email", "uid", "fingerprint", "connected_at")})
    if (cdir / "cloud.sqlite").exists():
        with _db(cdir) as con:
            out["contacts"] = con.execute("SELECT COUNT(*) FROM contact").fetchone()[0]
            out["key_changes"] = con.execute("SELECT COUNT(*) FROM contact WHERE key_changed_at IS NOT NULL").fetchone()[0]
            out["packages"] = {f"{r[0]}_{r[1]}": r[2] for r in con.execute(
                "SELECT direction, state, COUNT(*) FROM package GROUP BY direction, state")}
        out["last_poll"] = _meta(cdir, "last_poll")
        out["last_error"] = _meta(cdir, "last_error") or None
    out["poll_seconds"] = POLL_SECONDS
    return out


# ---------------------------------------------------------------- contacts
def _contact_row(r) -> dict:
    d = dict(r)
    d["confirmed"] = bool(d.get("confirmed_at"))
    d["key_changed"] = bool(d.get("key_changed_at"))
    for k in ("box_pk", "sign_pk", "new_box_pk", "new_sign_pk", "new_uid"):
        d.pop(k, None)
    return d


def contacts(cdir) -> dict:
    with _db(Path(cdir)) as con:
        return {"contacts": [_contact_row(r) for r in con.execute("SELECT * FROM contact ORDER BY email")]}


def set_contact(cdir, email: str, action: str) -> dict:
    """action: confirm (after comparing the 6 words by phone) | unconfirm | accept_key (take the new key the directory shows;
    resets the confirmation) | remove."""
    email = cc.norm_email(email)
    with _db(Path(cdir)) as con:
        row = con.execute("SELECT * FROM contact WHERE email = ?", (email,)).fetchone()
        if not row:
            raise LookupError(f"kein Kontakt {email}")
        if action == "confirm":
            if row["key_changed_at"]:
                raise ValueError("Schlüssel geändert: erst den neuen Schlüssel annehmen und die Wörter neu vergleichen")
            con.execute("UPDATE contact SET confirmed_at = ? WHERE email = ?", (_now(), email))
        elif action == "unconfirm":
            con.execute("UPDATE contact SET confirmed_at = NULL WHERE email = ?", (email,))
        elif action == "accept_key":
            if not row["key_changed_at"]:
                raise ValueError("kein geänderter Schlüssel")
            con.execute("UPDATE contact SET box_pk = new_box_pk, sign_pk = new_sign_pk, fingerprint = new_fingerprint, "
                        "uid = new_uid, new_box_pk = NULL, new_sign_pk = NULL, new_fingerprint = NULL, new_uid = NULL, "
                        "key_changed_at = NULL, "
                        "confirmed_at = NULL, note = ? WHERE email = ?", (f"neuer Schlüssel angenommen {_now()}", email))
            con.execute("UPDATE package SET state = 'pending', error = NULL WHERE direction = 'in' AND peer = ? AND state = 'held_key'",
                        (email,))
        elif action == "remove":
            con.execute("DELETE FROM contact WHERE email = ?", (email,))
            return {"removed": email}
        else:
            raise ValueError("action: confirm | unconfirm | accept_key | remove")
        return _contact_row(con.execute("SELECT * FROM contact WHERE email = ?", (email,)).fetchone())


def _directory(sess: Session, email: str) -> dict | None:
    d = sess.get(f"cloud_directory/{cc.email_hash(email)}")
    if not d:
        return None
    if cc.fingerprint(d["box_pk"], d["sign_pk"]) != d.get("fingerprint"):
        raise CloudError(f"Adressbuch-Eintrag von {email} ist inkonsistent")
    return d


def _trust(cdir: Path, email: str, d: dict) -> dict:
    """Trust on first use: store a new contact; if the directory now shows other keys than at first contact, keep the old
    keys, note the new ones and mark the contact 'key changed' (nothing is sent to or accepted from it until accepted)."""
    with _db(cdir) as con:
        row = con.execute("SELECT * FROM contact WHERE email = ?", (email,)).fetchone()
        if not row:
            con.execute("INSERT INTO contact(email, uid, box_pk, sign_pk, fingerprint, first_seen) VALUES (?, ?, ?, ?, ?, ?)",
                        (email, d["uid"], d["box_pk"], d["sign_pk"], d["fingerprint"], _now()))
        elif (row["box_pk"], row["sign_pk"], row["uid"]) != (d["box_pk"], d["sign_pk"], d["uid"]):
            if (row["new_sign_pk"], row["new_box_pk"], row["new_uid"]) != (d["sign_pk"], d["box_pk"], d["uid"]):
                con.execute("UPDATE contact SET new_box_pk = ?, new_sign_pk = ?, new_fingerprint = ?, new_uid = ?, key_changed_at = ? "
                            "WHERE email = ?", (d["box_pk"], d["sign_pk"], d["fingerprint"], d["uid"], _now(), email))
        elif row["key_changed_at"]:
            con.execute("UPDATE contact SET new_box_pk = NULL, new_sign_pk = NULL, new_fingerprint = NULL, new_uid = NULL, "
                        "key_changed_at = NULL WHERE email = ?", (email,))
        return dict(con.execute("SELECT * FROM contact WHERE email = ?", (email,)).fetchone())


def lookup(cdir, emails: list[str]) -> dict:
    """Which addresses have a Fundus Cloud account – with fingerprint and the local trust state."""
    cdir = Path(cdir)
    sess, ident = _require(cdir)
    out = []
    for raw in emails or []:
        try:
            email = cc.norm_email(raw)
        except ValueError as e:
            out.append({"email": raw, "registered": False, "error": str(e)})
            continue
        if email == ident["email"]:
            out.append({"email": email, "registered": True, "self": True, "fingerprint": ident["fingerprint"]})
            continue
        d = _directory(sess, email)
        if not d:
            out.append({"email": email, "registered": False})
            continue
        c = _trust(cdir, email, d)
        out.append({"email": email, "registered": True, "fingerprint": c["fingerprint"], "confirmed": bool(c["confirmed_at"]),
                    "key_changed": bool(c["key_changed_at"]), "new_fingerprint": c["new_fingerprint"], "first_seen": c["first_seen"]})
    return {"recipients": out}


# ---------------------------------------------------------------- send
def _package():
    from fundus import package    # team member B's module; imported lazily
    return package


def check_share(store, refs: list[str]) -> dict:
    """What would be sent: internal slides with reasons (they need an explicit confirmation)."""
    if not refs:
        raise ValueError("refs fehlen (project:<p>, set:<p>/<set>, exhibit:<id>)")
    rep = _package().internal_report(store, refs)
    return {"refs": refs, "internal": rep.get("internal", []), "internal_count": rep.get("count", len(rep.get("internal", [])))}


def share(store, refs: list[str], recipients: list[str], confirm_internal: bool = False, actor: str = "portal",
          progress=None) -> dict:
    """Export the slides, encrypt for every recipient, upload, record the send. Internal slides need confirm_internal=True
    (otherwise returns needs_confirmation with the report). progress(phase, done, total)."""
    cdir = cloud_dir(store)
    sess, ident = _require(cdir)
    say = progress or (lambda *a: None)
    if not refs:
        raise ValueError("refs fehlen (project:<p>, set:<p>/<set>, exhibit:<id>)")
    emails = list(dict.fromkeys(cc.norm_email(e) for e in recipients or []))
    if not emails:
        raise ValueError("Empfänger fehlen")
    if len(emails) > MAX_RECIPIENTS:
        raise ValueError(f"höchstens {MAX_RECIPIENTS} Empfänger je Paket")
    say("check", 0, 0)
    rep = check_share(store, refs)
    if rep["internal_count"] and not confirm_internal:
        return {"needs_confirmation": True, **rep}
    to = {}
    for email in emails:
        if email == ident["email"]:
            raise ValueError("an sich selbst senden geht nicht")
        d = _directory(sess, email)
        if not d:
            raise LookupError(f"{email} hat kein Fundus-Cloud-Konto")
        c = _trust(cdir, email, d)
        if c["key_changed_at"]:
            raise PermissionError(f"Der Schlüssel von {email} hat sich seit dem ersten Kontakt geändert "
                                  f"(früher: {c['fingerprint']}; jetzt: {c['new_fingerprint']}). Erst telefonisch prüfen und "
                                  "unter Verwaltung annehmen.")
        to[email] = c
    pid = secrets.token_urlsafe(16)
    tmp = cdir / "tmp" / pid
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        say("export", 0, 0)
        res = _package().export(store, refs, str(tmp / "package.zip"), actor=actor)
        plain = Path(res.get("path") or tmp / "package.zip")
        if plain.stat().st_size > MAX_BYTES:
            raise ValueError(f"Paket zu gross ({plain.stat().st_size // 1_000_000} MB, höchstens {MAX_BYTES // 1_000_000} MB)")
        manifest = res.get("manifest") or {}
        created = datetime.now(timezone.utc).replace(microsecond=0)
        header = {"package_id": pid, "sender_uid": sess.uid, "sender_email": ident["email"], "created": created.isoformat(),
                  "title": manifest.get("title"), "slides": manifest.get("slides") if isinstance(manifest.get("slides"), int)
                  else len(manifest.get("slides") or [])}
        enc = cc.encrypt_file(plain, tmp / "package.bin", header, ident, {c["uid"]: c["box_pk"] for c in to.values()},
                              progress=lambda d, t: say("encrypt", d, t))
        size = (tmp / "package.bin").stat().st_size
        path = f"cloud_packages/{pid}.bin"
        _log(cdir, pid, "out", json.dumps(emails), "uploading", created=_now(), size=size, title=header["title"],
             slides=header["slides"])
        sess.put(f"cloud_packages/{pid}", {
            "from_uid": sess.uid, "from_email": ident["email"], "to_uids": [c["uid"] for c in to.values()],
            "created": created, "expires": created + timedelta(days=TTL_DAYS), "size": size, "path": path,
            "sealed": enc["sealed"], "ready": False, "picked_up": {}}, must_exist=False)
        say("upload", 0, size)
        try:
            sess.upload(path, tmp / "package.bin", progress=lambda d, t: say("upload", d, t))
            sess.put(f"cloud_packages/{pid}", {"ready": True}, mask=["ready"])
        except Exception:
            try:
                sess.delete(f"cloud_packages/{pid}")
            except CloudError:
                pass
            raise
        say("record", 0, 0)
        sent_id = _package().record_sent(store, manifest, emails, transport="cloud")
        _log(cdir, pid, "out", json.dumps(emails), "sent", sent_id=sent_id)
        say("done", size, size)
        return {"package_id": pid, "recipients": [{"email": e, "fingerprint": c["fingerprint"], "confirmed": bool(c["confirmed_at"])}
                                                  for e, c in to.items()],
                "size": size, "sha256": enc["header"]["sha256"], "expires": (created + timedelta(days=TTL_DAYS)).isoformat(),
                "sent_id": sent_id, "internal_count": rep["internal_count"], "title": header["title"], "slides": header["slides"]}
    except Exception as e:
        with _db(cdir) as con:
            if con.execute("SELECT 1 FROM package WHERE id = ?", (pid,)).fetchone():
                con.execute("UPDATE package SET state = 'failed', error = ?, updated = ? WHERE id = ?", (str(e)[:500], _now(), pid))
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- receive
def _expired(d: dict) -> bool:
    exp = d.get("expires")
    return isinstance(exp, datetime) and exp < datetime.now(timezone.utc)


def _receive_one(store, cdir: Path, sess: Session, ident: dict, d: dict) -> dict:
    pid, sender = d["_id"], cc.norm_email(d.get("from_email") or "")
    entry = {"package_id": pid, "from": sender}
    dirent = _directory(sess, sender)
    if not dirent or dirent["uid"] != d.get("from_uid"):
        raise CloudError(f"Absender {sender} steht nicht (mehr) im Adressbuch")
    c = _trust(cdir, sender, dirent)
    if c["key_changed_at"]:
        _log(cdir, pid, "in", sender, "held_key", created=_now(), size=d.get("size"),
             error=f"WARNUNG: Schlüssel von {sender} geändert (früher {c['fingerprint']}, jetzt {c['new_fingerprint']})")
        return {**entry, "state": "held_key", "warning": f"Schlüssel von {sender} hat sich geändert – Paket zurückgehalten",
                "old_fingerprint": c["fingerprint"], "new_fingerprint": c["new_fingerprint"]}
    sealed = (d.get("sealed") or {}).get(sess.uid)
    if not sealed:
        raise CloudError("kein Schlüssel für mich im Paket")
    tmp = cdir / "tmp" / f"in-{pid}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        sess.download(d["path"], tmp / "package.bin")
        hdr = cc.decrypt_file(tmp / "package.bin", tmp / "package.zip", ident, sealed, c["sign_pk"])
        if hdr.get("package_id") != pid or hdr.get("sender_uid") != d.get("from_uid") or sess.uid not in hdr.get("recipients", []) \
                or cc.norm_email(hdr.get("sender_email") or "") != sender:
            raise cc.CryptoError("Paketkopf passt nicht zum Postfach-Eintrag")
        r = _package().receive(store, str(tmp / "package.zip"), sender=sender, sender_fingerprint=c["fingerprint"])
        sess.put(f"cloud_packages/{pid}", {"picked_up": {sess.uid: datetime.now(timezone.utc).replace(microsecond=0)}},
                 mask=[f"picked_up.`{sess.uid}`"])
        confirmed = bool(c["confirmed_at"])
        _log(cdir, pid, "in", sender, "received", created=hdr.get("created"), size=hdr.get("size"), confirmed=int(confirmed),
             title=hdr.get("title"), slides=hdr.get("slides"), received_id=(r or {}).get("received_id"), error=None)
        return {**entry, "state": "received", "confirmed": confirmed, "fingerprint": c["fingerprint"],
                "received_id": (r or {}).get("received_id"), "title": hdr.get("title"),
                **({} if confirmed else {"warning": f"Absender {sender} ist noch nicht bestätigt (Fingerabdruck telefonisch vergleichen)"})}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def poll(store) -> dict:
    """Pick up every new package addressed to me (download, verify, decrypt, package.receive, mark picked up) and delete my
    own packages once every recipient has them or they expired."""
    cdir = cloud_dir(store)
    sess, ident = _require(cdir)
    lk = _lock(cdir)
    if not lk.acquire(timeout=1):
        return {"busy": True}
    out = {"received": [], "held": [], "errors": [], "cleaned": 0}
    try:
        with _db(cdir) as con:
            seen = {r["id"]: dict(r) for r in con.execute("SELECT id, state, attempts FROM package WHERE direction = 'in'")}
        for d in sess.query("cloud_packages", "to_uids", "ARRAY_CONTAINS", sess.uid):
            pid = d["_id"]
            if sess.uid in (d.get("picked_up") or {}) or not d.get("ready") or _expired(d):
                continue
            s = seen.get(pid)
            if s and (s["state"] == "received" or (s["state"] == "failed" and s["attempts"] >= 3)):
                continue
            try:
                r = _receive_one(store, cdir, sess, ident, d)
                (out["held"] if r["state"] == "held_key" else out["received"]).append(r)
            except Exception as e:      # noqa: BLE001 – one bad package must not stop the others
                msg = str(e) if isinstance(e, (CloudError, cc.CryptoError, ValueError, LookupError)) else f"{type(e).__name__}: {e}"
                _log(cdir, pid, "in", d.get("from_email") or "?", "failed", size=d.get("size"), error=msg[:500], attempt=True)
                out["errors"].append({"package_id": pid, "from": d.get("from_email"), "error": msg})
        for d in sess.query("cloud_packages", "from_uid", "EQUAL", sess.uid):
            picked = set((d.get("picked_up") or {}).keys())
            done = set(d.get("to_uids") or []) <= picked
            if done or _expired(d):
                try:
                    sess.remove(d.get("path") or f"cloud_packages/{d['_id']}.bin")
                    sess.delete(f"cloud_packages/{d['_id']}")
                    _log(cdir, d["_id"], "out", "[]", "delivered" if done else "expired")
                    out["cleaned"] += 1
                except CloudError as e:
                    out["errors"].append({"package_id": d["_id"], "error": f"Aufräumen: {e}"})
        _meta(cdir, "last_poll", _now())
        _meta(cdir, "last_error", "; ".join(e["error"] for e in out["errors"])[:500])
        return out
    finally:
        lk.release()


def inbox(cdir, limit: int = 100) -> dict:
    """Local log of sent and received packages (newest first)."""
    with _db(Path(cdir)) as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM package ORDER BY updated DESC LIMIT ?", (int(limit),))]
    for r in rows:
        if r["direction"] == "out":
            try:
                r["peer"] = json.loads(r["peer"])
            except ValueError:
                pass
        r["confirmed"] = None if r["confirmed"] is None else bool(r["confirmed"])
    return {"packages": rows}


def connected(cdir) -> bool:
    cdir = Path(cdir)
    return (cdir / "session.json").exists() and (cdir / "identity.json").exists()
