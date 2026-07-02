"""Unit tests for batch QuickStatements aggregation (pure logic, no network/BQ)."""

from wikidata_discover.batch_qs import (
    aggregate_quickstatements,
    block_diff_keys,
    bq_unit_to_export_row,
    build_attributed_blocks,
    build_attributed_lines,
    filter_new_blocks,
    load_local_export_rows,
    quickstatements_batch_rows,
    unit_key,
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


def test_attribution_tags_lines_with_university_and_key():
    a = _missing_school("School A", "Q100")
    b = _missing_school("School B", "Q200")
    _, attributed = build_attributed_lines([a, b])
    universities = {u for u, _, _ in attributed}
    assert universities == {"Q100", "Q200"}
    # Every recorded line is non-blank and carries a unit key.
    assert all(line.strip() and key for _, key, line in attributed)


def test_local_row_without_parent_qid_falls_back_to_university():
    # Older/top-level CSV rows have university_qid but no parent_qid; the batch
    # builder must still emit LAST|P749|<university>.
    row = {
        "name": "School of Law",
        "unit_type": "school",
        "status": "missing",
        "qid": "",
        "university_qid": "Q49210",
        "university_label": "NYU",
        "level": 1,
    }
    lines = aggregate_quickstatements([row])
    assert "LAST|P749|Q49210" in lines


def test_bq_adapter_carries_existing_parent_qids_to_suppress_duplicates():
    # A cross-listed orphan whose LLM-resolved joint parent (Q5) is already a
    # parent must not re-emit that P749.
    unit = {
        "status": "exists_orphan",
        "unit_name": "Joint Program",
        "matched_qid": "Q99",
        "parent_qid": "Q1",
        "additional_parent_qids": "Q5",
        "existing_parent_qids": "Q5",
        "level": 2,
        "university_qid": "Q1",
    }
    row = bq_unit_to_export_row(unit)
    assert row["existing_parent_qids"] == "Q5"
    lines = aggregate_quickstatements([row])
    # Q5 is already a parent, so it should not appear as an added P749.
    assert not any(line.endswith("|P749|Q5") for line in lines)


def test_quickstatements_batch_rows_shape():
    attributed = [("Q1", "create|Q1|a|Q1", "CREATE"),
                  ("Q1", "create|Q1|a|Q1", 'LAST|Len|"School A"')]
    rows = quickstatements_batch_rows("batch-123", attributed)
    assert rows[0] == {
        "run_id": "batch-123",
        "university_qid": "Q1",
        "unit_key": "create|Q1|a|Q1",
        "qs_line": "CREATE",
        "uploaded_at": None,
    }
    assert len(rows) == 2


def test_unit_key_stable_for_same_missing_unit():
    a = _missing_school("School of Law", "Q1", parent_qid="Q10")
    b = _missing_school("School of Law", "Q1", parent_qid="Q10")
    assert unit_key(a) == unit_key(b)


def test_unit_key_differs_by_parent_and_university():
    base = _missing_school("School of Law", "Q1", parent_qid="Q10")
    other_parent = _missing_school("School of Law", "Q1", parent_qid="Q20")
    other_uni = _missing_school("School of Law", "Q2", parent_qid="Q10")
    assert unit_key(base) != unit_key(other_parent)
    assert unit_key(base) != unit_key(other_uni)


def test_load_local_export_rows_scans_only_given_dir(tmp_path):
    (tmp_path / "missing_divisions_Q1.csv").write_text(
        "name,status,parent_qid,university_qid,level\nSchool A,missing,Q1,Q1,1\n"
    )
    other = tmp_path / "other"
    other.mkdir()
    (other / "missing_divisions_Q2.csv").write_text(
        "name,status,parent_qid,university_qid,level\nStale School,missing,Q2,Q2,1\n"
    )
    rows = load_local_export_rows(directories=[tmp_path])
    names = {r["name"] for r in rows}
    assert names == {"School A"}  # the 'other' dir (stale) is not scanned


def test_filter_new_blocks_drops_fully_emitted_create():
    a = _missing_school("School A", "Q1", parent_qid="Q1")
    b = _missing_school("School B", "Q1", parent_qid="Q1")
    blocks = build_attributed_blocks([a, b])
    emitted = block_diff_keys(*blocks[0])  # everything block A would emit
    survivors, skipped = filter_new_blocks(blocks, emitted)
    assert skipped == 1
    survivor_names = {r.get("name") for r, _ in survivors}
    assert survivor_names == {"School B"}


def test_filter_new_blocks_keeps_link_with_new_joint_parent():
    # An orphan Q99 under Q1 also cross-listed under joint parents. A first batch
    # emitted P749 to Q2; a later run adds Q3 as a new joint parent.
    row = {
        "status": "orphan",
        "name": "Joint Program",
        "qid": "Q99",
        "parent_qid": "Q1",
        "additional_parent_qids": "Q2|Q3",
        "level": 2,
        "university_qid": "Q1",
    }
    blocks = build_attributed_blocks([row])
    keys = block_diff_keys(*blocks[0])
    assert "link|Q99|Q3" in keys
    emitted = {"link|Q99|Q1", "link|Q99|Q2"}  # Q3 not yet emitted
    survivors, skipped = filter_new_blocks(blocks, emitted)
    assert skipped == 0  # the new Q3 link keeps the block
