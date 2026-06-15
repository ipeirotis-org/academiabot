"""Unit tests for BigQuery row shaping."""

from wikidata_discover.harvester import university_rows_for_bq


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
