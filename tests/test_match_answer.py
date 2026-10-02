"""choose_match reply parsing and the CLI model override contract."""

from wikidata_discover import config
import wikidata_discover.llm_helpers as lh
from wikidata_discover.llm_helpers import parse_match_answer

CHILDREN = [("Q111", "Stern School of Business"), ("Q222", "School of Law")]


def test_bare_qid():
    assert parse_match_answer("Q222", CHILDREN) == ("Q222", "School of Law")


def test_orphan_prefix_is_preserved():
    assert parse_match_answer("ORPHAN:Q111", CHILDREN) == ("ORPHAN:Q111", "Stern School of Business")


def test_orphan_prefix_case_and_whitespace():
    assert parse_match_answer("  orphan:q111 extra words", CHILDREN) == ("ORPHAN:Q111", "Stern School of Business")


def test_none_and_empty():
    assert parse_match_answer("NONE", CHILDREN) is None
    assert parse_match_answer("", CHILDREN) is None


def test_unknown_qid_is_no_match():
    assert parse_match_answer("Q999", CHILDREN) is None
    assert parse_match_answer("ORPHAN:Q999", CHILDREN) is None


def test_model_names_are_not_captured_at_import(monkeypatch):
    # cli.py sets config.LLM_MODEL after import; llm_helpers must see the new value.
    assert not hasattr(lh, "LLM_MODEL")
    monkeypatch.setattr(config, "LLM_MODEL", "override-model")
    assert lh.config.LLM_MODEL == "override-model"
