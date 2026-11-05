"""--provider: one AI runs alone, for extraction and for matching. No network."""
import pytest

import wikidata_discover.config as config
from wikidata_discover import cli
from wikidata_discover import llm_helpers as lh
from wikidata_discover.llm_helpers import LLMHelper


def extractors(monkeypatch, answers):
    """Fake extractors; `answers` maps provider -> units. Returns the providers called."""
    called = []
    for name in config.PROVIDERS:
        def fake(label, website, name=name):
            called.append(name)
            return answers.get(name, [])
        monkeypatch.setattr(LLMHelper, f"extract_divisions_{name}", staticmethod(fake))
    return called


def test_extraction_asks_only_the_chosen_provider(monkeypatch):
    called = extractors(monkeypatch, {"openai": [{"name": "A"}], "anthropic": [{"name": "B"}]})
    monkeypatch.setattr(config, "ONLY_PROVIDER", "anthropic")
    assert LLMHelper.extract_divisions_best_available("NYU", "") == [{"name": "B"}]
    assert called == ["anthropic"] and lh.last_extraction_provider == "anthropic"


def test_a_failing_chosen_provider_stops_instead_of_falling_back(monkeypatch):
    called = extractors(monkeypatch, {"openai": [{"name": "A"}], "gemini": [{"name": "C"}]})
    monkeypatch.setattr(config, "ONLY_PROVIDER", "anthropic")
    with pytest.raises(ValueError, match="anthropic .run alone with --provider"):
        LLMHelper.extract_divisions_best_available("NYU", "")
    assert called == ["anthropic"]


def test_without_provider_the_first_that_answers_wins(monkeypatch):
    called = extractors(monkeypatch, {"anthropic": [{"name": "B"}], "gemini": [{"name": "C"}]})
    monkeypatch.setattr(config, "ONLY_PROVIDER", None)
    assert LLMHelper.extract_divisions_best_available("NYU", "") == [{"name": "B"}]
    assert called == ["openai", "anthropic"]


def test_matching_asks_only_the_chosen_provider_under_its_own_cache_key(monkeypatch, tmp_path):
    monkeypatch.setattr(lh, "_CACHE_DIR", tmp_path)
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setattr(config, k, "k")
    asked = []

    class Gemini:
        class models:
            @staticmethod
            def generate_content(**kw):
                asked.append("gemini")
                return type("R", (), {"text": "Q2"})()

    class OpenAI:
        def with_options(self, **kw):
            return self

        class responses:
            @staticmethod
            def create(**kw):
                asked.append("openai")
                return type("R", (), {"output_text": "NONE"})()

    def never():
        raise AssertionError("anthropic must not be asked")
    monkeypatch.setattr(lh, "_get_openai_client", lambda: OpenAI())
    monkeypatch.setattr(lh, "_get_anthropic_client", never)
    monkeypatch.setattr(lh, "_get_gemini_client", lambda: Gemini())
    children = [("Q1", "Stern School of Business"), ("Q2", "School of Law")]

    monkeypatch.setattr(config, "ONLY_PROVIDER", "gemini")
    assert LLMHelper.choose_match("Law School", "NYU", children) == ("Q2", "School of Law")
    assert LLMHelper.choose_match("Law School", "NYU", children) == ("Q2", "School of Law")   # cached
    assert asked == ["gemini"]
    # The default run is a different question: OpenAI is asked, and Gemini's answer is not reused.
    monkeypatch.setattr(config, "ONLY_PROVIDER", None)
    assert LLMHelper.choose_match("Law School", "NYU", children) is None
    assert asked == ["gemini", "openai"]
    assert len([p for p in tmp_path.glob("*.json") if not p.name.endswith(".meta.json")]) == 2


def test_a_chosen_provider_without_a_key_stops_before_any_work(monkeypatch):
    monkeypatch.setattr(config, "ONLY_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GOOGLE_API_KEY", None)
    with pytest.raises(SystemExit, match="--provider gemini: no API key"):
        cli.check_only_provider()
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "k")
    cli.check_only_provider()
