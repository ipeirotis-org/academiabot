"""Gap report (Anya, week 3). Pure functions only, no network."""

from wikidata_discover import gap_report as g


def snap(qid, label, units, website=None, aliases=()):
    root = {"in_tree": True, "depth": 0, "labels": {"en": label}, "aliases": {"en": list(aliases)},
            "P856": [{"value": website, "rank": "normal"}] if website else []}
    us = {qid: root}
    for i, depth in enumerate(units):
        us[f"{qid}x{i}"] = {"in_tree": True, "depth": depth}
    us["Qout"] = {"in_tree": False, "depth": None}          # a parent outside the tree
    return {"university_qid": qid, "units": us}


CROWD = [
    {"id": 1, "name": "Big University", "parent_id": None, "website_url": "http://www.big.edu/"},
    {"id": 2, "name": "School A", "parent_id": 1, "website_url": None},
    {"id": 3, "name": "Dept A1", "parent_id": 2, "website_url": None},
    {"id": 4, "name": "Small College", "parent_id": None, "website_url": "http://other-domain.org"},
    {"id": 5, "name": "Office", "parent_id": 4, "website_url": None},
]


def test_domain_and_name_normalization():
    assert g.domain("http://www.Princeton.edu/main/") == "princeton.edu"
    assert g.domain("https://law.x.edu") == "law.x.edu" and g.domain("") is None
    assert g.norm_name("Texas A&M University") == "texas a and m university"


def test_crowd_tree_counts_every_level_and_the_direct_children():
    roots = g.crowd_tree(CROWD)
    assert roots[1]["units"] == 2 and roots[1]["direct"] == 1
    assert roots[4]["units"] == 1 and set(roots) == {1, 4}


def test_one_row_per_university_with_counts_and_gap():
    snaps = {"Q1": snap("Q1", "Big University", [1, 1, 2], website="https://big.edu")}
    cands = [{"university_qid": "Q1", "status": s} for s in ["linked", "missing", "missing", "orphan", "missing"]]
    rows = g.build_rows([["Q1", "Big University"], ["Q2", "Not Yet U"]], snaps, cands, g.crowd_tree(CROWD))
    r1, r2 = rows
    assert (r1["wikidata_units"], r1["wikidata_direct"], r1["llm_proposed"]) == (3, 2, 5)
    assert (r1["linked"], r1["orphan"], r1["missing"], r1["unresolved"]) == (1, 1, 3, 0)
    assert r1["gap"] == 3                                  # 5 proposed - 2 linked directly on Wikidata
    assert r1["crowd_id"] == 1 and r1["crowd_match"] == "website" and r1["crowd_units"] == 2
    assert r2["snapshot"] == "not yet" and r2["gap"] is None and r2["llm_proposed"] == 0
    assert list(r1) == g.COLUMNS


def test_crowd_matches_by_exact_name_when_the_website_differs():
    snaps = {"Q4": snap("Q4", "Small College", [], website="https://small.edu")}
    m = g.match_crowd(snaps, g.crowd_tree(CROWD))
    assert m == {"Q4": (4, "name")}


def test_an_ambiguous_key_matches_nothing():
    snaps = {"Q1": snap("Q1", "Big University", [], website="https://big.edu"),
             "Q9": snap("Q9", "Another Big", [], website="https://big.edu")}     # same domain twice
    m = g.match_crowd(snaps, g.crowd_tree(CROWD))
    assert m.get("Q1") == (1, "name")      # the shared website is not used; the unique name is
    assert "Q9" not in m


def test_an_alias_can_match_the_crowd_name():
    crowd = [{"id": 7, "name": "MIT", "parent_id": None, "website_url": None}]
    snaps = {"Q8": snap("Q8", "Massachusetts Institute of Technology", [], aliases=["MIT"])}
    assert g.match_crowd(snaps, g.crowd_tree(crowd)) == {"Q8": (7, "name")}
