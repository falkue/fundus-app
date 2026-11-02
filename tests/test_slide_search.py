"""Foliensuche: hits filtered by creation year and confidence, grouped into aspects (faked LLM); saved searches;
references combining saved searches and slide sets; a review from references."""
import pytest

from fundus import slide_search as ss
from test_exhibits import projects as projects_root  # noqa: F401  (fixture)
from test_gemini import build


class Chat:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def complete(self, system, user):
        self.calls.append(user)
        if self.fail:
            raise RuntimeError("ollama busy")
        ids = [int(l.split(" | ")[0]) for l in user.splitlines() if " | " in l]
        return {"aspects": [{"name": "Händler", "summary": "Was Händler tun", "slides": ids[:1]}], "unrelated": ids[1:]}


def test_search_filters_aspects_and_saving(store, projects_root):
    doc, obs, s = build(store, projects_root)
    a, b = s[0]["exhibit_id"], s[1]["exhibit_id"]
    r = ss.search(store, "Dealers", chat=Chat(), include_internal=True)
    assert {x["id"] for x in r["slides"]} == {a, b} and all(x["year"] for x in r["slides"])
    assert r["aspects"][0]["name"] == "Händler" and len(r["unrelated"]) == 1
    y = r["slides"][0]["year"]
    assert ss.search(store, "Dealers", years=str(y - 1), aspects=False, include_internal=True)["count"] == 0
    assert ss.search(store, "Dealers", years=f"{y - 1},{y}", aspects=False, include_internal=True)["count"] == 2
    assert ss.search(store, "Dealers", min_confidence=2, aspects=False, include_internal=True)["count"] == 0   # nothing checked yet
    # the LLM down: hits stay, one group, a note
    r2 = ss.search(store, "Dealers", chat=Chat(fail=True), include_internal=True)
    assert r2["count"] == 2 and len(r2["aspects"]) == 1 and "ollama busy" in r2["aspects_note"]
    with pytest.raises(ValueError):
        ss.search(store, "  ")
    # save, list, get, update, references
    c = ss.save(store, "KI im Autohandel", [a, b], question="Dealers", filters=r.get("filters"), aspects=r["aspects"])
    assert c["ref"] == "search:ki-im-autohandel" and c["count"] == 2
    assert [g["name"] for g in c["aspects"]] == ["Händler", "Ohne Aspekt"]
    with pytest.raises(ValueError):
        ss.save(store, "KI im Autohandel", [a])
    assert ss.list_collections(store)[0]["slides"] == 2
    u = ss.update(store, "KI im Autohandel", name="KI Autohandel", remove=[b])
    assert u["slug"] == "ki-autohandel" and u["count"] == 1
    got = ss.resolve_refs(store, ["search:ki-autohandel", "set:acme-dealerships/main"])
    assert [x["id"] for x in got["slides"]][0] == a and {x["id"] for x in got["slides"]} == {a, b}
    assert next(x for x in got["slides"] if x["id"] == a)["from"] == ["search:ki-autohandel", "set:acme-dealerships/main"]
    assert ss.resolve_refs(store, ["ki-autohandel"], min_confidence=2)["dropped_by_filter"] == 1
    with pytest.raises(LookupError) as e:
        ss.resolve_refs(store, ["search:ki-autohandle"])
    assert "ki-autohandel" in str(e.value)
    new = ss.rerun(store, "ki-autohandel", chat=Chat())
    assert [x["id"] for x in new["new"]] == [b]
    assert ss.delete(store, "ki-autohandel")["name"] == "KI Autohandel"


def test_review_from_references(store, projects_root, monkeypatch):
    from fundus import mcp_server as ms
    doc, obs, s = build(store, projects_root)
    ss.save(store, "Auswahl", [s[0]["exhibit_id"]])
    monkeypatch.setattr(ms, "_start_gemini", lambda rid, retry=False: None)
    out = ms.review_tool(store, "gemini", refs=["search:auswahl"])
    assert out["project"] == "acme-dealerships" and out["slides"] == 1


def test_llm_answer_is_capped_and_grouping_has_a_short_wait(monkeypatch):
    """28 Oct 2026: gemma4 looped for 15 min (47'500 tokens) on a slide search – answers are capped (num_predict), a cut
    answer raises, and the slide search asks with a cap that fits its slides and a short timeout."""
    import io
    import json as _json
    from fundus import entities, slide_search as ss
    sent = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        sent["body"], sent["timeout"] = _json.loads(req.data), timeout
        return Resp(_json.dumps({"message": {"content": '{"aspects": [' + '{"name": "x", ' * 50}, "done_reason": "length"}).encode())
    monkeypatch.setattr(entities.urllib.request, "urlopen", fake_urlopen)
    chat = entities.OllamaChat(max_tokens=123, timeout=7)
    with pytest.raises(ValueError, match="longer than 123"):
        chat.complete("sys", "user")
    assert sent["body"]["options"]["num_predict"] == 123 and sent["timeout"] == 7
    slides = [{"id": i, "project": "p", "title": f"t{i}"} for i in range(10)]
    aspects, unrelated, note = ss.group_aspects("Frage", slides)
    assert sent["body"]["options"]["num_predict"] == 600 + 40 * 10 and sent["timeout"] == ss.ASPECT_TIMEOUT
    assert note and "longer than" in note and aspects[0]["slides"] == list(range(10))     # hits stay usable, ungrouped
