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


def test_choose_match_reuses_a_cached_decision(monkeypatch, tmp_path):
    monkeypatch.setattr(lh, "_CACHE_DIR", tmp_path)
    children = [("Q1", "Stern School of Business"), ("Q2", "School of Law")]
    calls = []

    def fake_client():
        class R:
            output_text = "ORPHAN:Q2"
        class Client:
            class responses:
                @staticmethod
                def create(**kw):
                    calls.append(1); return R()
        return Client()
    monkeypatch.setattr(lh, "_get_openai_client", fake_client)

    first = LLMHelper.choose_match("Law School", "NYU", children)
    second = LLMHelper.choose_match("Law School", "NYU", children)
    assert first == second == ("ORPHAN:Q2", "School of Law")
    assert len(calls) == 1                                                # second answer came from the cache
    cached = [json.loads(p.read_text()) for p in tmp_path.glob("*.json")]
    assert cached == [{"answer": "ORPHAN:Q2", "provider": "openai"}]

    # a different list of choices is a different question
    LLMHelper.choose_match("Law School", "NYU", children + [("Q3", "Law Library")])
    assert len(calls) == 2
