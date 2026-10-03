"""The university-list filter: which harvested items get a cloud run (tier 1), which
a person reviews, and which are dropped. No network."""

import json

from wikidata_discover.scripts import filter_universities as fu

LISTED = {"Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Q8"}


def item(**kw):
    d = {"label": "X", "classes": set(), "parents": set(), "ipeds": None, "ror": None, "website": None, "dissolved": None}
    d.update(kw)
    return d


def test_ipeds_means_tier_1_even_with_a_listed_parent():
    assert fu.classify(item(ipeds="190415"), LISTED) == ("tier1", "ipeds")
    assert fu.classify(item(ipeds="190415", parents={"Q2"}), LISTED) == ("tier1", "ipeds")   # Baruch under CUNY


def test_dissolved_is_dropped_whatever_else_it_has():
    assert fu.classify(item(ipeds="1", ror="r", dissolved="1999-01-01"), LISTED)[0] == "drop"


def test_a_unit_of_a_listed_institution_is_dropped():
    assert fu.classify(item(ror="r", classes={"Q3918"}, website="https://x", parents={"Q2"}), LISTED) == \
        ("drop", "unit of a listed institution")
    assert fu.classify(item(classes={"Q3918"}, website="https://x", parents={"Q999"}), LISTED)[0] == "review"  # parent not listed


def test_review_tier_needs_ror_or_a_university_class_and_a_website():
    assert fu.classify(item(ror="r"), LISTED) == ("review", "ror but no ipeds")
    assert fu.classify(item(classes={"Q902104"}, website="https://x"), LISTED) == ("review", "university class, no ipeds or ror")
    assert fu.classify(item(classes={"Q902104"}), LISTED) == ("drop", "no website")
    assert fu.classify(item(classes={"Q209465"}, website="https://x"), LISTED) == ("drop", "not a university class")  # a campus


def test_split_orders_tier_1_by_label_and_keeps_every_item_in_the_review():
    info = {"Q1": item(label="Zed University", ipeds="1"), "Q2": item(label="Alpha College", ipeds="2"),
            "Q3": item(label="Alpha College School of Law", classes={"Q3918"}, website="https://x", parents={"Q2"}),
            "Q4": item(label="Old College", ipeds="4", dissolved="1950")}
    tier1, review = fu.split(info)
    assert tier1 == [["Q2", "Alpha College"], ["Q1", "Zed University"]]
    assert [r["qid"] for r in review] == ["Q2", "Q1", "Q3", "Q4"]           # tier 1 first, then review, then drop
    assert {r["qid"]: r["tier"] for r in review} == {"Q1": "tier1", "Q2": "tier1", "Q3": "drop", "Q4": "drop"}


def test_list_qids_accepts_every_list_shape_and_dedupes():
    rows = [["Q1", "Q1"], "Q2", {"univ": {"value": "http://www.wikidata.org/entity/Q3"}}, ["Q1", "again"], []]
    assert fu.list_qids(rows) == ["Q1", "Q2", "Q3"]


def test_main_writes_both_files(monkeypatch, tmp_path):
    src = tmp_path / "list.json"
    src.write_text(json.dumps([["Q1", "Q1"], ["Q2", "Q2"]]))
    monkeypatch.setattr(fu, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(fu, "fetch_attributes", lambda qids: {
        "Q1": item(label="Real University", ipeds="1"), "Q2": item(label="Some Campus", classes={"Q209465"})})
    assert fu.main([str(src)]) == 0
    assert json.loads((tmp_path / "universities_us_tier1.json").read_text()) == [["Q1", "Real University"]]
    text = (tmp_path / "universities_us_review.csv").read_text()
    assert "Q2,Some Campus,drop,not a university class" in text and "Q1,Real University,tier1,ipeds" in text
