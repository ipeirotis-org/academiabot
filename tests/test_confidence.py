"""Week 7 (Shuo): checks and one confidence per node of a hierarchy file."""

import copy
import json
from pathlib import Path

from wikidata_discover.eval import confidence as conf

FIXTURE = Path(__file__).parent / "fixtures" / "hierarchy_nyu_sample.json"


def hierarchy():
    return conf.load_hierarchy(FIXTURE)


def node(node_id):
    return next(n for n in hierarchy()["nodes"] if n["node_id"] == node_id)


def page(text="The Expository Writing Program is part of the College of Arts and Science.", outcome="ok",
         status=200, url="https://cas.nyu.edu/ewp", final_url=""):
    return {"url": url, "outcome": outcome, "status": status, "final_url": final_url or url, "detail": "",
            "content_type": "text/html", "title": "Expository Writing Program", "name_check": "done",
            "text": text, "evidence_id": "ev1"}


def supported(unit, parent, text, outcome, status):
    return {"verdict": "supported", "quote": unit, "reason": "", "decided_by": "openai", "pieces": 1, "piece": 1,
            "llm_call_ids": ["call1"], "quote_ok": "yes"}


def unsupported(unit, parent, text, outcome, status):
    return {**supported(unit, parent, text, outcome, status), "verdict": "unsupported", "quote": ""}


def test_node_version_matches_the_hierarchy_file():
    """The version rule here is the same as Anya's: it gives the version stored in the file."""
    for n in hierarchy()["nodes"]:
        assert conf.node_version(n) == n["node_version"], n["node_id"]


def test_reviewable_nodes_leave_out_the_university():
    ids = [n["node_id"] for n in conf.reviewable_nodes(hierarchy())]
    assert "root" not in ids and len(ids) == 9


def test_parents_of_a_joint_unit():
    ps = conf.parents(node("n00022"))   # NYU Shanghai: NYU and East China Normal University
    assert [p["parent_id"] for p in ps] == ["root", "Q2034790/root"]
    assert [p["rank"] for p in ps] == ["normal", "normal"]


def test_all_checks_pass_gives_high():
    n = node("n00224")
    rows = conf.node_checks(n, page(), ["nyu.edu"], supported)
    kinds = sorted(r["kind"] for r in rows)
    assert kinds == ["name_on_page", "on_domain", "page_status", "soft_404", "verifier"]
    assert all(r["node_version"] == n["node_version"] for r in rows)
    level, reasons = conf.confidence(rows, [p["parent_id"] for p in conf.parents(n)])
    assert level == "high", reasons


def test_dead_page_gives_low():
    n = node("n00224")
    rows = conf.node_checks(n, page(outcome="http_error", status=404, text=""), ["nyu.edu"], supported)
    assert conf.confidence(rows, [p["parent_id"] for p in conf.parents(n)])[0] == "low"


def test_no_source_page_gives_low():
    n = node("n00038")   # a snapshot unit with no source page
    rows = conf.node_checks(n, None, ["nyu.edu"], supported)
    verifier = [r for r in rows if r["kind"] == "verifier"]
    assert verifier and verifier[0]["result"] == "fail"
    assert conf.confidence(rows)[0] == "low"


def test_unsupported_verifier_gives_low():
    n = node("n00224")
    rows = conf.node_checks(n, page(), ["nyu.edu"], unsupported)
    level, reasons = conf.confidence(rows, [p["parent_id"] for p in conf.parents(n)])
    assert level == "low" and any("unsupported" in r for r in reasons)


def test_page_that_refuses_the_fetcher_gives_medium():
    n = node("n00085")
    rows = conf.node_checks(n, page(outcome="http_error", status=405, text=""), ["nyu.edu"],
                            lambda *a: {"verdict": "unclear", "quote": "", "llm_call_ids": []})
    assert conf.confidence(rows, [p["parent_id"] for p in conf.parents(n)])[0] == "medium"


def test_off_domain_page_gives_medium():
    n = node("n00224")
    rows = conf.node_checks(n, page(url="https://example.org/ewp"), ["nyu.edu"], supported)
    assert conf.confidence(rows)[0] == "medium"


def test_only_checks_on_the_current_version_count():
    n = node("n00224")
    rows = conf.node_checks(n, page(), ["nyu.edu"], supported)
    later = copy.deepcopy(n)
    later["source_url"] = "https://cas.nyu.edu/other"
    assert conf.current_checks(rows, n) == rows
    assert conf.current_checks(rows, later) == []
    assert conf.confidence(conf.current_checks(rows, later)) == ("low", ["no checks on this version"])


def test_agreement_only_with_two_providers_or_more():
    n = node("n00224")
    one = conf.node_checks(n, page(), ["nyu.edu"], supported, run_providers=["anthropic"], named_by=["anthropic"])
    assert not [r for r in one if r["kind"] == "agreement"]
    two = conf.node_checks(n, page(), ["nyu.edu"], supported, run_providers=["anthropic", "openai"],
                           named_by=["anthropic"])
    agree = [r for r in two if r["kind"] == "agreement"]
    assert agree[0]["result"] == "fail" and agree[0]["score"] == 0.5
    assert conf.confidence(two)[0] == "medium"


def test_verifier_runs_once_per_parent_link():
    n = node("n00022")
    calls = []

    def verify(unit, parent, text, outcome, status):
        calls.append(parent)
        return supported(unit, parent, text, outcome, status)

    rows = conf.node_checks(n, page(text="NYU Shanghai", url="https://shanghai.nyu.edu/"), ["nyu.edu"], verify)
    assert len(calls) == 2
    assert sorted(r["parent_id"] for r in rows if r["kind"] == "verifier") == ["Q2034790/root", "root"]


def test_summarize_and_checks_csv_round_trip(tmp_path):
    from wikidata_discover.eval.agreement import write_csv
    h = hierarchy()
    nodes = conf.reviewable_nodes(h)
    checks = conf.check_all(h, {}, ["nyu.edu"], supported)
    rows = [{k: (json.dumps(c[k]) if isinstance(c.get(k), list) else c.get(k, "")) for k in conf.CHECK_COLUMNS}
            for c in checks]
    write_csv(tmp_path / "node_checks.csv", rows, conf.CHECK_COLUMNS)
    again = conf.summarize(nodes, conf.read_checks_csv(tmp_path / "node_checks.csv"))
    assert again == conf.summarize(nodes, checks)
    assert len(again) == 9
