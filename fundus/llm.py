"""The optional language model: one wiring for every feature that uses one, and `fundus llm check`.

Settings come from fundus.config (`llm_settings`: env → config file → defaults). Two API styles:

* ``ollama`` (default): Ollama's native API – /api/chat (JSON mode or a JSON schema in ``format``), /api/embed, /api/tags,
  /api/ps. The feature clients (entities.OllamaChat, photos.OllamaVision, embed.OllamaEmbedder) send exactly the request
  bodies they always sent.
* ``openai``: any OpenAI-compatible server (LM Studio, vLLM, llama.cpp server, LocalAI, Ollama's /v1, a hosted service) –
  /v1/chat/completions with ``response_format`` (json_schema where the server supports it, else json_object, else a
  prompt instruction; the answer is parsed and checked against the schema's required keys), /v1/embeddings, /v1/models.

``llm_enabled = false`` switches every model call off: the clients raise ``Unavailable`` at once and the features fall
back exactly as when no model is reachable (full-text search, ungrouped hits, descriptions wait).
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request

from fundus import config


class Unavailable(RuntimeError):
    """No model call possible right now (switched off, server down, model capacity rule, timeout)."""


def settings() -> dict:
    return config.llm_settings()


def require() -> dict:
    """The settings, or Unavailable when the language model is switched off."""
    s = settings()
    if not s["enabled"]:
        raise Unavailable("language model switched off (llm_enabled = false)")
    return s


def is_openai(s: dict | None = None) -> bool:
    return (s or settings())["api"] == "openai"


def openai_base(url: str) -> str:
    """Base URL of an OpenAI-compatible API: '…/v1' (added when the configured URL lacks it)."""
    url = url.rstrip("/")
    return url if re.search(r"/v\d+$", url) else url + "/v1"


def _headers(s: dict) -> dict:
    h = {"Content-Type": "application/json"}
    if s.get("api_key"):
        h["Authorization"] = f"Bearer {s['api_key']}"
    return h


def post(url: str, body: dict, timeout: float, s: dict | None = None) -> dict:
    s = s or settings()
    req = urllib.request.Request(url, json.dumps(body).encode(), _headers(s))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get(url: str, timeout: float, s: dict | None = None) -> dict:
    s = s or settings()
    req = urllib.request.Request(url, headers={k: v for k, v in _headers(s).items() if k != "Content-Type"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ------------------------------------------------------------------ OpenAI-compatible chat
def _json_from_text(text: str):
    """The JSON object in an answer (also inside ```json fences or after some words)."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            pass
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        return json.loads(text[i:j + 1])
    raise ValueError(f"answer is not JSON: {text[:200]}")


def check_schema(ans, schema: dict | None) -> None:
    """Light validation for servers without structured output: an object with the schema's required keys."""
    if not schema:
        return
    if schema.get("type") == "object" and not isinstance(ans, dict):
        raise ValueError("answer is not a JSON object")
    missing = [k for k in schema.get("required") or [] if not isinstance(ans, dict) or k not in ans]
    if missing:
        raise ValueError(f"answer lacks {', '.join(missing)}")


# remembered per base URL: which response_format the server accepts ('json_schema' → 'json_object' → 'none')
_format_support: dict[str, str] = {}
_FORMATS = ("json_schema", "json_object", "none")


def _response_format(kind: str, schema: dict | None):
    if kind == "json_schema" and schema:
        return {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": False}}
    if kind in ("json_schema", "json_object"):
        return {"type": "json_object"}
    return None


def openai_chat_json(model: str, messages: list, schema: dict | None = None, max_tokens: int = 4096,
                     timeout: float = 900, s: dict | None = None) -> dict:
    """One JSON answer from /v1/chat/completions at temperature 0. Tries json_schema, then json_object, then a plain
    request with the schema in the prompt (servers differ); a cut-off answer (finish_reason 'length') raises ValueError."""
    s = s or settings()
    base = openai_base(s["url"])
    start = _format_support.get(base, "json_schema")
    last = None
    for kind in _FORMATS[_FORMATS.index(start):]:
        msgs = [dict(m) for m in messages]
        if kind != "json_schema" or not schema:     # the model only sees the schema when the server cannot enforce it
            hint = "Answer with one JSON object only." + (f" It must follow this JSON schema: {json.dumps(schema)}" if schema else "")
            if msgs and msgs[0].get("role") == "system":
                msgs[0]["content"] = f"{msgs[0]['content']}\n\n{hint}"
            else:
                msgs.insert(0, {"role": "system", "content": hint})
        body = {"model": model, "messages": msgs, "temperature": 0, "max_tokens": int(max_tokens), "stream": False}
        rf = _response_format(kind, schema)
        if rf:
            body["response_format"] = rf
        try:
            data = post(base + "/chat/completions", body, timeout, s)
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:300]
            last = RuntimeError(f"{e.code}: {msg}")
            if e.code in (400, 422) and kind != "none":     # response_format not supported: try the next, simpler one
                continue
            raise last from None
        _format_support[base] = kind
        choice = (data.get("choices") or [{}])[0]
        if choice.get("finish_reason") == "length":
            raise ValueError(f"answer longer than {max_tokens} tokens (model looping) – cut off")
        content = (choice.get("message") or {}).get("content")
        if isinstance(content, list):      # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        ans = _json_from_text(content or "")
        check_schema(ans, schema)
        return ans
    raise last or RuntimeError("no answer")


def openai_image_part(jpeg: bytes) -> dict:
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}}


def openai_embed(model: str, texts: list[str], timeout: float, s: dict | None = None) -> list[list[float]]:
    s = s or settings()
    data = post(openai_base(s["url"]) + "/embeddings", {"model": model, "input": texts}, timeout, s)
    rows = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
    return [r.get("embedding") for r in rows]


# ------------------------------------------------------------------ fundus llm check
def _test_jpeg() -> bytes:
    """A small JPEG: a red square on white (Pillow is a dependency of the photo archive)."""
    import io
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (256, 256), "white")
    ImageDraw.Draw(im).rectangle((48, 48, 208, 208), fill=(220, 20, 20))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _active_space(db_path) -> str | None:
    """The embedding space semantic search uses, read-only (a check never writes to the database)."""
    import sqlite3
    from pathlib import Path
    p = Path(db_path)
    if not p.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        try:
            r = con.execute("SELECT value FROM meta WHERE key = 'embedding.active'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return r[0] if r and r[0] else None


def _have(name: str, available: list[str]) -> bool:
    names = set(available)
    return name in names or f"{name}:latest" in names or (name.endswith(":latest") and name[:-7] in names)


def check(db_path=None, quick: bool = False, timeout: float = 600) -> dict:
    """Report what is configured, whether the endpoint answers, which models it serves, and (unless `quick`) a JSON answer
    from the text model, an image description from the vision model and the embedding dimension. Never writes."""
    s = config.llm_settings(with_origin=True)
    rep = {"enabled": s["enabled"], "api": s["api"], "url": s["url"],
           "models": {"text": s["model_text"], "vision": s["model_vision"], "embed": s["model_embed"]},
           "embed_dim": s["embed_dim"], "num_ctx": s["num_ctx"] if s["api"] == "ollama" else None,
           "api_key": "set" if s.get("api_key") else None, "origin": s["origin"],
           "config_file": str(config.config_file() or "") or None, "checks": {}, "warnings": [], "ok": False}
    if not s["enabled"]:
        rep["warnings"].append("language model switched off (llm_enabled = false): features fall back to full-text search and "
                               "undescribed datasets/photos")
        rep["ok"] = True
        return rep
    c = rep["checks"]
    # 1. endpoint and model list
    try:
        if s["api"] == "ollama":
            available = [m.get("name") or m.get("model") for m in get(s["url"] + "/api/tags", 10, s).get("models", [])]
        else:
            available = [m.get("id") for m in get(openai_base(s["url"]) + "/models", 10, s).get("data", [])]
        c["endpoint"] = {"ok": True, "models_served": len(available)}
    except Exception as e:     # noqa: BLE001 – a check reports everything
        c["endpoint"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        rep["warnings"].append(f"endpoint {s['url']} does not answer – is the server running? (api = {s['api']})")
        return rep
    for role in ("text", "vision", "embed"):
        name = rep["models"][role]
        present = _have(name, available) if available else None
        c[f"{role}_model_listed"] = present
        if present is False:
            hint = f"ollama pull {name}" if s["api"] == "ollama" else "load it in the server or fix the name"
            rep["warnings"].append(f"{role} model '{name}' is not served by the endpoint ({hint})")
    if quick:
        rep["ok"] = all(c.get(f"{r}_model_listed") is not False for r in ("text", "vision", "embed"))
        return rep
    from fundus import embed, entities, photos
    # 2. text: a JSON answer constrained by a schema
    schema = {"type": "object", "properties": {"capital": {"type": "string"}}, "required": ["capital"]}
    try:
        chat = entities.OllamaChat(model=s["model_text"], max_tokens=200, timeout=timeout)
        ans = chat.complete("You answer with JSON only.", 'What is the capital of France? Answer as {"capital": "…"}.', schema)
        c["text"] = {"ok": isinstance(ans, dict) and "capital" in ans, "answer": ans}
    except Exception as e:     # noqa: BLE001
        c["text"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        rep["warnings"].append("text model gives no JSON answer – descriptions of datasets, questions and entities will fail")
    # 3. vision: one picture
    try:
        v = photos.OllamaVision(model=s["model_vision"])
        ans = v.describe('Which colour is the square in the middle of this picture? Answer with JSON: {"colour": "…"}',
                         _test_jpeg())
        colour = str((ans or {}).get("colour", "")).lower() if isinstance(ans, dict) else ""
        c["vision"] = {"ok": isinstance(ans, dict), "answer": ans, "sees_red": "red" in colour or "rot" in colour}
        if not c["vision"]["sees_red"]:
            rep["warnings"].append("vision model answered but did not see the red square – is it a vision model?")
    except Exception as e:     # noqa: BLE001
        c["vision"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        rep["warnings"].append("vision model does not accept the image – photos and videos stay unannotated "
                               "(choose a model with image input, e.g. gemma3/gemma4, qwen2.5-vl, llava)")
    # 4. embedding dimension and the active space of the archive
    try:
        e = embed.OllamaEmbedder(model=s["model_embed"], dim=s["embed_dim"])
        raw = e.raw_embed(["Fundus embedding test"], timeout=timeout)
        native = len(raw[0]) if raw and raw[0] else 0
        c["embed"] = {"ok": native >= s["embed_dim"], "native_dim": native, "stored_dim": min(native, s["embed_dim"])}
        if native and native < s["embed_dim"]:
            rep["warnings"].append(f"embedding model gives {native} dimensions, llm_embed_dim is {s['embed_dim']}: "
                                   f"set llm_embed_dim to {native - native % 8} or less")
    except Exception as e:     # noqa: BLE001
        c["embed"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        rep["warnings"].append("embedding model does not answer – search stays full-text only")
    active = _active_space(db_path or config.default_db())
    want = f"{s['model_embed']}|{min(s['embed_dim'], c.get('embed', {}).get('native_dim') or s['embed_dim'])}"
    rep["active_space"] = active
    if active and not active.startswith(want + "|"):
        dim = want.rsplit("|", 1)[1]
        rep["warnings"].append(
            f"the archive searches with '{active}', configured is '{want}': changing the embedding model needs a re-embedding "
            f"of the archive – run `fundus embed --model {s['model_embed']} --dim {dim}` (fills the new space, resumable), then "
            f"`fundus embed --model {s['model_embed']} --dim {dim} --activate`; photos: `fundus photos embed`")
    rep["ok"] = all((c.get(k) or {}).get("ok") for k in ("endpoint", "text", "vision", "embed"))
    return rep


def format_check(rep: dict) -> str:
    yes = lambda b: "ok" if b else ("–" if b is None else "FAILED")
    lines = [f"Language model: {'on' if rep['enabled'] else 'off'}  (api {rep['api']}, {rep['url']})",
             f"  config file:  {rep.get('config_file') or 'none (environment/defaults only)'}"]
    o = rep.get("origin", {})
    for role in ("text", "vision", "embed"):
        lines.append(f"  model_{role:<7} {rep['models'][role]}  [{o.get('model_' + role, '')}]")
    lines.append(f"  embed_dim     {rep['embed_dim']}" + (f"   num_ctx {rep['num_ctx']}" if rep.get("num_ctx") else ""))
    c = rep.get("checks", {})
    if c:
        lines.append("Checks:")
        for k, v in c.items():
            if isinstance(v, dict):
                extra = {kk: vv for kk, vv in v.items() if kk != "ok"}
                lines.append(f"  {k:<20} {yes(v.get('ok'))}  {json.dumps(extra, ensure_ascii=False)[:160] if extra else ''}")
            else:
                lines.append(f"  {k:<20} {yes(v)}")
    if rep.get("active_space"):
        lines.append(f"Embedding space the archive searches with: {rep['active_space']}")
    for w in rep.get("warnings", []):
        lines.append(f"WARNING: {w}")
    lines.append("Result: " + ("all good" if rep["ok"] else "not complete – see above"))
    return "\n".join(lines)
