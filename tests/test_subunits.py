"""Week 4 sub-unit step: prompt answer parsing, the Anthropic pause_turn loop, the cache,
and the CSV files. No network."""
import csv
import json

import pytest

import wikidata_discover.config as config
from wikidata_discover import llm_helpers as lh
from wikidata_discover import run_log, subunits
from wikidata_discover.llm_helpers import LLMHelper

STERN = {"qid": "Q770467", "label": "NYU Stern School of Business", "parent_label": "New York University",
         "parent_qid": "Q49210", "website": "https://www.stern.nyu.edu", "country": "United States"}
ANSWER = {"units": [
    {"name": "Finance", "unit_type": "department", "website": None,
     "source_url": "https://www.stern.nyu.edu/finance", "country": "United States",
     "country_source": "44 West 4th Street, New York, NY"},
    {"name": "Career Center", "unit_type": "office", "website": "https://stern.nyu.edu/cdc",
     "source_url": None, "country": "United States", "country_source": None},
]}


def test_the_json_answer_is_found_inside_text_and_fences():
    text = 'I searched the site.\n```json\n{"note": 1}\n' + json.dumps(ANSWER) + "\n```\nDone."
    assert lh.json_with_units(text) == ANSWER
    with pytest.raises(ValueError):
        lh.json_with_units("no json here {not json}")


def test_normalize_keeps_offices_out_of_scope_and_flags_units_without_a_url():
    units = lh.normalize_subunits({"units": ANSWER["units"] + [
        {"name": "  Finance ", "unit_type": "department"},                    # repeated name
        {"name": "Center for Sustainable Business", "unit_type": "Research Center",
         "source_url": "javascript:alert(1)", "website": "ftp://x"},
        {"name": "", "unit_type": "department"}]})
    by = {u["name"]: u for u in units}
    assert list(by) == ["Finance", "Career Center", "Center for Sustainable Business"]
    assert (by["Finance"]["in_scope"], by["Finance"]["no_url"]) == ("yes", False)
    assert (by["Career Center"]["in_scope"], by["Career Center"]["no_url"]) == ("no", True)
    other = by["Center for Sustainable Business"]
    assert (other["unit_type"], other["in_scope"], other["source_url"], other["website"]) == ("other", "check", None, None)
    assert by["Finance"]["country_source"] == "44 West 4th Street, New York, NY"


def test_the_parent_is_read_from_p749_before_p361():
    rows = [{"label": "Courant", "parent": "http://www.wikidata.org/entity/Q2", "parentLabel": "B", "prop": "P361"},
            {"label": "Courant", "parent": "http://www.wikidata.org/entity/Q1", "parentLabel": "A", "prop": "P749",
             "countryLabel": "United States"}]
    u = subunits.unit_from_bindings("Q946028", rows)
    assert (u["label"], u["parent_qid"], u["parent_label"], u["country"]) == ("Courant", "Q1", "A", "United States")
    assert subunits.unit_from_bindings("Q5", [])["label"] == "Q5"
    raw = [{"label": {"type": "literal", "value": "Courant"},
            "parent": {"type": "uri", "value": "http://www.wikidata.org/entity/Q49210"}}]
    assert subunits.flatten(raw) == [{"label": "Courant", "parent": "http://www.wikidata.org/entity/Q49210"}]


class Block:
    def __init__(self, type_, text=None):
        self.type, self.text = type_, text


class Resp:
    def __init__(self, blocks, stop):
        self.content, self.stop_reason = blocks, stop

    def model_dump_json(self, indent=2):
        return json.dumps({"stop_reason": self.stop_reason})


def fake_anthropic(monkeypatch, replies):
    sent = []

    class Client:
        def with_options(self, **kw):
            return self

        class messages:
            @staticmethod
            def stream(**kw):
                sent.append(kw)
                reply = replies[len(sent) - 1]

                class Stream:
                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                    def __iter__(self):
                        return iter([type("E", (), {"type": "content_block_start",
                                                    "content_block": Block("server_tool_use")})()])

                    def get_final_message(self):
                        return reply
                return Stream()
    monkeypatch.setattr(lh, "_get_anthropic_client", lambda: Client())
    return sent


def test_anthropic_searches_resumes_after_pause_turn_and_caches_the_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(lh, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "ONLY_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    sent = fake_anthropic(monkeypatch, [
        Resp([Block("text", "Let me search."), Block("server_tool_use")], "pause_turn"),
        Resp([Block("web_search_tool_result"), Block("text", json.dumps(ANSWER))], "end_turn")])
    units = LLMHelper.extract_subunits(STERN)
    assert [u["name"] for u in units] == ["Finance", "Career Center"] and lh.last_subunit_provider == "anthropic"
    assert len(sent) == 2
    assert sent[0]["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    assert sent[1]["messages"][-1]["role"] == "assistant"                # the paused turn is sent back
    assert "NYU Abu Dhabi" in sent[0]["system"] and "Q770467" in sent[0]["messages"][0]["content"]
    # A second request for the same unit reads the cache: no new call.
    assert LLMHelper.extract_subunits(STERN) == units and len(sent) == 2


def test_the_cache_key_changes_with_the_search_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(lh, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "ONLY_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    answer = Resp([Block("text", json.dumps(ANSWER))], "end_turn")
    sent = fake_anthropic(monkeypatch, [answer, answer])
    LLMHelper.extract_subunits(STERN)
    monkeypatch.setattr(lh, "SUBUNIT_MAX_SEARCHES", 3)               # same request, other search limit
    LLMHelper.extract_subunits(STERN)
    assert len(sent) == 2


def test_a_chosen_provider_that_answers_nothing_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(lh, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "ONLY_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    bad = Resp([Block("text", "I could not find anything.")], "end_turn")
    fake_anthropic(monkeypatch, [bad, bad])
    with pytest.raises(ValueError, match="anthropic .run alone with --provider. returned no sub-units"):
        LLMHelper.extract_subunits(STERN)


def test_subunits_for_writes_both_csv_files_and_unaligned_candidates(monkeypatch, tmp_path):
    monkeypatch.setattr(LLMHelper, "extract_subunits", staticmethod(lambda unit: lh.normalize_subunits(ANSWER)))
    monkeypatch.setattr(lh, "last_subunit_provider", "anthropic")
    monkeypatch.setattr(lh, "last_subunit_call_ids", ["c1"])
    log = run_log.RunLog("subunits-test", results_dir=tmp_path, enabled=False)
    s = subunits.subunits_for("Q770467", log, out_dir=tmp_path, fetch=lambda q: STERN)
    assert (s["units"], s["no_url"], s["by_type"]) == (2, 1, {"department": 1, "office": 1})
    rows = list(csv.DictReader((tmp_path / "Q770467_anthropic.csv").open()))
    assert [(r["name"], r["in_scope"], r["no_url"]) for r in rows] == [("Finance", "yes", ""), ("Career Center", "no", "yes")]
    depts = list(csv.DictReader((tmp_path / "Q770467_anthropic_departments.csv").open()))
    assert [r["name"] for r in depts] == ["Finance"] and "parent" not in depts[0]   # scorer input
    cands = log.pending["candidates"]
    assert {c["status"] for c in cands} == {"unaligned"} and cands[0]["llm_call_ids"] == ["c1"]
    assert cands[0]["university_qid"] == "Q49210" and cands[0]["parent_qid"] == "Q770467"


def test_without_provider_the_subunit_step_asks_anthropic_first(monkeypatch):
    asked = []

    def fake(provider, unit, mode):
        asked.append(provider)
        return ([{"name": "X"}], ["c"]) if provider == "openai" else ([], [])
    monkeypatch.setattr(lh, "_subunits_from", fake)
    monkeypatch.setattr(config, "ONLY_PROVIDER", None)
    assert LLMHelper.extract_subunits(STERN) == [{"name": "X"}]
    assert asked == ["anthropic", "openai"] and lh.last_subunit_provider == "openai"
