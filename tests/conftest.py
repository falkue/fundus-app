import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def store(tmp_path):
    from fundus import Store
    st = Store(tmp_path / "t.sqlite")
    yield st
    st.close()


class _NoOllama:
    """Default embedder in tests: never touches the network (tests that need vectors install a fake)."""
    model, dim = "no-ollama", 8

    def embed_documents(self, texts):
        from fundus.embed import EmbedUnavailable
        raise EmbedUnavailable("no ollama in tests")

    def embed_query(self, text, timeout=4.0):
        from fundus.embed import EmbedUnavailable
        raise EmbedUnavailable("no ollama in tests")


@pytest.fixture(autouse=True)
def _no_ollama():
    from fundus import embed
    embed.set_embedder(_NoOllama())
    yield
    embed.set_embedder(None)


@pytest.fixture(autouse=True)
def _no_vertex(monkeypatch):
    """Tests never call Vertex AI: background Gemini checks do not start, generate() fails loudly unless a test fakes it."""
    from fundus import gemini
    monkeypatch.setattr(gemini, "start", lambda *a, **k: None)

    def _blocked(*a, **k):
        raise RuntimeError("Vertex AI is not called in tests – fake fundus.gemini.generate")
    monkeypatch.setattr(gemini, "generate", _blocked)
    monkeypatch.setattr(gemini, "token", lambda: "test-token")


@pytest.fixture(autouse=True)
def _no_crossref(monkeypatch):
    """Tests never call Crossref (a test that needs a journal fakes fundus.crossref.lookup)."""
    monkeypatch.setenv("FUNDUS_NO_NET", "1")
