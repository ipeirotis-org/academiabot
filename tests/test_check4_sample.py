import csv
import json
from pathlib import Path

import pytest

from wikidata_discover.scripts import check4_sample as c4


def cands():
    rows = [{"candidate_id": f"{i:04d}", "status": "missing", "name": f"School {i}",
             "university_qid": "Q1", "website": f"https://x.edu/{i}"} for i in range(50)]
    rows.append({"candidate_id": "9999", "status": "linked", "name": "Linked", "university_qid": "Q1"})
    return rows


def test_draw_takes_only_missing_and_ignores_input_order():
    a = c4.draw(cands())
    b = c4.draw(list(reversed(cands())))
    assert [r["candidate_id"] for r in a] == [r["candidate_id"] for r in b]
    assert len(a) == 20 and all(r["status"] == "missing" for r in a)


def test_sheet_rows_links_url_and_empty_columns():
    row = c4.sheet_rows([cands()[3]], {"Q1": "Test University"})[0]
    assert row["search_school_only"].endswith("search=School+3&fulltext=1")
    assert "search=Test+University+School+3" in row["search_university_plus_school"]
    assert row[c4.URL_COLUMN] == "https://x.edu/3"
    assert row["already on Wikidata (QID)"] == "" and row["really a school (yes/no)"] == ""


REPO = Path(__file__).resolve().parents[1]
REAL_INPUT = REPO / c4.DEFAULT_CANDIDATES
SHEET = REPO / "wikidata_discover/results/check4_missing_sample.csv"


def dry_run_text():
    rows = [{"candidate_id": f"{i:05d}", "status": "missing", "name": f"School {i}", "university_qid": "Q1"}
            for i in range(c4.EXPECTED_MISSING)]
    rows += [{"candidate_id": "z1", "status": "linked", "name": "Linked", "university_qid": "Q1"}]
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def test_a_fresh_checkout_downloads_the_input_and_draws_the_same_20_rows(tmp_path, monkeypatch):
    text = dry_run_text()
    local = tmp_path / "with_file"
    (local / c4.DEFAULT_CANDIDATES).parent.mkdir(parents=True)
    (local / c4.DEFAULT_CANDIDATES).write_text(text)
    monkeypatch.chdir(local)
    from_local = c4.draw(c4.load_candidates(None, download=lambda url: pytest.fail("no download needed")))

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    asked = []
    bucket = {c4.BUCKET_CANDIDATES: text}
    from_bucket = c4.draw(c4.load_candidates(None, download=lambda url: asked.append(url) or bucket[url]))
    assert asked == [c4.BUCKET_CANDIDATES]
    assert [r["candidate_id"] for r in from_bucket] == [r["candidate_id"] for r in from_local]


@pytest.mark.skipif(not REAL_INPUT.exists(), reason="the real dry-run file is only on a machine that ran it")
def test_the_bucket_path_gives_the_committed_20_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cands = c4.load_candidates(None, download=lambda url: REAL_INPUT.read_text())
    want = [r["candidate_id"] for r in csv.DictReader(SHEET.open())]
    assert [r["candidate_id"] for r in c4.draw(cands)] == want


def test_no_local_file_and_no_bucket_stops_with_both_places_and_the_input_option(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def unreachable(url):
        raise ConnectionError("no route to host")
    with pytest.raises(c4.InputError) as e:
        c4.load_candidates(None, download=unreachable)
    msg = str(e.value)
    assert c4.DEFAULT_CANDIDATES in msg and c4.BUCKET_CANDIDATES in msg and "--input" in msg


def test_main_returns_1_and_writes_nothing_when_no_input_can_be_read(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    def down(url):
        raise ConnectionError("down")
    monkeypatch.setattr(c4, "download_gs", down)
    assert c4.main(["--out", "out.csv"]) == 1
    assert not (tmp_path / "out.csv").exists()


def test_an_input_with_the_wrong_number_of_missing_rows_is_refused(tmp_path):
    p = tmp_path / "other.jsonl"
    p.write_text(json.dumps({"candidate_id": "a", "status": "missing"}) + "\n")
    with pytest.raises(c4.InputError, match="1 missing rows, not 6413"):
        c4.load_candidates(str(p), download=lambda url: "")
