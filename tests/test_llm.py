"""Optional services: the language-model wiring (fundus.config llm_*, fundus.llm) and the Vertex settings – never network."""
import io
import json
import urllib.error

import pytest

from fundus import config, embed, entities, gemini, llm, photos

LLM_ENVS = [e for envs in config.LLM_ENV.values() for e in envs] + ["FUNDUS_ENTITY_MODEL", "FUNDUS_DATASET_MODEL"]
VX_ENVS = [e for envs in config.VERTEX_ENV.values() for e in envs] + ["GOOGLE_APPLICATION_CREDENTIALS"]
SA_TYPE = "service_account"   # the key-file type (kept out of JSON-looking literals: the public audit scans for them)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """An empty config file and no FUNDUS_LLM_*/VERTEX env; returns a writer for the config file."""
    for e in LLM_ENVS + VX_ENVS:
        monkeypatch.delenv(e, raising=False)
    p = tmp_path / "config.json"
    p.write_text("{}")
    monkeypatch.setenv("FUNDUS_CONFIG", str(p))
    llm._format_support.clear()

    def write(**kw):
        p.write_text(json.dumps(kw))
    return write


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_server(monkeypatch, answer):
    """urlopen fake: records requests, `answer(url, body)` returns a dict or raises."""
    sent = []

    def urlopen(req, timeout=None):
        body = json.loads(req.data) if getattr(req, "data", None) else None
        sent.append({"url": req.full_url, "body": body, "headers": dict(req.header_items()), "timeout": timeout})
        return Resp(json.dumps(answer(req.full_url, body)).encode())
    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    return sent


def test_defaults_are_the_reference_install(cfg):
    s = config.llm_settings()
    assert s == {"enabled": True, "url": "http://localhost:11434", "api": "ollama", "api_key": None,
                 "model_text": "gemma4:26b", "model_vision": "gemma4:26b", "model_embed": "qwen3-embedding:8b",
                 "embed_dim": 1024, "num_ctx": 262144}
    v = config.vertex_settings()
    assert v["project"] == "fundus-review" and v["location"] == "global" and v["models"] is None
    assert v["key_file"].endswith("fundus-review-sa.json")


def test_precedence_env_config_default(cfg, monkeypatch):
    cfg(llm_url="http://gpu-box:1234/", llm_api="OpenAI", llm_model_text="qwen3:8b", llm_embed_dim="768",
        llm_enabled="false", vertex_project="my-proj", vertex_models="m1=Model one;m2")
    s = config.llm_settings(with_origin=True)
    assert s["url"] == "http://gpu-box:1234" and s["api"] == "openai" and s["model_text"] == "qwen3:8b"
    assert s["embed_dim"] == 768 and s["enabled"] is False and s["origin"]["url"] == "config"
    assert s["origin"]["model_vision"] == "default"
    monkeypatch.setenv("FUNDUS_OLLAMA_URL", "http://old:11434")       # the older names keep working …
    monkeypatch.setenv("FUNDUS_EMBED_MODEL", "nomic-embed-text")
    assert config.llm_settings()["url"] == "http://old:11434"
    assert config.llm_settings()["model_embed"] == "nomic-embed-text"
    monkeypatch.setenv("FUNDUS_LLM_URL", "http://new:11434")          # … the new ones win
    s = config.llm_settings(with_origin=True)
    assert s["url"] == "http://new:11434" and s["origin"]["url"] == "env:FUNDUS_LLM_URL"
    v = config.vertex_settings()
    assert v["project"] == "my-proj" and v["models"] == [("m1", "Model one"), ("m2", "m2")]
    monkeypatch.setenv("FUNDUS_GCP_PROJECT", "env-proj")
    assert config.vertex_settings()["project"] == "env-proj"


def test_embed_default_space_follows_config(cfg):
    assert embed.default_space().key == "qwen3-embedding:8b|1024|int8"
    cfg(llm_model_embed="bge-m3", llm_embed_dim=512)
    assert embed.default_space().key == "bge-m3|512|int8"


def test_disabled_raises_unavailable_without_network(cfg, monkeypatch):
    cfg(llm_enabled=False)
    fake_server(monkeypatch, lambda u, b: pytest.fail("no request when switched off"))
    with pytest.raises(embed.EmbedUnavailable, match="switched off"):
        entities.OllamaChat().complete("s", "u")
    with pytest.raises(llm.Unavailable):
        photos.OllamaVision().describe("p", b"jpeg")
    with pytest.raises(embed.EmbedUnavailable):
        embed.OllamaEmbedder().embed_documents(["x"], retries=0)
    with pytest.raises(embed.EmbedUnavailable):
        entities.OllamaChat().check_capacity()
    rep = llm.check()
    assert rep["ok"] and rep["enabled"] is False and not rep["checks"]


def test_ollama_body_unchanged(cfg, monkeypatch):
    sent = fake_server(monkeypatch, lambda u, b: {"message": {"content": '{"a": 1}'}, "done_reason": "stop"})
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    assert entities.OllamaChat(max_tokens=99, timeout=5).complete("sys", "user", schema) == {"a": 1}
    r = sent[-1]
    assert r["url"] == "http://localhost:11434/api/chat" and r["body"]["format"] == schema
    assert r["body"]["options"] == {"temperature": 0, "num_ctx": 262144, "num_predict": 99} and r["body"]["think"] is False


def test_openai_chat_json_schema_and_fallbacks(cfg, monkeypatch):
    cfg(llm_api="openai", llm_url="http://box:8000", llm_api_key="sk-local", llm_model_text="local-model")
    calls = []

    def answer(url, body):
        calls.append(body.get("response_format", {}).get("type"))
        if body.get("response_format", {}).get("type") == "json_schema":
            raise urllib.error.HTTPError(url, 400, "response_format not supported", {}, io.BytesIO(b"unsupported"))
        return {"choices": [{"finish_reason": "stop", "message": {"content": 'Sure: ```json\n{"a": 2}\n```'}}]}
    sent = fake_server(monkeypatch, answer)
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    chat = entities.OllamaChat(max_tokens=50, timeout=3)
    assert chat.complete("sys", "user", schema) == {"a": 2}
    assert calls == ["json_schema", "json_object"]
    r = sent[-1]
    assert r["url"] == "http://box:8000/v1/chat/completions" and r["body"]["model"] == "local-model"
    assert r["headers"].get("Authorization") == "Bearer sk-local" and r["body"]["max_tokens"] == 50
    assert "JSON schema" in r["body"]["messages"][0]["content"]       # schema in the prompt when not enforced
    chat.complete("sys", "user", schema)                                # the working format is remembered
    assert calls[-1] == "json_object" and calls.count("json_schema") == 1


def test_openai_missing_key_and_length(cfg, monkeypatch):
    cfg(llm_api="openai", llm_url="http://box:8000/v1")
    fake_server(monkeypatch, lambda u, b: {"choices": [{"finish_reason": "stop", "message": {"content": '{"b": 1}'}}]})
    with pytest.raises(ValueError, match="lacks a"):
        entities.OllamaChat().complete("s", "u", {"type": "object", "required": ["a"]})
    fake_server(monkeypatch, lambda u, b: {"choices": [{"finish_reason": "length", "message": {"content": '{"a'}}]})
    with pytest.raises(ValueError, match="longer than"):
        entities.OllamaChat(max_tokens=7).complete("s", "u")


def test_openai_vision_and_embeddings(cfg, monkeypatch):
    cfg(llm_api="openai", llm_url="http://box:8000", llm_model_vision="vl", llm_model_embed="emb", llm_embed_dim=8)

    def answer(url, body):
        if url.endswith("/embeddings"):
            return {"data": [{"index": i, "embedding": [float(i + 1)] * 16} for i in range(len(body["input"]))]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"caption_en": "x"}'}}]}
    sent = fake_server(monkeypatch, answer)
    assert photos.OllamaVision().describe("describe", b"\xff\xd8jpeg") == {"caption_en": "x"}
    part = sent[-1]["body"]["messages"][-1]["content"][1]
    assert part["type"] == "image_url" and part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    vecs = embed.OllamaEmbedder().embed_documents(["a", "b"])
    assert vecs.shape == (2, 8) and sent[-1]["url"] == "http://box:8000/v1/embeddings"


def test_llm_check_reports_models_and_space(cfg, monkeypatch, tmp_path, store):
    cfg(llm_model_embed="new-embed", llm_embed_dim=1024)
    store.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('embedding.active', 'qwen3-embedding:8b|1024|int8')")
    store.db.commit()

    def answer(url, body):
        if url.endswith("/api/tags"):
            return {"models": [{"name": "gemma4:26b"}, {"name": "new-embed:latest"}]}
        if url.endswith("/api/embed"):
            return {"embeddings": [[0.1] * 768]}
        if body.get("messages", [{}])[-1].get("images"):
            return {"message": {"content": '{"colour": "red"}'}}
        return {"message": {"content": '{"capital": "Paris"}'}, "done_reason": "stop"}
    fake_server(monkeypatch, answer)
    rep = llm.check(store.path)
    assert rep["checks"]["endpoint"]["ok"] and rep["checks"]["text"]["ok"] and rep["checks"]["vision"]["sees_red"]
    assert rep["checks"]["embed"]["native_dim"] == 768 and rep["checks"]["embed_model_listed"] is True
    text = " ".join(rep["warnings"])
    assert "llm_embed_dim" in text and "--activate" in text and "re-embedding" in text
    assert rep["ok"] is False and rep["checks"]["embed"]["ok"] is False     # 768 < llm_embed_dim 1024
    assert "Result" in llm.format_check(rep)
    cfg(llm_model_embed="new-embed", llm_embed_dim=768)
    rep = llm.check(store.path)
    assert rep["ok"] is True and any("--dim 768" in w for w in rep["warnings"])


def test_llm_check_endpoint_down(cfg, monkeypatch):
    def urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    rep = llm.check(quick=True)
    assert rep["ok"] is False and rep["checks"]["endpoint"]["ok"] is False and "does not answer" in rep["warnings"][0]


def test_vertex_check_without_credentials(cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(gemini, "KEY_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setattr(gemini, "token", lambda: (_ for _ in ()).throw(RuntimeError("no gcloud login")))
    rep = gemini.check(call=False)
    assert rep["ok"] is False and rep["credentials"] == "gcloud" and rep["checks"]["credentials"]["ok"] is False
    assert any("vertex_project is not set" in w for w in rep["warnings"])
    assert "not ready" in gemini.format_check(rep)


def test_vertex_check_with_fake_model(cfg, monkeypatch, tmp_path):
    key = tmp_path / "sa.json"
    key.write_text(json.dumps({"type": SA_TYPE, "private_key": "SECRET-NEVER-SHOWN"}))
    monkeypatch.setattr(gemini, "KEY_FILE", str(key))
    monkeypatch.setattr(gemini, "generate", lambda model, parts, schema, max_tokens=0, search=False:
                        ({"ok": True}, {"totalTokenCount": 12}))
    rep = gemini.check()
    assert rep["ok"] and rep["credentials"] == "service_account" and rep["checks"]["model"]["tokens"] == 12
    assert "SECRET" not in json.dumps(rep) and "SECRET" not in gemini.format_check(rep)


def test_vertex_adc_service_account_file(cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(gemini, "KEY_FILE", str(tmp_path / "missing.json"))
    adc = tmp_path / "adc.json"
    adc.write_text(json.dumps({"type": "authorized_user"}))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(adc))
    assert gemini.credentials() == ("gcloud", None)          # a user ADC file goes through gcloud
    adc.write_text(json.dumps({"type": SA_TYPE}))
    assert gemini.credentials() == ("service_account", str(adc))
