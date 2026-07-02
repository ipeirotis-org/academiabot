"""Unit tests for IPEDS reconciliation logic (pure, no network/BQ)."""

from wikidata_discover.ipeds import (
    MATCHED,
    MISSING,
    normalize_ipeds_id,
    parse_ipeds_csv,
    reconcile_ipeds,
    summarize,
)


def test_normalize_strips_non_digits_and_leading_zeros():
    assert normalize_ipeds_id("002130") == "2130"
    assert normalize_ipeds_id(" 193900 ") == "193900"
    assert normalize_ipeds_id("unitid=110635") == "110635"
    assert normalize_ipeds_id("") is None
    assert normalize_ipeds_id(None) is None
    assert normalize_ipeds_id("abc") is None


def test_parse_ipeds_csv_extracts_core_columns():
    csv = (
        "UNITID,INSTNM,WEBADDR,OTHER\n"
        "193900,New York University,www.nyu.edu,x\n"
        "166683,MIT,web.mit.edu,y\n"
    )
    rows = parse_ipeds_csv(csv)
    assert rows[0] == {
        "ipeds_id": "193900",
        "name": "New York University",
        "website": "www.nyu.edu",
    }
    assert rows[1]["ipeds_id"] == "166683"


def test_parse_ipeds_csv_skips_rows_without_unitid():
    csv = "UNITID,INSTNM\n,No ID\n123,Has ID\n"
    rows = parse_ipeds_csv(csv)
    assert [r["ipeds_id"] for r in rows] == ["123"]


def test_reconcile_matches_by_normalized_id():
    ipeds_rows = [
        {"ipeds_id": "193900", "name": "NYU", "website": "www.nyu.edu"},
        {"ipeds_id": "999999", "name": "Nowhere U", "website": None},
    ]
    wikidata_map = {"193900": "Q49210"}
    results = reconcile_ipeds(ipeds_rows, wikidata_map)
    assert results[0]["status"] == MATCHED
    assert results[0]["matched_qid"] == "Q49210"
    assert results[1]["status"] == MISSING
    assert results[1]["matched_qid"] is None


def test_summarize_counts():
    results = [
        {"status": MATCHED},
        {"status": MATCHED},
        {"status": MISSING},
    ]
    assert summarize(results) == {
        "total": 3,
        "matched": 2,
        "missing_from_wikidata": 1,
    }
