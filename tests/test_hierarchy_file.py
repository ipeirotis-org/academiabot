"""discover --depth 2 (week 5): the hierarchy file, on a made-up university (fakes only)."""
import json

import pytest

from wikidata_discover import config, hierarchy_file as hf, node_register as nr, run_log
from tests.test_node_register import FakeBucket


# ── a made-up university ─────────────────────────────────────────────────

def snap_unit(qid, label, p31, parents=(), website=None, depth=1, in_tree=True, aliases=()):
    return {"qid": qid, "label": label, "in_tree": in_tree, "depth": depth if in_tree else None,
            "P31": [{"qid": "Qx", "label": t, "rank": "normal"} for t in p31],
            "P856": [{"value": website, "rank": "normal"}] if website else [], "P6782": [],
            "aliases": {"en": list(aliases)} if aliases else {}, "labels": {"en": label},
            "parents": [{"parent": p, "prop": prop, "rank": "normal", "statement_id": f"{qid}-{p}-{prop}",
                         "statement_on": qid, "qualifiers": {}} for p, prop in parents]}


def make_snapshot(extra=()):
    units = [
        snap_unit("Q1", "Test University", ["university"], depth=0, aliases=["TU"], website="https://tu.edu"),
        snap_unit("Q10", "Test University School of Law", ["law school"], [("Q1", "P749")], "https://law.tu.edu"),
        snap_unit("Q11", "School of Art", ["faculty"], [("Q1", "P361")], "https://art.tu.edu"),
        snap_unit("Q12", "Test University Department of History", ["academic department"], [("Q11", "P361")],
                  depth=2),
        snap_unit("Q13", "TU Shanghai", ["private university"], [("Q1", "P749"), ("Q99", "P749")],
                  "https://shanghai.tu.edu"),
        snap_unit("Q14", "Test University Library", ["academic library"], [("Q1", "P361")], "https://lib.tu.edu"),
        snap_unit("Q15", "TU Florence", ["higher education institution"], [("Q1", "P361")]),
        snap_unit("Q16", "Lab of Old Maps", ["research center"], [("Q12", "P361")], depth=3),
        snap_unit("Q99", "East China Normal University", ["public university"], in_tree=False),
        *extra,
    ]
    by = {u["qid"]: u for u in units}
    links = [{"child": u["qid"], "parent": p["parent"], "prop": p["prop"], "rank": "normal",
              "statement_id": p["statement_id"], "statement_on": u["qid"], "qualifiers": {},
              "parent_in_tree": by.get(p["parent"], {}).get("in_tree", False)}
             for u in units for p in u["parents"]]
    return {"university_qid": "Q1", "university_label": "Test University", "taken_at": "2026-11-18T00:00:00Z",
            "counts": {}, "units": by, "links": links}


def llm(name, unit_type="school", website=None, source_url="https://tu.edu/units", country="United States"):
    return {"name": name, "unit_type": unit_type, "in_scope": "no" if unit_type == "office" else "yes",
            "website": website, "source_url": source_url, "country": country,
            "country_source": "https://tu.edu/about"}


def answers():
    return {
        "Q1": [llm("School of Law", website="https://law.tu.edu/"), llm("School of Art"), llm("School of Music",
               website="https://music.tu.edu"), llm("TU Shanghai", "campus", country="China"),
               llm("TU Florence", "campus", country="Italy"), llm("Career Office", "office")],
        "Q10": [llm("Department of Torts", "department")],
        "Q11": [llm("Department of History", "department"), llm("Department of Sculpture", "department",
                                                                 source_url=None)],
        "School of Music": [llm("Department of Piano", "department")],
        "Q13": [llm("Division of Science", "department", country="China")],
    }


class FakePages:
    def __init__(self):
        self.cited = []

    def cite(self, units):
        out = {}
        for u in units:
            for url in [u.get("source_url"), u.get("country_source")]:
                if url and url.startswith("http"):
                    self.cited.append(url)
                    out[url] = {"evidence_id": "ev-" + url, "outcome": "ok", "text": u["name"], "name_check": "done"}
        return out


class World:
    """Fakes for one run: snapshot, Wikidata units, LLM answers, pages, register bucket."""

    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
        self.tmp, self.snap, self.answers, self.asked = tmp_path, make_snapshot(), answers(), []
        self.bucket = FakeBucket()
        self.study_away = {"Q1": {"qids": [], "sites": ["TU Florence"]}}

    def extract(self, unit):
        who = unit.get("qid") or unit["label"]
        self.asked.append(who)
        return [dict(u) for u in self.answers.get(who, [])], "anthropic", [f"call-{who}"]

    def fetch_unit(self, qid):
        u = self.snap["units"][qid]
        return {"qid": qid, "label": u["label"], "website": (u["P856"] or [{}])[0].get("value"),
                "country": "United States", "parent_label": None if qid == "Q1" else "Test University"}

    def deps(self):
        return hf.Deps(take_snapshot=lambda q: json.loads(json.dumps(self.snap)), fetch_unit=self.fetch_unit,
                       extract=self.extract, pages=FakePages(),
                       countries=lambda qids: {q: {"qid": "Q30", "label": "United States"} for q in qids},
                       is_university=lambda q: q == "Q99", study_away=self.study_away)

    def run(self, expand=None, run_id="r1"):
        log = run_log.RunLog(run_id, "inv1", bucket=self.bucket, bq_client=None, results_dir=self.tmp)
        store = nr.RegisterStore(self.bucket, self.tmp / "hierarchy")
        s = hf.run_university("Q1", 2, log, store, self.deps(), expand=expand, out_dir=self.tmp / "hierarchy")
        doc = json.loads((self.tmp / "hierarchy" / "Q1.json").read_text())
        return doc, log, s


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def by_name(doc):
    return {n["name"]: n for n in doc["nodes"]}


# ── SPECS.md week 5 tests ────────────────────────────────────────────────

def test_a_school_the_llm_left_out_comes_from_the_snapshot_and_gets_its_departments(world):
    world.answers["Q1"] = [u for u in world.answers["Q1"] if u["name"] != "School of Art"]
    doc, _, _ = world.run()
    art = by_name(doc)["School of Art"]
    assert art["origin"] == "snapshot" and art["qid"] == "Q11"
    assert art["children_lookup"] == "looked up" and "Q11" in world.asked
    kids = [n for n in doc["nodes"] if art["node_id"] in n["parent_ids"]]
    assert {n["name"] for n in kids} == {"Department of History", "Department of Sculpture"}
    assert by_name(doc)["Department of History"]["qid"] == "Q12"


def test_a_renamed_unit_and_a_unit_that_gets_a_qid_keep_their_node_ids(world):
    world.answers["Q1"].append(llm("School of Dance"))
    doc1, _, _ = world.run(run_id="r1")
    music1, dance1 = by_name(doc1)["School of Music"], by_name(doc1)["School of Dance"]
    assert dance1["qid"] is None
    # Run 2: Music is renamed (same website); Dance is now on Wikidata under the university.
    world.answers["Q1"] = [dict(u, name="Conservatory of Music") if u["name"] == "School of Music" else u
                           for u in world.answers["Q1"]]
    world.answers["Conservatory of Music"] = world.answers.pop("School of Music")
    world.snap = make_snapshot([snap_unit("Q21", "Test University School of Dance", ["faculty"], [("Q1", "P749")])])
    doc2, _, _ = world.run(run_id="r2")
    assert by_name(doc2)["Conservatory of Music"]["node_id"] == music1["node_id"]
    dance2 = by_name(doc2)["School of Dance"]
    assert dance2["qid"] == "Q21" and dance2["node_id"] == dance1["node_id"]
    assert dance2["node_version"] != dance1["node_version"]


def test_a_second_run_on_the_same_input_gives_every_unit_the_same_node_id(world):
    doc1, _, _ = world.run(run_id="r1")
    doc2, _, _ = world.run(run_id="r2")
    ids = lambda d: {(n["name"], tuple(n["parent_ids"])): n["node_id"] for n in d["nodes"]}
    assert ids(doc1) == ids(doc2)
    assert len({n["node_id"] for n in doc1["nodes"]}) == len(doc1["nodes"])
    assert doc2["register"]["loaded_generation"] == doc1["register"]["saved_generation"]


def test_every_node_has_a_source_url_or_a_flag(world):
    doc, _, _ = world.run()
    for n in doc["nodes"]:
        assert n["source_url"] or n["flags"], n["name"]
    assert "no source URL" in by_name(doc)["Department of Sculpture"]["flags"]


# ── Anya's week 5 rules ──────────────────────────────────────────────────

def test_a_study_away_site_is_kept_and_its_children_are_not_looked_up(world):
    doc, _, _ = world.run()
    florence = by_name(doc)["TU Florence"]
    assert florence["children_lookup"] == "children not looked up (study-away site)"
    assert florence["unit_type"] == "campus" and "Q15" not in world.asked


def test_a_unit_not_in_the_snapshot_is_uncertain_with_the_week_6_reason(world):
    doc, _, _ = world.run()
    music = by_name(doc)["School of Music"]
    assert music["alignment"] == "uncertain" and music["qid"] is None
    assert music["alignment_reason"] == "not in the snapshot; full search in week 6"
    piano = by_name(doc)["Department of Piano"]       # its parent has no QID: looked up by name
    assert piano["alignment"] == "uncertain" and "School of Music" in world.asked


def test_a_unit_with_two_parents_keeps_both(world):
    doc, _, _ = world.run()
    sh = by_name(doc)["TU Shanghai"]
    assert sh["parent_ids"] == ["root", "Q99/root"]
    assert [l["p749"] for l in sh["parent_links"]] == ["present", "present"]
    assert sh["country"]["label"] == "China"


def test_alignment_follows_the_p749_links_in_the_snapshot(world):
    doc, _, _ = world.run()
    n = by_name(doc)
    assert n["School of Law"]["alignment"] == "linked"            # matched by website, P749 present
    assert n["School of Art"]["alignment"] == "orphan"            # only P361 to the university
    assert n["Department of History"]["alignment"] == "orphan"
    assert n["Test University"]["alignment"] == "root" and n["Test University"]["node_id"] == "root"


def test_units_out_of_scope_or_too_deep_are_listed_as_left_out(world):
    doc, _, _ = world.run()
    left = {l["name"]: l["reason"] for l in doc["left_out"]}
    assert left["Career Office"].startswith("out of scope")
    assert left["Test University Library"].startswith("out of scope")
    assert "Lab of Old Maps" in left
    assert "Career Office" not in by_name(doc)


def test_expand_limits_level_2_to_the_named_units(world):
    doc, _, _ = world.run(expand=["Q10", "School of Music"])
    assert sorted(world.asked) == ["Q1", "Q10", "School of Music"]
    assert by_name(doc)["School of Art"]["children_lookup"] == "not looked up (not named in --expand)"
    assert by_name(doc)["Test University Department of History"]["origin"] == "snapshot"  # Wikidata units still come in


def test_nodes_go_to_the_run_log_and_candidates_carry_the_alignment(world):
    doc, log, s = world.run()
    rows = log.pending["nodes"]
    assert len(rows) == len(doc["nodes"]) == s["nodes"]
    assert {r["node_id"] for r in rows} == {n["node_id"] for n in doc["nodes"]}
    statuses = {r["name"]: r["status"] for r in log.pending["candidates"]}
    assert statuses["School of Law"] == "linked" and statuses["School of Music"] == "uncertain"
    piano = next(r for r in log.pending["candidates"] if r["name"] == "Department of Piano")
    music = next(r for r in log.pending["candidates"] if r["name"] == "School of Music")
    assert piano["parent_candidate_id"] == music["candidate_id"]


def test_no_register_means_no_ids_no_file_and_no_llm_calls(world):
    world.bucket.broken = True
    log = run_log.RunLog("r1", "inv1", bucket=FakeBucket(), bq_client=None, results_dir=world.tmp)
    store = nr.RegisterStore(world.bucket, world.tmp / "hierarchy")
    with pytest.raises(nr.RegisterUnavailable):
        hf.run_university("Q1", 2, log, store, world.deps(), out_dir=world.tmp / "hierarchy")
    assert not (world.tmp / "hierarchy" / "Q1.json").exists() and world.asked == []


# ── pure helpers ─────────────────────────────────────────────────────────

def test_names_fit_after_the_university_and_parent_prefixes_are_removed():
    pre = ["New York University", "NYU", "Stern School of Business"]
    assert hf.names_fit("School of Law", "New York University School of Law", pre)
    assert hf.names_fit("Department of Finance", "Stern School of Business Department of Finance", pre)
    assert hf.names_fit("NYU Shanghai", "New York University Shanghai", pre)
    assert not hf.names_fit("Department of Mathematics", "Department of Computer Science", pre)


def test_two_llm_units_that_fit_one_snapshot_unit_are_both_left_uncertain():
    snap = [{"qid": "Q5", "label": "Department of Economics", "aliases_en": [], "P856_urls": []}]
    m = hf.match_units([{"name": "Department of Economics"}, {"name": "Economics Department"}], snap, [])
    assert m[0]["qid"] is None and m[1]["qid"] is None


def test_classify_snapshot_unit_reads_the_label_before_a_wrong_p31():
    u = {"label": "New York University Department of Accounting", "P31": [{"label": "business school"}]}
    assert hf.classify_snapshot_unit(u)[0] == "department"
    assert hf.classify_snapshot_unit({"label": "Bobst Library", "P31": [{"label": "academic library"}]})[1] == "no"


def test_tree_parent_is_the_most_specific_parent():
    snap = make_snapshot([snap_unit("Q30", "Department of Anthropology", ["academic department"],
                                    [("Q1", "P361"), ("Q11", "P361")])])
    assert hf.tree_parents(snap, "Q30") == ["Q11"]


def test_pending_when_a_parent_has_no_qid():
    node = {"qid": "Q5", "parent_links": [{"p749": "not applicable", "parent_qid": None}]}
    assert hf.alignment_of(node)[0] == "pending"


def test_journals_and_scholarships_are_not_units():
    assert hf.classify_snapshot_unit({"label": "New York University Law Review", "P31": [{"label": "law review"}]})[1] == "no"
    assert hf.classify_snapshot_unit({"label": "Root-Tilden Scholarship", "P31": []})[1] == "no"


def test_a_snapshot_unit_with_the_same_name_under_another_parent_is_a_candidate_not_a_match(world):
    world.answers["Q10"].append(llm("Department of History", "department"))   # History is under Art in Wikidata
    doc, _, _ = world.run()
    hist_law = next(n for n in doc["nodes"] if n["name"] == "Department of History"
                    and n["parents_shown"][0]["qid"] == "Q10")
    assert hist_law["qid"] is None and hist_law["alignment"] == "uncertain"
    assert {"Q12"} == {c["qid"] for c in hist_law["alignment_candidates"] if not c["chosen"]}
    assert hist_law["alignment_reason"].startswith("not in the snapshot; full search in week 6")


def test_the_parent_itself_is_never_a_same_name_candidate():
    snap = make_snapshot([snap_unit("Q40", "School of Mathematics", ["faculty"], [("Q1", "P749")])])
    got = hf.elsewhere_matches("Department of Mathematics", snap, "Q40", ["Test University", "School of Mathematics"])
    assert "Q40" not in {c["qid"] for c in got}
