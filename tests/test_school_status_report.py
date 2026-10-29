"""School status report (pure parts). No network."""
from wikidata_discover import school_status_report as ssr

UNIS = [("Q2", "Beta University"), ("Q1", "alpha College"), ("Q3", "Gamma Institute")]
CANDS = [
    {"university_qid": "Q1", "name": "School of Law", "unit_type": "School", "status": "linked", "provider": "openai"},
    {"university_qid": "Q1", "name": "Main Campus", "unit_type": "Campus", "status": "missing", "provider": "openai"},
    {"university_qid": "Q2", "name": "College of Arts", "unit_type": "college", "status": "orphan",
     "matched_qid": "Q9", "matched_label": "Beta College of Arts", "provider": "openai"},
    {"university_qid": "Q2", "name": "Career Services", "unit_type": "Student Services", "status": "missing",
     "provider": "anthropic"},
]


def test_summary_counts_statuses_types_and_universities_without_a_linked_school():
    s = ssr.summarize(CANDS, UNIS)
    assert s["schools"] == 4 and s["universities_with_schools"] == 2
    assert s["universities_not_included"] == ["Gamma Institute (Q3)"]
    assert s["by_status"]["missing"] == (2, 50.0) and s["by_status"]["unresolved"] == (0, 0.0)
    types = {t: n for t, n, _ in s["by_type"]}
    assert types == {"school": 1, "campus": 1, "college": 1, "student services": 1}
    assert s["universities_with_0_linked"] == 1          # Q2 has rows but no linked school
    assert s["flagged_not_school"] == 2 and s["providers"] == {"openai": 3, "anthropic": 1}


def test_unit_types_like_campus_office_program_or_service_are_flagged():
    assert ssr.not_school_word("Branch Campus") == "campus"
    assert ssr.not_school_word("Administrative Office") == "office"
    assert ssr.not_school_word("Academic Program") == "program"
    assert ssr.not_school_word("Student Services") == "service"
    assert ssr.not_school_word("College") == "" and ssr.not_school_word("Graduate School") == ""
    assert ssr.not_school_word(None) == ""


def test_school_rows_note_linked_rows_without_a_match_and_keep_orphan_matches():
    rows = ssr.school_rows(CANDS, dict(UNIS))
    assert [r["University"] for r in rows] == ["alpha College", "alpha College", "Beta University", "Beta University"]
    law = next(r for r in rows if r["School"] == "School of Law")
    assert law["Matched QID"] == "" and law["Note"] == ssr.LINKED_NOTE
    arts = next(r for r in rows if r["School"] == "College of Arts")
    assert (arts["Matched QID"], arts["Matched label"], arts["Note"]) == ("Q9", "Beta College of Arts", "")
    campus = next(r for r in rows if r["School"] == "Main Campus")
    assert (campus["Likely not a school"], campus["Flag reason"]) == ("yes", "campus")


def test_university_rows_are_sorted_by_name_and_use_the_snapshot_counts():
    snap = {"university_qid": "Q2", "units": {
        "Q2": {"in_tree": True, "depth": 0}, "Q20": {"in_tree": True, "depth": 1},
        "Q21": {"in_tree": True, "depth": 2}, "Q22": {"in_tree": False, "depth": 1}}}
    rows = ssr.university_rows(CANDS, UNIS, {"Q2": snap})
    assert [r["QID"] for r in rows] == ["Q1", "Q2", "Q3"]
    beta = rows[1]
    assert (beta["Schools proposed"], beta["Orphan"], beta["Missing"]) == (2, 1, 1)
    assert (beta["Units on Wikidata (all levels)"], beta["Units on Wikidata (directly below)"]) == (2, 1)
    assert rows[2]["Schools proposed"] == 0 and rows[2]["Units on Wikidata (all levels)"] is None


def test_the_summary_page_names_the_caveats():
    md = ssr.summary_md(ssr.summarize(CANDS, UNIS), "2026-10-29")
    assert "1 is already on" in md and "6 are not schools" in md
    assert "3 of 4 orphans" in md and "no matched QID" in md and "Gamma Institute (Q3)" in md
    assert "—" not in md
