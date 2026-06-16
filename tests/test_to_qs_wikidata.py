"""Unit tests for QuickStatements export."""

from wikidata_discover.to_qs_wikidata import (
    build_quickstatements,
    parent_specs_for_item,
    type_qid_for,
)


def test_type_qid_for_phase_2_unit_types():
    assert type_qid_for("school") == "Q31855"
    assert type_qid_for("department") == "Q2467461"
    assert type_qid_for("lab") == "Q483242"
    assert type_qid_for("center") == "Q7315155"


def test_type_qid_for_unverified_types_fall_back_to_generic():
    # Q1664727 (a Catholic society), Q576104 (neonate) and Q33506 (museum) are
    # not organizational-unit classes; these types fall back to the generic
    # organization QID until verified class QIDs are supplied.
    assert type_qid_for("program") == "Q43229"
    assert type_qid_for("institute") == "Q43229"
    assert type_qid_for("division") == "Q43229"
    assert type_qid_for("campus") == "Q43229"


def test_build_quickstatements_creates_missing_with_recursive_parent():
    rows = [
        {
            "name": "Department of History",
            "unit_type": "department",
            "status": "missing",
            "url": "https://example.edu/history",
            "parent_qid": "Q1",
            "parent_label": "School of Arts",
        }
    ]

    lines = build_quickstatements(rows, "Q0", "Example University")

    assert lines == [
        "CREATE",
        'LAST|Len|"Department of History"',
        'LAST|Den|"department within School of Arts"',
        "LAST|P31|Q2467461",
        'LAST|P856|"https://example.edu/history"',
        "LAST|P749|Q1",
        "",
    ]


def test_build_quickstatements_links_orphan_without_create():
    rows = [
        {
            "name": "School of Science",
            "unit_type": "school",
            "status": "orphan",
            "qid": "Q2",
            "parent_qid": "Q0",
        }
    ]

    lines = build_quickstatements(rows, "Q0", "Example University")

    assert lines == [
        "Q2|P749|Q0",
        "",
    ]


def test_joint_parent_qids_emit_multiple_p749_statements():
    rows = [
        {
            "name": "Joint Program in Data Science",
            "unit_type": "program",
            "status": "missing",
            "parent_qid": "Q1",
            "additional_parent_qids": "Q2|Q3",
        }
    ]

    lines = build_quickstatements(rows, "Q0", "Example University")

    assert "LAST|P749|Q1" in lines
    assert "LAST|P749|Q2" in lines
    assert "LAST|P749|Q3" in lines


def test_linked_joint_unit_adds_extra_parents_without_create():
    # An already-linked unit that is cross-listed: no CREATE, just add P749 to
    # the matched QID for its other parents.
    rows = [
        {
            "name": "Cross-listed Program",
            "unit_type": "program",
            "status": "linked_joint",
            "qid": "Q5",
            "parent_qid": "Q1",
            "additional_parent_qids": "Q2",
        }
    ]

    lines = build_quickstatements(rows, "Q0", "Example University")

    assert "CREATE" not in lines
    assert "Q5|P749|Q2" in lines


def test_parent_specs_support_qualifiers_for_joint_units():
    specs = parent_specs_for_item(
        {
            "parent_qid": "Q1",
            "joint_parent_qids": ["Q2"],
            "joint_qualifiers": [{"property": "P3831", "value": "joint unit"}],
        },
        "Q0",
    )

    assert specs == [
        {"qid": "Q1", "qualifiers": []},
        {"qid": "Q2", "qualifiers": [{"property": "P3831", "value": "joint unit"}]},
    ]
