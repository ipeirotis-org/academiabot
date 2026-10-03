"""The process deadline bounds Wikidata retries, waits and timeouts. No network."""

import time

import wikidata_discover.config as config
import wikidata_discover.sparql_helpers as sh
import wikidata_discover.wikidata_api as wa


def test_no_deadline_means_defaults(monkeypatch):
    monkeypatch.setattr(config, "DEADLINE", None)
    assert sh.request_timeout(120) == 120 and sh.bounded_wait(65) == 65 and not sh.past_deadline()


def test_deadline_caps_timeouts_and_waits(monkeypatch):
    monkeypatch.setattr(config, "DEADLINE", time.time() + 40)
    assert 30 <= sh.request_timeout(120) <= 40
    assert 30 <= sh.bounded_wait(65) <= 40
    assert not sh.past_deadline()
    monkeypatch.setattr(config, "DEADLINE", time.time() - 1)
    assert sh.request_timeout(120) == 5           # floor, never zero
    assert sh.bounded_wait(65) == 0
    assert sh.past_deadline()


def test_sparql_retry_stops_at_the_deadline(monkeypatch):
    calls = []
    def failing_get(query):
        calls.append(1)
        raise sh.requests.ConnectionError("down")
    monkeypatch.setattr(sh, "_get", failing_get)
    monkeypatch.setattr(sh.time, "sleep", lambda s: None)
    sh.execute_sparql_bindings.retry.sleep = lambda s: None
    monkeypatch.setattr(config, "DEADLINE", time.time() - 1)   # already past
    try:
        sh.execute_sparql_bindings("SELECT 1")
    except Exception:
        pass
    assert len(calls) == 1                                       # no second attempt
    monkeypatch.setattr(config, "DEADLINE", None)
    calls.clear()
    try:
        sh.execute_sparql_bindings("SELECT 1")
    except Exception:
        pass
    assert len(calls) == 4                                       # the usual four


def test_search_retry_stops_at_the_deadline(monkeypatch):
    calls = []
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)
    monkeypatch.setattr(wa.requests, "get", lambda *a, **k: calls.append(k["timeout"]) or (_ for _ in ()).throw(wa.requests.ConnectionError("down")))
    wa.quick_wd_search.retry.sleep = lambda s: None
    monkeypatch.setattr(config, "DEADLINE", time.time() + 12)
    try:
        wa.quick_wd_search("x")
    except Exception:
        pass
    assert calls and all(t <= 12 for t in calls)                 # timeout capped by the deadline


def test_llm_calls_respect_the_deadline(monkeypatch):
    import wikidata_discover.llm_helpers as lh
    from wikidata_discover.llm_helpers import LLMHelper
    monkeypatch.setattr(config, "DEADLINE", None)
    assert lh.llm_timeout() == lh.LLM_TIMEOUT_S and lh.enough_time_for_llm_call()
    monkeypatch.setattr(config, "DEADLINE", time.time() + 50)
    assert 40 <= lh.llm_timeout() <= 50 and lh.enough_time_for_llm_call()
    monkeypatch.setattr(config, "DEADLINE", time.time() + 10)      # under the 30 s floor
    assert not lh.enough_time_for_llm_call()

    calls = []
    class Client:
        def with_options(self, **kw):
            calls.append(kw); return self
        class responses:
            @staticmethod
            def create(**kw):
                raise AssertionError("must not be called this close to the deadline")
    monkeypatch.setattr(lh, "_get_openai_client", lambda: Client())
    monkeypatch.setattr(lh, "_load_cache", lambda key: None)
    assert LLMHelper.extract_divisions_openai("Some University", "https://x.edu") == []
    import pytest
    with pytest.raises(lh.LLMDeadline):                              # a refusal is never "NONE"
        LLMHelper.choose_match("Law School", "Some University", [("Q1", "School of Law")])
    assert calls == []                                               # no request was even prepared


def test_no_wikidata_request_starts_after_the_deadline(monkeypatch):
    import pytest
    monkeypatch.setattr(config, "DEADLINE", time.time() - 1)
    monkeypatch.setattr(sh.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")))
    monkeypatch.setattr(wa.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")))
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)
    with pytest.raises(sh.DeadlineExceeded):
        sh._get("SELECT 1")
    with pytest.raises(sh.DeadlineExceeded):
        wa.quick_wd_search("x")                                      # not retried either


def test_run_batch_sets_and_clears_the_deadline(monkeypatch, tmp_path):
    import wikidata_discover.batch as batch
    import wikidata_discover.discovery as disc
    seen = {}
    class Stub:
        def __init__(self, qid):
            self.university_qid, self.university_label = qid, qid
            seen["deadline"], seen["hard"] = config.DEADLINE, config.HARD_DEADLINE
        def discover_missing(self):
            return []
    monkeypatch.setattr(disc, "Discovery", Stub)
    monkeypatch.setattr(batch, "source_identity", lambda run_dir: "x")
    class Blob:
        def __init__(self, store, name): self.store, self.name = store, name
        def exists(self, **kw): return self.name in self.store
        def download_as_text(self, **kw): return self.store[self.name]
        def upload_from_filename(self, p, **kw): self.store[self.name] = open(p).read()
        def delete(self, **kw): self.store.pop(self.name, None)
    class Bucket:
        store = {}
        def blob(self, name): return Blob(self.store, name)
        def list_blobs(self, prefix="", **kw): return []
    t0 = time.time()
    batch.run_batch("d1", ["Q1"], Bucket(), results_dir=tmp_path, report=lambda m: None, hard_deadline_s=1800)
    assert t0 + 1800 - 95 <= seen["deadline"] <= t0 + 1800 - 85   # 90 s before the kill
    assert t0 + 1795 <= seen["hard"] <= t0 + 1805                  # the kill itself
    assert config.DEADLINE is None and config.HARD_DEADLINE is None   # cleared afterwards
