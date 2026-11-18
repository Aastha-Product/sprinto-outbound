"""Cost controls from the L4 lecture: answer cache (repeat = free) and the per-run ceiling. Fake Gemini only."""

import pytest

from outbound import llm
from outbound.models import AgentAction, Citation
from outbound.storage import MemoryStore


class FakeResponse:
    def __init__(self, text):
        self.text = text
        self.candidates = []


@pytest.fixture
def fake_gemini(monkeypatch):
    calls = []

    def fake_generate(contents, config, retries=True):
        calls.append(contents)
        return FakeResponse('{"action": "decide", "argument": "", "why": "enough"}')

    monkeypatch.setattr(llm, "_generate", fake_generate)
    monkeypatch.setattr(llm, "_store", None)
    monkeypatch.setattr(llm, "_run", llm._RunMeter())
    llm.start_domain("acme.io")
    yield calls
    llm.use_store(None)


def test_same_prompt_twice_is_paid_once(fake_gemini):
    llm.use_store(MemoryStore())
    vars_ = {"question": "Is Acme on SOC 2?"}
    first = llm.generate_json("research", vars_, AgentAction)
    second = llm.generate_json("research", vars_, AgentAction)
    assert first == second
    assert len(fake_gemini) == 1  # second answer came from the cache
    assert llm.run_usage()["cached_calls"] == 1 and llm.run_usage()["calls"] == 1
    assert llm.calls_used() == 1  # cached answers don't use the per-domain budget either


def test_different_prompt_is_not_served_from_cache(fake_gemini):
    llm.use_store(MemoryStore())
    llm.generate_json("research", {"question": "a"}, AgentAction)
    llm.generate_json("research", {"question": "b"}, AgentAction)
    assert len(fake_gemini) == 2


def test_run_ceiling_stops_the_run(fake_gemini):
    llm.start_run(max_calls=2, max_searches=0)
    llm.generate_json("research", {"question": "1"}, AgentAction)
    llm.generate_json("research", {"question": "2"}, AgentAction)
    with pytest.raises(llm.RunBudgetExceeded) as err:
        llm.generate_json("research", {"question": "3"}, AgentAction)
    assert isinstance(err.value, llm.LLMFatalError)  # the CLI stops cleanly on it
    assert len(fake_gemini) == 2


def test_cached_search_is_free_and_skips_ceiling(fake_gemini, monkeypatch):
    store = MemoryStore()
    llm.use_store(store)
    monkeypatch.setattr(llm, "search_available", lambda: True)
    prompt = llm.render_prompt("research", {"question": "acme funding"})
    key = llm._cache_key("search", llm.get_settings().gemini_model, prompt)
    store.set_cache("search", key, {"text": "Acme raised a Series B in 2025.", "citations": [{"url": "https://news.example/a", "title": "n"}]})
    llm.start_run(max_calls=0, max_searches=0)  # no paid calls allowed at all
    text, cites = llm.research_with_search("acme funding")
    assert "Series B" in text and cites == [Citation(url="https://news.example/a", title="n")]
    assert fake_gemini == []
