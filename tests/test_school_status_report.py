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
    assert s["providers"] == {"openai": 3, "anthropic": 1}
    groups = {g: (n, c) for g, n, c in s["by_group"]}
    assert groups["school"] == (2, {"linked": 1, "orphan": 1}) and groups["campus"] == (1, {"missing": 1})
    assert groups["office"] == (1, {"missing": 1}) and groups["other"] == (0, {})
    scope = {k: (n, c) for k, n, c in s["by_scope"]}
    assert scope["yes"] == (3, {"linked": 1, "missing": 1, "orphan": 1}) and scope["no"] == (1, {"missing": 1})


def test_type_group_follows_the_scope_rule():
    cases = {"College": "school", "Graduate School": "school", "academic division": "school", "كلية": "school",
             "Campus": "campus", "Branch Campus": "campus", "Instructional Site": "campus",
             "Regional Education Center": "campus",
             "Department": "department", "Departamento Académico": "department",
             "Academic Program": "program", "Online Program": "program", "Online Campus": "program",
             "Certificate Program": "program", "Major": "program",
             "Administrative Office": "office", "Student Services": "office", "Administrative Division": "office",
             "Office of the Registrar": "office", "Career Services": "office",
             "Research Center": "other", "Institute": "other", "Library": "other", "Camp": "other", None: "other"}
    assert {t: ssr.type_group(t) for t in cases} == cases
    assert [ssr.in_scope(t) for t in ("School", "Campus", "Department", "Program", "Office", "Center")] == \
        ["yes", "yes", "yes", "yes", "no", "check"]


def test_summary_sheet_has_status_scope_group_and_type_tables():
    lines, heads = ssr.summary_lines(ssr.summarize(CANDS, UNIS), "2026-10-29")
    titles = [lines[h - 1][0] for h in heads]
    assert titles[1:] == ["Status", "in_scope", "type_group", "unit_type (as the AI gave it, case made uniform)"]
    office = next(l for l in lines if l and l[0] == "office")
    assert office == ("office", 1, 25.0, 0, 0, 1, 0, "no")


def test_school_rows_note_linked_rows_without_a_match_and_keep_orphan_matches():
    rows = ssr.school_rows(CANDS, dict(UNIS))
    assert [r["University"] for r in rows] == ["alpha College", "alpha College", "Beta University", "Beta University"]
    law = next(r for r in rows if r["School"] == "School of Law")
    assert law["Matched QID"] == "" and law["Note"] == ssr.LINKED_NOTE
    arts = next(r for r in rows if r["School"] == "College of Arts")
    assert (arts["Matched QID"], arts["Matched label"], arts["Note"]) == ("Q9", "Beta College of Arts", "")
    campus = next(r for r in rows if r["School"] == "Main Campus")
    assert (campus["type_group"], campus["in_scope"]) == ("campus", "yes")
    office = next(r for r in rows if r["School"] == "Career Services")
    assert (office["type_group"], office["in_scope"]) == ("office", "no")
    assert "Likely not a school" not in law and "Flag reason" not in law


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
    assert "Panos" in md and "Out of scope (offices)" in md and "17 are in scope and missing" in md
    assert "3 of 4 orphans" in md and "no matched QID" in md and "Gamma Institute (Q3)" in md
    assert "—" not in md
