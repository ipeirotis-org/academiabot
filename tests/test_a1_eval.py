"""Research question A1 (week 6): rates, weights, and the 'before' rows (fakes only)."""
import pytest

from wikidata_discover import a1_eval as a1, alignment


def test_candidate_sets_are_alone_then_cumulative_and_share_identical_sets():
    names = [n for n, _ in a1.candidate_sets()]
    assert names[0] == "alone: prefix" and names[1] == "alone: prefix + fulltext"
    assert names[-1] == "cumulative: " + " + ".join(alignment.SEARCH_KINDS)
    assert len(a1.distinct_sets()) == 10       # 6 alone + 5 cumulative; "prefix + fulltext" is both


def test_parse_label():
    assert a1.parse_label({"my_qid": "none"}) == []
    assert a1.parse_label({"my_qid": "q5", "second_qid": "Q6"}) == ["Q5", "Q6"]
    assert a1.parse_label({"my_qid": ""}) is None


def test_overall_rate_is_weighted_by_stratum_size_never_an_average_of_stratum_rates():
    strata = {"m1": "missing", "m2": "missing", "l1": "linked_or_orphan"}
    labels = {"m1": ["Q1"], "m2": [], "l1": ["Q3"]}
    status = {"m1": "new", "m2": "new", "l1": "new"}
    answers = {i: None for i in labels}
    sizes = {"missing": 200, "linked_or_orphan": 10}
    r = a1.rates(answers, status, labels, strata, {"missing": 2, "linked_or_orphan": 1}, sizes)
    assert r["missing"]["false_new_rate"] == 0.5
    assert r["linked_or_orphan"]["false_new_rate"] == 1.0
    # weights: missing 100 each, linked 10: (100 + 10) / (100 + 100 + 10)
    assert r["overall (weighted)"]["false_new_rate"] == pytest.approx(110 / 210)
    assert r["overall (weighted)"]["false_new_rate"] != pytest.approx((0.5 + 1.0) / 2)


def test_a_link_to_either_duplicate_is_right_and_uncertain_is_in_no_denominator():
    strata = {"a": "missing", "b": "missing", "c": "missing"}
    labels = {"a": ["Q1", "Q2"], "b": ["Q5"], "c": []}
    answers = {"a": "Q2", "b": "Q9", "c": None}
    status = {"a": "aligned", "b": "aligned", "c": "uncertain"}
    r = a1.rates(answers, status, labels, strata, {"missing": 3, "linked_or_orphan": 0})["missing"]
    assert r["false_link"] == 1 and r["given_qid"] == 2 and r["false_new_rate"] is None and r["uncertain"] == 1


def test_before_rows_mark_the_rebuilt_answer_and_give_orphans_alone():
    answers_rows = [{"item_id": "1", "stratum": "missing"}, {"item_id": "2", "stratum": "linked_or_orphan"},
                    {"item_id": "3", "stratum": "linked_or_orphan"}]
    before = {"1": {"status": "missing", "matched_qid": None, "rebuilt_qid": None},
              "2": {"status": "linked", "matched_qid": None, "rebuilt_qid": "Q7"},
              "3": {"status": "orphan", "matched_qid": "Q8", "rebuilt_qid": None}}
    labels = [{"item_id": "1", "my_qid": "Q4"}, {"item_id": "2", "my_qid": "Q7"}, {"item_id": "3", "my_qid": "Q9"}]
    rows = a1.score(labels, answers_rows, [], before)
    main = [r for r in rows if r["method"] == "before (us-tier1)"]
    assert all(r["note"] == "linked rows: approximate (rebuilt)" for r in main)
    orph = [r for r in rows if r["method"] == "before (us-tier1), orphans only"]
    assert orph and orph[0]["group"] == "linked_or_orphan" and orph[0]["false_link"] == 1
    m = next(r for r in main if r["group"] == "missing")
    assert m["false_new"] == 1 and m["called_new"] == 1


def test_rebuilt_linked_answer_uses_the_old_fuzzy_match_on_direct_children():
    snap = {"university_qid": "Q1", "units": {
        "Q1": {"label": "Test U", "depth": 0, "in_tree": True},
        "Q10": {"label": "Test U School of Law", "depth": 1, "in_tree": True},
        "Q11": {"label": "School of Law Clinic", "depth": 2, "in_tree": True}}}
    assert a1.rebuilt_linked_answer("Test U School of Law", snap) == "Q10"
    assert a1.rebuilt_linked_answer("School of Dance", snap) is None


def test_search_recall_counts_which_search_found_the_labeled_item():
    labels = [{"item_id": "1", "my_qid": "Q5"}, {"item_id": "2", "my_qid": "none"}]
    decisions = [{"item_id": "1", "searches": {"prefix": [], "fulltext": ["Q5"], "tree": ["Q5"]}}]
    rows = {r["search"]: r for r in a1.search_recall(labels, decisions)}
    assert rows["fulltext"]["found_by_this_search"] == 1 and rows["fulltext"]["only_this_search"] == 0
    assert rows["prefix"]["found_by_this_search"] == 0 and rows["prefix"]["units_with_a_qid"] == 1


class _Blob:
    def __init__(self, text=None):
        self.text = text

    def download_as_text(self):
        return self.text

    def exists(self):
        return False


class _Bucket:
    def __init__(self, files):
        self.files = files

    def blob(self, name):
        return _Blob(self.files.get(name))


def test_run_searches_each_unit_once_and_decides_every_set_both_ways(tmp_path):
    sheet = ("item_id,unit_name,university,university_qid,unit_type,website_or_source_url,my_qid,second_qid,notes\n"
             "A1-001,Wheelock College,Boston University,Q49110,School,,,,\n")
    answers = "item_id,candidate_id,stratum,status,matched_qid,matched_label,university_qid,name,source_file\n" \
              "A1-001,c1,missing,missing,,,Q49110,Wheelock College,x\n"
    from tests.test_alignment import FakeWikidata
    wd = FakeWikidata(items={"Q9": {"label": "BU Wheelock College"}}, fulltext={"Wheelock College": ["Q9"]})
    searcher = alignment.Searcher(wd)
    choose = lambda u, c: {"choice": c[0]["qid"], "also_same_unit": [], "reason": "", "llm_call_ids": [], "provider": "f"}
    yes_no = lambda u, c: {"same_unit": True, "reason": "", "llm_call_ids": [], "provider": "f"}
    path = a1.run(_Bucket({"a1/a1_labeling_sheet.csv": sheet, "a1/a1_pipeline_answers.csv": answers}), None,
                  workers=2, out_dir=tmp_path, searcher=searcher, choose_fn=choose, yes_no_fn=yes_no,
                  snapshots={"Q49110": None})
    import json
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    decisions = [l for l in lines if "mode" in l]
    assert len(decisions) == len(a1.distinct_sets()) * 2
    by = {(tuple(d["kinds"]), d["mode"]): d for d in decisions}
    assert by[(("prefix",), "choose")]["status"] == "new"
    assert by[(("prefix", "fulltext"), "choose")]["qid"] == "Q9"
    n_search_queries = searcher.queries
    assert n_search_queries == len(set(wd.queries))      # every query was sent once


def test_a_resumed_run_reuses_saved_responses_and_saves_them_again(tmp_path):
    from tests.test_alignment import FakeWikidata
    from tests.test_node_register import FakeBucket
    from wikidata_discover import run_log
    import json
    bucket = FakeBucket()
    first = alignment.Searcher(FakeWikidata(fulltext={"Wheelock College": []}),
                               log=run_log.RunLog("old", "i", bucket=bucket, results_dir=tmp_path))
    first.ask(alignment.fulltext_query("Wheelock College"))
    bucket.list_blobs = lambda prefix: [bucket.blob(k) for k in list(bucket.objects) if k.startswith(prefix)]
    for name in list(bucket.objects):
        blob = bucket.blob(name)
        blob.download_as_bytes = (lambda n=name: bucket.objects[n][0].encode())
    saved = {}
    for k in bucket.objects:
        if k.startswith("runs/old/searches/"):
            d = json.loads(bucket.objects[k][0])
            saved[d["query"]] = d["bindings"]
    wd = FakeWikidata(fulltext={"Wheelock College": []})
    new = alignment.Searcher(wd, log=run_log.RunLog("new", "i", bucket=bucket, results_dir=tmp_path), preloaded=saved)
    new.ask(alignment.fulltext_query("Wheelock College"))
    assert wd.queries == [] and new.reused == 1 and new.queries == 0
    assert any(k.startswith("runs/new/searches/") for k in bucket.objects)


def test_load_saved_responses_reads_the_run_folder():
    import json

    class Blob:
        def __init__(self, name, data):
            self.name, self.data = name, data

        def download_as_bytes(self):
            return self.data

    class Bucket:
        def list_blobs(self, prefix):
            assert prefix == "runs/r1/searches/"
            return [Blob("runs/r1/searches/a.json", json.dumps({"query": "Q", "bindings": [1]}).encode()),
                    Blob("runs/r1/searches/b.json", b"not json")]
    assert alignment.load_saved_responses(Bucket(), "r1") == {"Q": [1]}


def test_the_run_log_is_flushed_during_the_run(tmp_path):
    from tests.test_alignment import FakeWikidata
    rows = "".join(f"A1-{i:03d},School {i},Boston University,Q49110,School,,,,\n" for i in range(1, 21))
    sheet = "item_id,unit_name,university,university_qid,unit_type,website_or_source_url,my_qid,second_qid,notes\n" + rows
    answers = "item_id,candidate_id,stratum,status,matched_qid,matched_label,university_qid,name,source_file\n" + "".join(
        f"A1-{i:03d},c{i},missing,missing,,,Q49110,School {i},x\n" for i in range(1, 21))

    class Log:
        flushes = 0

        def flush(self):
            Log.flushes += 1
    choose = lambda u, c: {"choice": None, "also_same_unit": [], "reason": "", "llm_call_ids": [], "provider": "f"}
    a1.run(_Bucket({"a1/a1_labeling_sheet.csv": sheet, "a1/a1_pipeline_answers.csv": answers}), Log(), workers=1,
           out_dir=tmp_path, searcher=alignment.Searcher(FakeWikidata()), choose_fn=choose,
           yes_no_fn=lambda u, c: {}, snapshots={"Q49110": None})
    assert Log.flushes >= 2          # after units 10 and 20, before the end
