"""Week 6 checks 1 and 2: the scripts that count them (fakes only)."""
from wikidata_discover.scripts import week6_checks as w6


def node(nid, name, school_qid, qid=None, alignment="uncertain", aliases=()):
    return {"node_id": nid, "name": name, "aliases": list(aliases), "qid": qid, "alignment": alignment,
            "parents_shown": [{"node_id": "p", "name": "Leonard N. Stern School of Business", "qid": school_qid}]}


def test_check1_counts_right_wrong_missing_and_none():
    nodes = [node("n1", "Department of Accounting", "Q770467", "Q101027479", "orphan"),
             node("n2", "Department of Finance", "Q770467", "Q999"),
             node("n3", "Business and Society Program", "Q770467")]
    truth = [{"parent": "Stern", "name": "Accounting", "qid": "Q101027479"},
             {"parent": "Stern", "name": "Finance", "qid": "Q128602068"},
             {"parent": "Stern", "name": "Marketing", "qid": "Q101027500"},
             {"parent": "Stern", "name": "Business and Society Program", "qid": "none"}]
    got = {r["ground_truth_name"]: r["result"] for r in w6.ground_truth_rows(nodes, truth)}
    assert got == {"Accounting": "right", "Finance": "wrong QID", "Marketing": "unit not in the file",
                   "Business and Society Program": "right (none)"}


def test_check1_only_looks_under_the_right_school():
    nodes = [node("n1", "Department of Economics", "Q3339065", "Q101027493")]      # under Arts and Science
    rows = w6.ground_truth_rows(nodes, [{"parent": "Stern", "name": "Economics", "qid": "Q101027493"}])
    assert rows[0]["result"] == "unit not in the file"


def test_check2_compares_with_the_week3_label():
    from tests.test_alignment import FakeWikidata
    from wikidata_discover import alignment
    sample = [{"sample_no": "17", "school_name": "J. Jeffrey and Ann Marie Fox Graduate School", "unit_type_llm": "Graduate School",
               "university_name": "Pennsylvania State University", "university_qid": "Q739627",
               "source_url (the LLM website field; us-tier1 kept no other URL)": "https://gradschool.psu.edu/",
               "already on Wikidata (QID)": "Yes - Q15265896", "really a school (yes/no)": "Yes"},
              {"sample_no": "20", "school_name": "School of Business", "unit_type_llm": "School",
               "university_name": "St. John Fisher University", "university_qid": "Q7588902",
               "source_url (the LLM website field; us-tier1 kept no other URL)": "Not Given",
               "already on Wikidata (QID)": "No", "really a school (yes/no)": "Yes"}]
    wd = FakeWikidata(items={"Q15265896": {"label": "Penn State Graduate School"}},
                      sites={"https://gradschool.psu.edu/": "Q15265896"})
    choose = lambda u, c: {"choice": c[0]["qid"] if c else None, "also_same_unit": [], "reason": "", "llm_call_ids": [],
                           "provider": "f"}
    rows = w6.baseline_rows(sample, "choose", alignment.Searcher(wd), choose_fn=choose)
    assert rows[0]["result"] == "right" and rows[0]["found_by"] == "website"
    assert rows[1]["result"] == "right (new)"
