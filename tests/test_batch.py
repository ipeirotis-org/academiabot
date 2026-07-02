"""Unit tests for batch discovery helpers (pure logic, no network/BQ)."""

from wikidata_discover.batch import parse_universities_payload, select_pending


def test_parse_pairs_format():
    data = [["Q49210", "New York University"], ["Q49108", "MIT"]]
    result = parse_universities_payload(data)
    assert result == [("Q49210", "New York University"), ("Q49108", "MIT")]


def test_parse_bindings_format_both_key_conventions():
    data = [
        {
            "univ": {"value": "http://www.wikidata.org/entity/Q49210"},
            "univLabel": {"value": "New York University"},
        },
        {
            "university": {"value": "http://www.wikidata.org/entity/Q49108"},
            "universityLabel": {"value": "MIT"},
        },
    ]
    result = parse_universities_payload(data)
    assert result == [("Q49210", "New York University"), ("Q49108", "MIT")]


def test_parse_treats_qid_placeholder_label_as_missing():
    # Older harvests recorded the QID itself as the label when no English label existed.
    data = [{"univ": {"value": "http://www.wikidata.org/entity/Q2820388"},
             "univLabel": {"value": "Q2820388"}}]
    result = parse_universities_payload(data)
    assert result == [("Q2820388", None)]


def test_parse_skips_rows_without_qid():
    data = [["", "No QID"], {"univLabel": {"value": "Also no QID"}}, ["Q1", "Keep"]]
    result = parse_universities_payload(data)
    assert result == [("Q1", "Keep")]


def test_parse_non_list_returns_empty():
    assert parse_universities_payload({"not": "a list"}) == []


def test_select_pending_skips_processed():
    universities = [("Q1", "A"), ("Q2", "B"), ("Q3", "C")]
    pending = select_pending(universities, processed_qids={"Q2"})
    assert pending == [("Q1", "A"), ("Q3", "C")]


def test_select_pending_dedupes_and_preserves_order():
    universities = [("Q1", "A"), ("Q1", "A dup"), ("Q2", "B")]
    pending = select_pending(universities, processed_qids=set())
    assert pending == [("Q1", "A"), ("Q2", "B")]


def test_select_pending_applies_limit():
    universities = [("Q1", "A"), ("Q2", "B"), ("Q3", "C")]
    pending = select_pending(universities, processed_qids=set(), limit=2)
    assert pending == [("Q1", "A"), ("Q2", "B")]


def test_select_pending_limit_zero_yields_nothing():
    universities = [("Q1", "A")]
    assert select_pending(universities, processed_qids=set(), limit=0) == []
