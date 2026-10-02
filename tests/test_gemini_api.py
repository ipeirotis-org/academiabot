"""The Gemini calls use the installed google-genai API (keyword-only from_text,
config=GenerateContentConfig). A fake client records what it was given. No network."""

import wikidata_discover.llm_helpers as lh
from wikidata_discover.llm_helpers import LLMHelper


class FakeModels:
    def __init__(self, text):
        self.text, self.calls = text, []

    def generate_content(self, *, model, contents, config=None, **unexpected):
        assert not unexpected, f"old-API keyword passed: {unexpected}"
        self.calls.append((model, contents, config))
        class R:
            pass
        r = R(); r.text = self.text
        return r


def test_choose_match_gemini_call_shape(monkeypatch):
    models = FakeModels("Q2")
    class Client:
        pass
    c = Client(); c.models = models
    monkeypatch.setattr(lh, "_get_gemini_client", lambda: c)
    monkeypatch.setattr(lh, "_load_cache", lambda key: None)
    monkeypatch.setattr(lh, "_save_cache", lambda key, v: None)
    for name in ("_get_openai_client", "_get_anthropic_client"):
        monkeypatch.setattr(lh, name, lambda: (_ for _ in ()).throw(ValueError("not configured")))
    assert LLMHelper.choose_match("School of Law", "NYU", [("Q1", "Stern"), ("Q2", "School of Law")]) == ("Q2", "School of Law")
    model, contents, config = models.calls[0]
    assert contents[0].parts[0].text.startswith("You are assisting")


def test_extract_gemini_call_shape(monkeypatch):
    models = FakeModels('{"units": [{"name": "School of Law"}], "reference": "https://x.edu"}')
    class Client:
        pass
    c = Client(); c.models = models
    monkeypatch.setattr(lh, "_get_gemini_client", lambda: c)
    monkeypatch.setattr(lh, "_load_cache", lambda key: None)
    monkeypatch.setattr(lh, "_save_cache", lambda key, v: None)
    assert LLMHelper.extract_divisions_gemini("X University", "https://x.edu") == [{"name": "School of Law"}]
    model, contents, config = models.calls[0]
    assert config.max_output_tokens == 2048 and "X University" in contents[0].parts[0].text
    assert config.http_options.timeout == lh.LLM_TIMEOUT_S * 1000      # no deadline: the full timeout


def test_gemini_timeout_shrinks_near_the_deadline(monkeypatch):
    import time
    import wikidata_discover.config as cfg
    monkeypatch.setattr(cfg, "DEADLINE", time.time() + 45)
    assert 35_000 <= lh._gemini_http_options().timeout <= 45_000
