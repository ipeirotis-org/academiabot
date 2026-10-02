"""Unit tests for BigQuery row shaping."""

from wikidata_discover import bq_helpers
from wikidata_discover.harvester import (
    build_university_sparql,
    university_rows_for_bq,
)


def test_insert_rows_chunks_large_payloads(monkeypatch):
    calls = []

    class FakeClient:
        def insert_rows_json(self, table_id, rows):
            calls.append(len(rows))
            return []

    monkeypatch.setattr(bq_helpers, "ensure_dataset_and_tables", lambda: None)
    monkeypatch.setattr(bq_helpers, "_client", lambda: FakeClient())
    monkeypatch.setattr(bq_helpers, "_INSERT_CHUNK_SIZE", 500)

    rows = [{"run_id": str(i)} for i in range(1200)]
    bq_helpers._insert_rows("quickstatements_batches", rows)

    assert calls == [500, 500, 200]  # chunked, not one 1200-row request


def test_university_rows_for_bq_maps_wikidata_bindings():
    rows = [
        {
            "univ": {"value": "http://www.wikidata.org/entity/Q49210"},
            "univLabel": {"value": "New York University"},
            "website": {"value": "https://www.nyu.edu/"},
        },
        {
            "univ": {"value": "http://www.wikidata.org/entity/Q49108"},
            "univLabel": {"value": "Massachusetts Institute of Technology"},
        },
    ]

    result = university_rows_for_bq(rows)

    assert result[0]["qid"] == "Q49210"
    assert result[0]["label"] == "New York University"
    assert result[0]["website"] == "https://www.nyu.edu/"
    assert result[0]["country"] == "Q30"
    assert result[0]["ipeds_id"] is None
    assert result[0]["harvested_at"]
    assert result[1]["qid"] == "Q49108"
    assert result[1]["website"] is None


def test_university_rows_for_bq_populates_us_ipeds_from_identifier():
    rows = [
        {
            "univ": {"value": "http://www.wikidata.org/entity/Q49210"},
            "univLabel": {"value": "New York University"},
            "identifier": {"value": "193900"},
        }
    ]
    result = university_rows_for_bq(rows, country_qid="Q30", identifier_prop="P1771")
    assert result[0]["ipeds_id"] == "193900"
    assert result[0]["identifier"] == "193900"
    assert result[0]["identifier_property"] == "P1771"
    assert result[0]["country"] == "Q30"


def test_university_rows_for_bq_non_us_country_leaves_ipeds_null():
    rows = [
        {
            "univ": {"value": "http://www.wikidata.org/entity/Q160302"},
            "univLabel": {"value": "University of Oxford"},
        }
    ]
    result = university_rows_for_bq(rows, country_qid="Q145")
    assert result[0]["country"] == "Q145"
    assert result[0]["ipeds_id"] is None
    assert result[0]["identifier"] is None


def test_build_university_sparql_uses_country_and_optional_identifier():
    us = build_university_sparql("Q30", identifier_prop="P1771")
    assert "wd:Q30" in us
    assert "wdt:P1771" in us
    assert "?identifier" in us

    uk = build_university_sparql("Q145")
    assert "wd:Q145" in uk
    assert "?identifier" not in uk
