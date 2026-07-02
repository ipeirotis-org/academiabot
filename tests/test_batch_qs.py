"""Unit tests for batch QuickStatements aggregation (pure logic, no network/BQ)."""

from wikidata_discover.batch_qs import (
    aggregate_quickstatements,
    bq_unit_to_export_row,
    build_attributed_lines,
    quickstatements_batch_rows,
)


def _missing_school(name, university_qid, level=1, parent_qid="Q1"):
    return {
        "name": name,
        "unit_type": "school",
        "url": "",
        "status": "missing",
        "qid": "",
        "parent_qid": parent_qid,
        "parent_label": "Some University",
        "additional_parent_qids": "",
        "level": level,
        "university_qid": university_qid,
        "university_label": "Some University",
    }


def test_bq_unit_missing_maps_to_missing_row():
    unit = {
        "status": "missing",
        "unit_name": "School of Law",
        "unit_type": "school",
        "website": "https://law.example.edu",
        "matched_qid": "",
        "parent_qid": "Q1",
        "parent_label": "Example U",
        "additional_parent_qids": "",
        "level": 1,
        "university_qid": "Q1",
    }
    row = bq_unit_to_export_row(unit)
    assert row["status"] == "missing"
    assert row["name"] == "School of Law"
    assert row["url"] == "https://law.example.edu"


def test_bq_unit_exists_orphan_maps_to_orphan():
    unit = {"status": "exists_orphan", "unit_name": "X", "matched_qid": "Q99",
            "additional_parent_qids": ""}
    assert bq_unit_to_export_row(unit)["status"] == "orphan"


def test_bq_unit_exists_linked_with_joint_maps_to_linked_joint():
    unit = {"status": "exists_linked", "unit_name": "X", "matched_qid": "Q99",
            "additional_parent_qids": "Q5"}
    assert bq_unit_to_export_row(unit)["status"] == "linked_joint"


def test_bq_unit_plain_exists_linked_is_dropped():
    unit = {"status": "exists_linked", "unit_name": "X", "matched_qid": "Q99",
            "additional_parent_qids": ""}
    assert bq_unit_to_export_row(unit) is None


def test_aggregate_orders_schools_before_departments():
    dept = _missing_school("Dept of History", "Q1", level=2, parent_qid="Q10")
    dept["unit_type"] = "department"
    school = _missing_school("School of Arts", "Q1", level=1, parent_qid="Q1")
    lines = aggregate_quickstatements([dept, school])
    text = "\n".join(lines)
    # The school's label line must appear before the department's.
    assert text.index("School of Arts") < text.index("Dept of History")


def test_attribution_tags_lines_with_university():
    a = _missing_school("School A", "Q100")
    b = _missing_school("School B", "Q200")
    _, attributed = build_attributed_lines([a, b])
    universities = {u for u, _ in attributed}
    assert universities == {"Q100", "Q200"}
    # Every recorded line is non-blank.
    assert all(line.strip() for _, line in attributed)


def test_quickstatements_batch_rows_shape():
    attributed = [("Q1", "CREATE"), ("Q1", 'LAST|Len|"School A"')]
    rows = quickstatements_batch_rows("batch-123", attributed)
    assert rows[0] == {
        "run_id": "batch-123",
        "university_qid": "Q1",
        "qs_line": "CREATE",
        "uploaded_at": None,
    }
    assert len(rows) == 2
