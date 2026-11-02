"""Minimal Firestore and Cloud Storage client over REST for the review sync (no firebase-admin/gRPC on the Spark).

Uses the Vertex/Firebase access token of fundus.gemini.token() (service account, cloud-platform scope); admin access is not
bound by the security rules. Values are converted between Python and Firestore's typed JSON (None, bool, int, float, str,
datetime, list, dict).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROJECT = os.environ.get("FUNDUS_GCP_PROJECT", "fundus-review")
BUCKET = os.environ.get("FUNDUS_REVIEW_BUCKET", "fundus-review.firebasestorage.app")


class FirebaseError(RuntimeError):
    pass


def enc(v):
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, datetime):
        return {"timestampValue": v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [enc(x) for x in v]}}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {str(k): enc(x) for k, x in v.items()}}}
    return {"stringValue": str(v)}


def dec(v: dict):
    (k, x), = v.items()
    if k == "nullValue":
        return None
    if k == "integerValue":
        return int(x)
    if k == "timestampValue":
        return datetime.fromisoformat(x.replace("Z", "+00:00"))
    if k == "arrayValue":
        return [dec(i) for i in x.get("values", [])]
    if k == "mapValue":
        return {a: dec(b) for a, b in x.get("fields", {}).items()}
    return x   # string, boolean, double, reference, bytes


def fields(d: dict) -> dict:
    return {k: enc(v) for k, v in d.items()}


def from_doc(doc: dict) -> dict:
    return {"_id": doc["name"].rsplit("/", 1)[-1], "_name": doc["name"], **{k: dec(v) for k, v in doc.get("fields", {}).items()}}


class Client:
    def __init__(self, token_fn=None, project: str = PROJECT, bucket: str = BUCKET):
        if token_fn is None:
            from fundus.gemini import token as token_fn
        self.token, self.project, self.bucket = token_fn, project, bucket
        self.root = f"projects/{project}/databases/(default)/documents"
        self.base = "https://firestore.googleapis.com/v1/"

    def _req(self, method: str, url: str, body=None, data: bytes | None = None, ctype: str = "application/json"):
        payload = data if data is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(url, data=payload, method=method,
                                     headers={"Authorization": f"Bearer {self.token()}", "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404 and method in ("GET", "DELETE"):
                return None
            raise FirebaseError(f"{method} {url.split('?')[0][-80:]}: {e.code} {e.read().decode('utf-8', 'replace')[:300]}") from None
        return json.loads(raw) if raw else {}

    # ---------------------------------------------------------------- Firestore
    def name(self, path: str) -> str:
        return f"{self.root}/{path}"

    def get(self, path: str) -> dict | None:
        d = self._req("GET", self.base + self.name(path))
        return from_doc(d) if d else None

    def commit(self, writes: list[dict]) -> None:
        for i in range(0, len(writes), 400):
            self._req("POST", f"{self.base}projects/{self.project}/databases/(default)/documents:commit", {"writes": writes[i:i + 400]})

    def w_set(self, path: str, data: dict) -> dict:
        return {"update": {"name": self.name(path), "fields": fields(data)}}

    def w_merge(self, path: str, data: dict) -> dict:
        return {"update": {"name": self.name(path), "fields": fields(data)}, "updateMask": {"fieldPaths": list(data)}}

    def w_delete(self, path: str) -> dict:
        return {"delete": self.name(path)}

    def list(self, path: str) -> list[dict]:
        out, token = [], None
        while True:
            q = "?pageSize=300" + (f"&pageToken={urllib.parse.quote(token)}" if token else "")
            d = self._req("GET", self.base + self.name(path) + q) or {}
            out += [from_doc(x) for x in d.get("documents", [])]
            token = d.get("nextPageToken")
            if not token:
                return out

    def query(self, parent: str, collection: str, field: str, op: str, value) -> list[dict]:
        """Documents of parent/collection where field op value (op: EQUAL, GREATER_THAN, …)."""
        url = f"{self.base}{self.name(parent)}:runQuery"
        body = {"structuredQuery": {"from": [{"collectionId": collection}],
                                    "where": {"fieldFilter": {"field": {"fieldPath": field}, "op": op, "value": enc(value)}}}}
        rows = self._req("POST", url, body) or []
        return [from_doc(r["document"]) for r in rows if r.get("document")]

    # ---------------------------------------------------------------- Storage
    def upload(self, name: str, data: bytes, ctype: str) -> str:
        url = (f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o?uploadType=media&name="
               f"{urllib.parse.quote(name, safe='')}")
        self._req("POST", url, data=data, ctype=ctype)
        return self.public_url(name)

    def public_url(self, name: str) -> str:
        return f"https://firebasestorage.googleapis.com/v0/b/{self.bucket}/o/{urllib.parse.quote(name, safe='')}?alt=media"

    def delete_prefix(self, prefix: str) -> int:
        d = self._req("GET", f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o?prefix={urllib.parse.quote(prefix, safe='')}") or {}
        n = 0
        for o in d.get("items", []):
            self._req("DELETE", f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{urllib.parse.quote(o['name'], safe='')}")
            n += 1
        return n
