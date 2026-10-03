"""LLM cache keys and the match-decision cache. No network."""

import json

import wikidata_discover.llm_helpers as lh
from wikidata_discover.llm_helpers import LLMHelper


def test_extract_cache_key_changes_with_the_prompt(monkeypatch):
    before = lh._cache_key("NYU", "openai", "gpt-4o")
    assert before == lh._cache_key("NYU", "openai", "gpt-4o")            # stable
    assert before != lh._cache_key("NYU", "anthropic", "gpt-4o")         # provider matters
    other_prompt = lh._cache_key("NYU", "openai", "gpt-4o", prompt_hash=lh._prompt_hash("new prompt"))
    assert before != other_prompt                                         # prompt matters
    a = lh._cache_key("Trinity College", "openai", "gpt-4o", extra="https://trincoll.edu")
    b = lh._cache_key("Trinity College", "openai", "gpt-4o", extra="https://trinity.duke.edu")
    assert a != b                                                         # same name, other website


def test_choose_match_reuses_a_cached_decision(monkeypatch, tmp_path):
    import wikidata_discover.config as config
    monkeypatch.setattr(lh, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "k")                    # the preferred provider is configured
    children = [("Q1", "Stern School of Business"), ("Q2", "School of Law")]
    calls = []

    def fake_client():
        class R:
            output_text = "ORPHAN:Q2"
        class Client:
            def with_options(self, **kw):
                return self
            class responses:
                @staticmethod
                def create(**kw):
                    calls.append(1); return R()
        return Client()
    def unavailable():
        raise ValueError("not configured in this test")
    monkeypatch.setattr(lh, "_get_openai_client", fake_client)
    monkeypatch.setattr(lh, "_get_anthropic_client", unavailable)
    monkeypatch.setattr(lh, "_get_gemini_client", unavailable)

    first = LLMHelper.choose_match("Law School", "NYU", children)
    second = LLMHelper.choose_match("Law School", "NYU", children)
    assert first == second == ("ORPHAN:Q2", "School of Law")
    assert len(calls) == 1                                                # second answer came from the cache
    cached = [json.loads(p.read_text()) for p in tmp_path.glob("*.json")]
    assert cached == [{"answer": "ORPHAN:Q2", "provider": "openai"}]

    # a different list of choices is a different question
    LLMHelper.choose_match("Law School", "NYU", children + [("Q3", "Law Library")])
    assert len(calls) == 2


def test_match_cache_is_keyed_by_the_configured_providers(monkeypatch, tmp_path):
    """A decision made while a provider was missing is not the same question as one
    made with all providers configured, and a fallback provider's decision is asked
    again once the preferred provider is back."""
    import wikidata_discover.config as config
    monkeypatch.setattr(lh, "_CACHE_DIR", tmp_path)
    children = [("Q1", "Stern School of Business"), ("Q2", "School of Law")]
    monkeypatch.setattr(config, "OPENAI_API_KEY", None)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(config, "GOOGLE_API_KEY", None)
    anthropic_calls, openai_calls = [], []
    class Msg:
        content = [type("T", (), {"text": "Q2"})()]
    class Anthropic:
        def with_options(self, **kw): return self
        class messages:
            @staticmethod
            def create(**kw): anthropic_calls.append(1); return Msg()
    class OpenAI:
        def with_options(self, **kw): return self
        class responses:
            @staticmethod
            def create(**kw): openai_calls.append(1); return type("R", (), {"output_text": "NONE"})()
    def unavailable():
        raise ValueError("not configured")
    monkeypatch.setattr(lh, "_get_openai_client", unavailable)
    monkeypatch.setattr(lh, "_get_anthropic_client", lambda: Anthropic())
    monkeypatch.setattr(lh, "_get_gemini_client", unavailable)
    # OpenAI key missing: Anthropic decides, and the decision is cached under that configuration.
    assert LLMHelper.choose_match("Law School", "NYU", children) == ("Q2", "School of Law")
    assert LLMHelper.choose_match("Law School", "NYU", children) == ("Q2", "School of Law")
    assert len(anthropic_calls) == 1
    # OpenAI key back: a different configuration, so the preferred provider is asked.
    monkeypatch.setattr(config, "OPENAI_API_KEY", "k")
    monkeypatch.setattr(lh, "_get_openai_client", lambda: OpenAI())
    assert LLMHelper.choose_match("Law School", "NYU", children) is None
    assert len(openai_calls) == 1 and len(anthropic_calls) == 1
    # Same configuration, but the cached answer came from a fallback provider (OpenAI
    # was failing at the time): the preferred provider is asked again, once.
    files = list(tmp_path.glob("*.json"))
    for p in files:
        d = json.loads(p.read_text())
        if d.get("provider") == "openai":
            p.write_text(json.dumps({"answer": "Q2", "provider": "anthropic"}))
    assert LLMHelper.choose_match("Law School", "NYU", children) is None
    assert len(openai_calls) == 2
    assert LLMHelper.choose_match("Law School", "NYU", children) is None
    assert len(openai_calls) == 2                                          # replaced answer is reused
    # Same configuration, cached fallback answer, and the preferred provider still
    # failing: the cached decision stands and the fallback is not paid again.
    for p in tmp_path.glob("*.json"):
        if json.loads(p.read_text()).get("provider") == "openai":
            p.write_text(json.dumps({"answer": "Q2", "provider": "anthropic"}))
    class OpenAIDown:
        def with_options(self, **kw): return self
        class responses:
            @staticmethod
            def create(**kw): openai_calls.append(1); raise RuntimeError("503")
    monkeypatch.setattr(lh, "_get_openai_client", lambda: OpenAIDown())
    assert LLMHelper.choose_match("Law School", "NYU", children) == ("Q2", "School of Law")
    assert len(openai_calls) == 3 and len(anthropic_calls) == 1            # asked once, fallback not called
    assert json.loads(next(p for p in tmp_path.glob("*.json")
                           if json.loads(p.read_text()).get("provider") == "anthropic").read_text())["answer"] == "Q2"
