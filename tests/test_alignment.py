"""Week 6: every search before "new", two ways to decide, merges, and duplicates (fakes only)."""
import json
import re

import pytest

from wikidata_discover import alignment as al, hierarchy_file as hf, node_register as nr
from tests.test_hierarchy_file import World, by_name, llm, make_snapshot, snap_unit


# ── a fake Wikidata search service ───────────────────────────────────────

class FakeWikidata:
    """Answers the alignment queries from a small table. Records every query."""

    def __init__(self, items=None, prefix=None, fulltext=None, sites=None, broken=()):
        self.items = items or {}            # qid -> details
        self.prefix = prefix or {}          # text -> [qid]
        self.fulltext = fulltext or {}      # text -> [qid]
        self.sites = sites or {}            # url -> qid
        self.broken = set(broken)           # kinds that fail: "fulltext", ...
        self.queries = []

    def __call__(self, q):
        self.queries.append(q)
        uri = lambda x: {"value": "http://www.wikidata.org/entity/" + x}
        if '"EntitySearch"' in q:
            if "prefix" in self.broken:
                raise RuntimeError("HTTP 500")
            text = json.loads(re.search(r"mwapi:search (\".*?\");", q).group(1))
            return [{"item": uri(x)} for x in self.prefix.get(text, [])]
        if '"Search"' in q:
            if "fulltext" in self.broken:
                raise RuntimeError("HTTP 500")
            text = json.loads(re.search(r"mwapi:srsearch (\".*?\");", q).group(1))
            return [{"item": uri(x)} for x in self.fulltext.get(text, [])]
        if "?site" in q and "wdt:P856 ?site" in q and "VALUES ?site" in q:
            urls = re.findall(r"<([^>]+)>", q.split("VALUES ?site")[1].split("}")[0])
            return [{"item": uri(self.sites[u])} for u in urls if u in self.sites]
        if "VALUES ?item" in q:
            rows = []
            for x in re.findall(r"wd:(Q\d+)", q):
                d = self.items.get(x)
                if d:
                    rows.append({"item": uri(x), "label": {"value": d["label"]},
                                 **({"p31": uri(d["p31"])} if d.get("p31") else {}),
                                 **({"p31Label": {"value": d["p31_label"]}} if d.get("p31_label") else {})})
            return rows
        raise AssertionError("unexpected query: " + q[:80])


def unit(name="Wheelock College", **kw):
    return {"name": name, "unit_type": "school", "parent_name": "Boston University",
            "university": "Boston University", "website": None, "source_url": None, "aliases": [], **kw}


def chooser(choice=None, also=()):
    calls = []

    def fn(u, cands):
        calls.append([c["qid"] for c in cands])
        return {"choice": choice, "also_same_unit": list(also), "reason": "fake", "llm_call_ids": ["c1"],
                "provider": "fake"}
    fn.calls = calls
    return fn


# ── SPECS.md week 6 tests ────────────────────────────────────────────────

def test_a_unit_found_only_by_full_text_search_is_linked_not_new():
    wd = FakeWikidata(items={"Q4948183": {"label": "Boston University Wheelock College of Education"}},
                      fulltext={"Wheelock College": ["Q4948183"]})
    choose = chooser("Q4948183")
    r = al.align_unit(unit(), al.Searcher(wd), "choose", choose_fn=choose)
    assert r["status"] == "aligned" and r["qid"] == "Q4948183"
    assert r["candidates"][0]["found_by"] == ["fulltext"]
    only_prefix = al.align_unit(unit(), al.Searcher(wd), "choose", kinds=["prefix"], choose_fn=chooser("Q4948183"))
    assert only_prefix["status"] == "new"             # the old method would have called it new


def test_two_of_our_nodes_for_one_unit_under_two_names_are_merged(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    # Wikidata has "Department of Accounting" directly under the university (Q50); the LLM
    # names "Accounting Department" under the School of Law. Search finds Q50 in the tree.
    w.snap = make_snapshot([snap_unit("Q50", "Test University Department of Accounting", ["academic department"],
                                      [("Q1", "P361")])])
    w.answers["Q10"] = [llm("Accounting Department", "department")]
    wd = FakeWikidata(items={"Q50": {"label": "Test University Department of Accounting"}})
    deps = w.deps()
    deps.aligner = hf.SearchAligner(al.Searcher(wd), "choose", choose_fn=lambda u, c: {
        "choice": "Q50" if u["name"] == "Accounting Department" else None, "also_same_unit": [],
        "reason": "fake", "llm_call_ids": [], "provider": "fake"})
    w.deps = lambda: deps
    doc, _, _ = w.run()
    names = by_name(doc)
    assert "Test University Department of Accounting" not in names
    acc = names["Accounting Department"]
    assert acc["qid"] == "Q50" and acc["parents_shown"][0]["qid"] == "Q10"
    assert "Test University Department of Accounting" in acc["aliases"]
    assert doc["merged"] == [{"name": "Test University Department of Accounting", "qid": "Q50",
                              "origin": "snapshot", "merged_into": "Accounting Department"}]


def test_two_wikidata_items_for_one_unit_are_flagged_and_not_merged():
    wd = FakeWikidata(items={"Q7": {"label": "Kent State University at Geauga"},
                             "Q8": {"label": "Kent State Geauga"}},
                      fulltext={"Kent State Geauga": ["Q7", "Q8"]})
    r = al.align_unit(unit("Kent State Geauga"), al.Searcher(wd), "choose", choose_fn=chooser("Q7", also=["Q8"]))
    assert r["qid"] == "Q7" and r["duplicates"] == ["Q7", "Q8"]
    # In yes-no mode, two yes answers choose neither, and the pair is flagged.
    yn = al.align_unit(unit("Kent State Geauga"), al.Searcher(wd), "yes-no", yes_no_fn=lambda u, c: {
        "same_unit": True, "reason": "fake", "llm_call_ids": [], "provider": "fake"})
    assert yn["qid"] is None and yn["status"] == "uncertain" and yn["duplicates"] == ["Q7", "Q8"]
    # Nothing is ever written to Wikidata: only read queries were sent.
    assert all(q.lstrip().upper().startswith("SELECT") for q in wd.queries)


def test_every_node_records_its_candidates_and_the_reason(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    wd = FakeWikidata(items={"Q60": {"label": "Test University School of Music"}},
                      fulltext={"School of Music": ["Q60"]})
    deps = w.deps()
    deps.aligner = hf.SearchAligner(al.Searcher(wd), "choose", choose_fn=chooser(None))
    w.deps = lambda: deps
    doc, _, _ = w.run()
    for n in doc["nodes"]:
        assert n["alignment_reason"], n["name"]
        assert isinstance(n["alignment_candidates"], list)
    music = by_name(doc)["School of Music"]
    assert music["alignment"] == "new"
    assert music["alignment_candidates"] == [{"qid": "Q60", "label": "Test University School of Music",
                                              "found_by": ["fulltext"], "chosen": False}]
    assert music["alignment_reason"].startswith("no item after every search")


def test_yes_no_mode_runs_behind_its_flag_and_the_default_is_unchanged():
    wd = FakeWikidata(items={"Q1": {"label": "A"}, "Q2": {"label": "B"}}, fulltext={"Wheelock College": ["Q1", "Q2"]})
    asked = []

    def yes_no(u, c):
        asked.append(c["qid"])
        return {"same_unit": c["qid"] == "Q2", "reason": "fake", "llm_call_ids": [], "provider": "fake"}
    choose = chooser("Q1")
    default = al.align_unit(unit(), al.Searcher(wd), choose_fn=choose, yes_no_fn=yes_no)
    assert default["qid"] == "Q1" and asked == [] and len(choose.calls) == 1
    yn = al.align_unit(unit(), al.Searcher(wd), "yes-no", choose_fn=choose, yes_no_fn=yes_no)
    assert yn["qid"] == "Q2" and asked == ["Q1", "Q2"] and len(choose.calls) == 1


# ── more rules ───────────────────────────────────────────────────────────

def test_a_failed_search_makes_a_unit_uncertain_never_new():
    wd = FakeWikidata(broken={"fulltext"})
    r = al.align_unit(unit(), al.Searcher(wd), choose_fn=chooser(None))
    assert r["status"] == "uncertain" and "fulltext" in r["search_errors"]


def test_new_only_after_every_search_kind_ran():
    wd = FakeWikidata()
    tree_asked = []
    r = al.align_unit(unit(website="https://wheelock.bu.edu"), al.Searcher(wd), choose_fn=chooser(None),
                      tree_fn=lambda u: tree_asked.append(1) or [])
    assert r["status"] == "new" and set(r["found"]) == set(al.SEARCH_KINDS) and tree_asked == [1]
    assert any("VALUES ?site" in q for q in wd.queries)                       # website search ran
    assert any('"EntitySearch"' in q and "Wheelock College" in q for q in wd.queries)
    assert any("Boston University Wheelock College" in q for q in wd.queries)  # university name in front


def test_the_university_and_the_parent_are_never_candidates():
    wd = FakeWikidata(items={"Q1": {"label": "Boston University"}, "Q9": {"label": "Wheelock"}},
                      fulltext={"Wheelock College": ["Q1", "Q9"]})
    choose = chooser(None)
    al.align_unit(unit(), al.Searcher(wd), choose_fn=choose, exclude={"Q1"})
    assert choose.calls == [["Q9"]]


def test_categories_and_people_are_not_shown_to_the_llm():
    wd = FakeWikidata(items={"Q3": {"label": "Category:Wheelock College"}, "Q4": {"label": "John Wheelock",
                                                                                 "p31": "Q5"}},
                      fulltext={"Wheelock College": ["Q3", "Q4"]})
    choose = chooser(None)
    r = al.align_unit(unit(), al.Searcher(wd), choose_fn=choose)
    assert choose.calls == [] and r["status"] == "new"


def test_a_choice_outside_the_list_is_never_used(monkeypatch):
    from wikidata_discover import llm_helpers
    monkeypatch.setattr(llm_helpers, "_align_json", lambda *a, **k: (
        {"choice": "Q999", "also_same_unit": ["Q888"], "reason": "x"}, [], "fake"))
    r = llm_helpers._decide_choose({"name": "X"}, [{"qid": "Q1"}])
    assert r["choice"] is None and r["also_same_unit"] == []


def test_other_names_turn_the_name_around():
    assert al.other_names("Department of Finance", []) == ["Finance Department"]
    assert "School of Law" in al.other_names("Law School", [])
    assert al.other_names("Stern", ["Stern"]) == []


def test_website_variants_cover_scheme_www_and_slash():
    vs = al.website_variants("https://www.stern.nyu.edu/")
    assert "https://www.stern.nyu.edu/" in vs and "http://stern.nyu.edu" in vs and len(vs) == 8


def test_every_search_response_is_saved_with_the_run(tmp_path):
    from wikidata_discover import run_log
    from tests.test_node_register import FakeBucket
    bucket = FakeBucket()
    log = run_log.RunLog("r1", "i1", bucket=bucket, results_dir=tmp_path)
    wd = FakeWikidata(fulltext={"Wheelock College": []})
    s = al.Searcher(wd, log=log)
    s.run(unit())
    saved = [k for k in bucket.objects if k.startswith("runs/r1/searches/")]
    assert len(saved) == s.queries > 0


def test_register_records_a_merge_and_retires_the_old_id():
    root = {"key": "root", "qid": "Q1", "name": "U", "parent_keys": [], "is_root": True}
    snapnode = {"key": "s", "qid": "Q50", "name": "U Department of Accounting", "parent_keys": ["root"]}
    llmnode = {"key": "l", "qid": None, "name": "Department of Accounting", "parent_keys": ["root"]}
    ids1, _, reg = nr.assign_ids(nr.empty_register("Q1"), [root, snapnode, llmnode], "r1")
    # Week 6: the LLM node gets Q50, and the Wikidata node is merged away (not passed).
    ids2, flags, reg2 = nr.assign_ids(reg, [root, dict(llmnode, qid="Q50")], "r2")
    survivor = ids2["l"]
    old = ({ids1["s"], ids1["l"]} - {survivor}).pop()
    assert reg2["merged"] == {old: survivor}
    ids3, flags3, reg3 = nr.assign_ids(reg2, [root, dict(llmnode, qid="Q50")], "r3")
    assert ids3["l"] == survivor and not flags3 and reg3["merged"] == {old: survivor}


def test_two_llm_units_on_one_qid_are_not_merged():
    nodes = [{"key": "a", "qid": "Q5", "origin": "llm and search", "name": "Center for Data Science", "level": 2,
              "parent_keys": ["x"], "flags": []},
             {"key": "b", "qid": "Q5", "origin": "llm and search", "name": "Center for Data Science", "level": 2,
              "parent_keys": ["y"], "flags": []}]
    kept, merged = hf.merge_same_qid(nodes)
    assert merged == [] and len(kept) == 2
    assert all(n["qid"] is None and n["search_status"] == "uncertain" and n["flags"] for n in kept)


def test_an_item_found_outside_the_snapshot_brings_its_subtree(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    sub = {"university_qid": "Q70", "units": {
        "Q70": snap_unit("Q70", "Conservatory of Music", ["music school"], depth=0),
        "Q71": snap_unit("Q71", "Department of Piano (Wikidata)", ["academic department"], [("Q70", "P749")])},
        "links": [{"child": "Q71", "parent": "Q70", "prop": "P749", "rank": "normal", "statement_id": "Q71-Q70",
                   "statement_on": "Q71", "qualifiers": {}, "parent_in_tree": True}]}
    wd = FakeWikidata(items={"Q70": {"label": "Conservatory of Music"}}, sites={"https://music.tu.edu": "Q70"})
    deps = w.deps()
    deps.aligner = hf.SearchAligner(al.Searcher(wd), "choose", take_subtree=lambda q: sub,
                                    choose_fn=lambda u, c: {"choice": "Q70" if u["name"] == "School of Music" else None,
                                                            "also_same_unit": [], "reason": "fake",
                                                            "llm_call_ids": [], "provider": "fake"})
    w.deps = lambda: deps
    doc, _, _ = w.run()
    music = by_name(doc)["School of Music"]
    assert music["qid"] == "Q70" and music["alignment"] == "orphan"     # no P749 to the university
    assert by_name(doc)["Department of Piano (Wikidata)"]["parent_ids"] == [music["node_id"]]
    assert doc["alignment"]["subtrees_added"] == [{"qid": "Q70", "units_added": 2}]
