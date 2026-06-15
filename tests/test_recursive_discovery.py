"""Unit tests for recursive discovery tree helpers."""

from wikidata_discover.discovery import (
    collect_missing,
    count_statuses,
    extract_joint_parent_names,
    flatten_discovered_units,
    normalize_unit_type,
    resolve_parent_qids,
)


def test_normalize_unit_type_defaults_by_level():
    assert normalize_unit_type(None, level=1) == "school"
    assert normalize_unit_type(None, level=2) == "department"
    assert normalize_unit_type(None, level=3) == "unit"


def test_normalize_unit_type_aliases():
    assert normalize_unit_type("dept", level=2) == "department"
    assert normalize_unit_type("Research Centre", level=3) == "center"
    assert normalize_unit_type("Laboratory", level=3) == "lab"
    assert normalize_unit_type("College", level=1) == "school"


def test_collect_missing_flattens_missing_and_orphans():
    tree = {
        "children": [
            {
                "name": "School of Arts",
                "status": "exists_linked",
                "children": [
                    {
                        "name": "Department of History",
                        "unit_type": "department",
                        "website": "https://example.edu/history",
                        "location": "New York, NY",
                        "status": "missing",
                        "qid": None,
                        "parent_qid": "Q1",
                        "parent_label": "School of Arts",
                        "university_qid": "Q0",
                        "university_label": "Example University",
                        "level": 2,
                        "path": ["Example University", "School of Arts", "Department of History"],
                        "children": [],
                    }
                ],
            },
            {
                "name": "School of Science",
                "unit_type": "school",
                "status": "exists_orphan",
                "qid": "Q2",
                "parent_qid": "Q0",
                "parent_label": "Example University",
                "university_qid": "Q0",
                "university_label": "Example University",
                "level": 1,
                "path": ["Example University", "School of Science"],
                "children": [],
            },
        ]
    }

    rows = collect_missing(tree)

    assert [row["name"] for row in rows] == [
        "Department of History",
        "School of Science",
    ]
    assert rows[0]["status"] == "missing"
    assert rows[0]["parent_qid"] == "Q1"
    assert rows[0]["path"] == "Example University > School of Arts > Department of History"
    assert rows[1]["status"] == "orphan"
    assert rows[1]["qid"] == "Q2"


def test_count_statuses_counts_nested_candidates():
    tree = {
        "children": [
            {
                "status": "exists_linked",
                "children": [
                    {"status": "missing", "children": []},
                    {"status": "exists_orphan", "children": []},
                ],
            },
            {"status": "exists_linked", "children": []},
        ]
    }

    assert count_statuses(tree) == {
        "total_candidates": 4,
        "exists_linked": 2,
        "exists_orphan": 1,
        "missing": 1,
    }


def test_flatten_discovered_units_includes_all_candidates():
    tree = {
        "children": [
            {
                "name": "School of Arts",
                "unit_type": "school",
                "website": "https://example.edu/arts",
                "location": "",
                "status": "exists_linked",
                "qid": "Q1",
                "parent_qid": "Q0",
                "parent_label": "Example University",
                "university_qid": "Q0",
                "university_label": "Example University",
                "level": 1,
                "path": ["Example University", "School of Arts"],
                "reference": "https://example.edu/schools",
                "children": [
                    {
                        "name": "Department of History",
                        "unit_type": "department",
                        "website": None,
                        "location": "New York, NY",
                        "status": "missing",
                        "qid": None,
                        "parent_qid": "Q1",
                        "parent_label": "School of Arts",
                        "university_qid": "Q0",
                        "university_label": "Example University",
                        "level": 2,
                        "path": ["Example University", "School of Arts", "Department of History"],
                        "reference": None,
                        "is_joint": True,
                        "parent_names": ["School of Science"],
                        "additional_parent_qids": ["Q2"],
                        "unresolved_parent_names": [],
                        "evidence": "listed as jointly administered",
                        "children": [],
                    }
                ],
            }
        ]
    }

    rows = flatten_discovered_units(tree, run_id="run-1", discovered_at="2026-05-28T00:00:00+00:00")

    assert len(rows) == 2
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["unit_name"] == "School of Arts"
    assert rows[0]["matched_qid"] == "Q1"
    assert rows[0]["path"] == "Example University > School of Arts"
    assert rows[1]["unit_name"] == "Department of History"
    assert rows[1]["status"] == "missing"
    assert rows[1]["parent_qid"] == "Q1"
    assert rows[1]["is_joint"] is True
    assert rows[1]["parent_names"] == "School of Science"
    assert rows[1]["additional_parent_qids"] == "Q2"


def test_extract_joint_parent_names_ignores_current_parent_and_dedupes():
    division = {
        "parent_names": ["School of Arts", "School of Science"],
        "joint_with": "School of Science; School of Engineering",
    }

    assert extract_joint_parent_names(division, "School of Arts") == [
        "School of Science",
        "School of Engineering",
    ]


def test_resolve_parent_qids_matches_known_choices():
    result = resolve_parent_qids(
        ["School of Science", "Unknown School"],
        current_parent_qid="Q1",
        choices=[
            ("Q1", "School of Arts"),
            ("Q2", "School of Science"),
            ("Q3", "School of Engineering"),
        ],
    )

    assert result == {
        "qids": ["Q2"],
        "resolved_names": ["School of Science"],
    }
