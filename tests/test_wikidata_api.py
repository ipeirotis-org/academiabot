"""Wikidata search wrapper: 429 handling and result shape, without network."""

import wikidata_discover.wikidata_api as wa


class FakeResp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status; self._payload = payload or {}; self.headers = headers or {}
    def raise_for_status(self):
        if self.status_code >= 400:
            raise wa.requests.HTTPError(f"{self.status_code}")
    def json(self):
        return self._payload


def test_search_returns_id_label_pairs(monkeypatch):
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)
    monkeypatch.setattr(wa.requests, "get", lambda *a, **k: FakeResp(200, {"search": [{"id": "Q1", "label": "A"}]}))
    assert wa.quick_wd_search("A") == [("Q1", "A")]


def test_search_language_is_passed_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)
    monkeypatch.setattr(wa.requests, "get", lambda *a, **k: seen.update(k["params"]) or FakeResp(200, {"search": []}))
    wa.quick_wd_search("A", language="es")
    assert seen["language"] == "es"
    wa.quick_wd_search("A")
    assert seen["language"] == "en"


def test_429_waits_then_retries(monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr(wa.time, "sleep", lambda s: sleeps.append(s))
    responses = [FakeResp(429, headers={"Retry-After": "7"}), FakeResp(200, {"search": []})]
    monkeypatch.setattr(wa.requests, "get", lambda *a, **k: calls.append(1) or responses.pop(0))
    wa.quick_wd_search.retry.sleep = lambda s: None  # skip tenacity's own backoff
    assert wa.quick_wd_search("X") == []
    assert len(calls) == 2
    assert 7 in sleeps
