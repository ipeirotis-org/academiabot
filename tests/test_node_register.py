"""Node id register (week 5): ids stay the same across runs, and are never guessed."""
import json

import pytest

from wikidata_discover import node_register as nr


class PreconditionFailed(Exception):
    code = 412


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    @property
    def generation(self):
        return self.bucket.objects.get(self.name, (None, 0))[1]

    def exists(self):
        if self.bucket.broken:
            raise OSError("bucket unreachable")
        return self.name in self.bucket.objects

    def reload(self):
        pass

    def download_as_bytes(self, if_generation_match=None):
        return self.bucket.objects[self.name][0].encode()

    def upload_from_string(self, text, content_type=None, if_generation_match=None, timeout=None):
        if self.bucket.broken:
            raise OSError("bucket unreachable")
        if self.bucket.before_write:
            hook, self.bucket.before_write = self.bucket.before_write, None
            hook()
        if if_generation_match is not None and if_generation_match != self.generation:
            raise PreconditionFailed("generation changed")
        self.bucket.objects[self.name] = (text if isinstance(text, str) else text.decode(), self.generation + 1)


class FakeBucket:
    name = "fake-bucket"

    def __init__(self, broken=False):
        self.objects, self.broken, self.before_write = {}, broken, None

    def blob(self, name):
        return FakeBlob(self, name)


def unit(key, name, parent="root", qid=None, website=None, source_url=None, aliases=()):
    return {"key": key, "qid": qid, "name": name, "aliases": list(aliases), "parent_keys": [parent],
            "website": website, "source_url": source_url, "is_root": False}


ROOT = {"key": "root", "qid": "Q1", "name": "Uni", "aliases": [], "parent_keys": [], "website": None,
        "source_url": None, "is_root": True}


def first_run(units):
    return nr.assign_ids(nr.empty_register("Q1"), [ROOT] + units, "run1")


def test_norm_url_ignores_scheme_www_and_trailing_slash():
    assert nr.norm_url("https://www.Stern.nyu.edu/") == nr.norm_url("http://stern.nyu.edu") == "stern.nyu.edu"
    assert nr.norm_url("not a url") == ""


def test_a_second_run_on_the_same_input_gives_every_unit_the_same_id():
    units = [unit("a", "School of Law", qid="Q10"), unit("b", "School of Art"),
             unit("c", "Department of History", parent="b", source_url="https://u.edu/art/history")]
    ids1, _, reg = first_run(units)
    ids2, flags, _ = nr.assign_ids(reg, [ROOT] + units, "run2")
    assert ids1 == ids2 and not flags
    assert ids1["root"] == "root" and len(set(ids1.values())) == 4


def test_a_renamed_unit_and_a_unit_that_gets_a_qid_keep_their_ids():
    """SPECS week 5 test: rename one unit and give another a QID, then run again."""
    units = [unit("a", "School of Art", website="https://art.u.edu"), unit("b", "School of Law")]
    ids1, _, reg = first_run(units)
    renamed = unit("a", "College of Art and Design", website="https://art.u.edu/")
    now_qid = unit("b", "School of Law", qid="Q77")
    ids2, flags, reg2 = nr.assign_ids(reg, [ROOT, renamed, now_qid], "run2")
    assert ids2["a"] == ids1["a"] and ids2["b"] == ids1["b"]
    assert not flags
    assert reg2["entries"][ids1["b"]]["qid"] == "Q77"
    # the old name stays known, so the old name also still matches later
    assert "school of art" in reg2["entries"][ids1["a"]]["names"]


def test_a_unit_matched_to_a_qid_later_keeps_its_id_by_qid_even_after_a_rename():
    ids1, _, reg = first_run([unit("a", "School of Law", qid="Q77")])
    ids2, _, _ = nr.assign_ids(reg, [ROOT, unit("x", "Law School", qid="Q77")], "run2")
    assert ids2["x"] == ids1["a"]


def test_two_same_name_units_under_one_parent_are_told_apart_by_url():
    units = [unit("a", "Graduate Program", parent="root", source_url="https://u.edu/p1"),
             unit("b", "Graduate Program", parent="root", source_url="https://u.edu/p2")]
    ids1, _, reg = first_run(units)
    ids2, flags, _ = nr.assign_ids(reg, [ROOT] + list(reversed(units)), "run2")
    assert ids2 == ids1 and not flags


def test_same_name_units_that_the_url_cannot_tell_apart_get_new_ids_and_a_flag():
    units = [unit("a", "Graduate Program"), unit("b", "Graduate Program")]
    ids1, _, reg = first_run(units)
    ids2, flags, _ = nr.assign_ids(reg, [ROOT] + units, "run2")
    assert set(ids2.values()).isdisjoint({ids1["a"], ids1["b"]})
    assert flags["a"] and flags["b"]


def test_a_move_needs_name_and_url():
    units = [unit("s1", "School A"), unit("s2", "School B"),
             unit("d", "Department of Economics", parent="s1", website="https://u.edu/econ")]
    ids1, _, reg = first_run(units)
    moved = unit("d", "Department of Economics", parent="s2", website="https://u.edu/econ")
    ids2, flags, _ = nr.assign_ids(reg, [ROOT, units[0], units[1], moved], "run2")
    assert ids2["d"] == ids1["d"] and not flags.get("d")


def test_a_name_only_match_under_another_parent_is_not_a_move():
    units = [unit("s1", "School A"), unit("s2", "School B"),
             unit("d", "Department of Economics", parent="s1", website="https://a.u.edu/econ")]
    ids1, _, reg = first_run(units)
    other = unit("d2", "Department of Economics", parent="s2", website="https://b.u.edu/econ")
    ids2, flags, _ = nr.assign_ids(reg, [ROOT, units[0], units[1], other], "run2")
    assert ids2["d2"] != ids1["d"]
    assert ids1["d"] in flags["d2"][0]


def test_store_writes_with_a_generation_check_and_reloads_on_conflict(tmp_path):
    bucket = FakeBucket()
    store = nr.RegisterStore(bucket, tmp_path)
    ids1, *_ = nr.assign_and_save(store, "Q1", [ROOT, unit("a", "School A")], "run1")

    # Another run writes the register between this run's load and its save.
    def other_run_writes():
        reg, gen = store.load("Q1")
        _, _, reg2 = nr.assign_ids(reg, [ROOT, unit("a", "School A"), unit("z", "School Z")], "other")
        store.save(reg2, gen)
    bucket.before_write = other_run_writes
    ids2, _, reg, _, loaded_gen, saved_gen = nr.assign_and_save(store, "Q1", [ROOT, unit("a", "School A"),
                                                                              unit("b", "School B")], "run2")
    assert ids2["a"] == ids1["a"]
    names = {e["name"] for e in reg["entries"].values()}
    assert {"School Z", "School B"} <= names              # neither run's unit was lost
    assert saved_gen == loaded_gen + 1
    assert json.loads((tmp_path / "Q1" / "ids.json").read_text())["entries"].keys() == reg["entries"].keys()


def test_an_unreachable_bucket_assigns_no_ids(tmp_path):
    store = nr.RegisterStore(FakeBucket(broken=True), tmp_path)
    with pytest.raises(nr.RegisterUnavailable):
        nr.assign_and_save(store, "Q1", [ROOT, unit("a", "School A")], "run1")
    assert not (tmp_path / "Q1" / "ids.json").exists()


def test_a_joint_unit_in_the_tree_twice_keeps_both_ids_across_runs():
    """The Furman Center is under Wagner and under Law on Wikidata, with one QID (check 5)."""
    units = [unit("w", "Wagner", qid="Q618001"), unit("l", "Law", qid="Q846173"),
             unit("f1", "Furman Center", parent="w", qid="Q594306"),
             unit("f2", "Furman Center for Real Estate", parent="l", qid="Q594306")]
    ids1, _, reg = first_run(units)
    ids2, flags, reg2 = nr.assign_ids(reg, [ROOT] + units, "run2")
    assert ids2 == ids1 and not flags
    ids3, flags3, _ = nr.assign_ids(reg2, [ROOT] + units, "run3")
    assert ids3 == ids1 and not flags3


def test_entries_with_the_same_qid_and_parent_merge_into_the_oldest_id():
    reg = nr.empty_register("Q1")
    reg["entries"] = {"n00001": {"qid": "Q9", "names": ["center"], "parents": ["root"]},
                      "n00007": {"qid": "Q9", "names": ["center"], "parents": ["root"]}}
    reg["next_seq"] = 8
    ids, flags, reg2 = nr.assign_ids(reg, [ROOT, unit("c", "Center", qid="Q9")], "run")
    assert ids["c"] == "n00001" and reg2["merged"] == {"n00007": "n00001"}
    ids2, flags2, _ = nr.assign_ids(reg2, [ROOT, unit("c", "Center", qid="Q9")], "run2")
    assert ids2["c"] == "n00001" and not flags2
