"""Unit tests for recursive discovery tree helpers."""

from wikidata_discover.discovery import (
    classify_search_match,
    collect_missing,
    count_statuses,
    extract_joint_parent_names,
    flatten_discovered_units,
    normalize_unit_type,
    registrable_domain,
    resolve_parent_qids,
    same_registrable_domain,
)


def test_children_queries_cover_downward_hierarchy_predicates():
    # Existing children can be modeled from the parent side via P355/P527/P199
    # (has subsidiary / has part / business division), not only child-side
    # P361/P749. The direct-children queries must read all of them so such units
    # are matched instead of re-created as duplicates.
    from wikidata_discover.discovery import (
        CHILDREN_SPARQL_TEMPLATE,
        CHILDREN_ALT_LABELS_SPARQL_TEMPLATE,
    )

    for template in (CHILDREN_SPARQL_TEMPLATE, CHILDREN_ALT_LABELS_SPARQL_TEMPLATE):
        for predicate in ("P361", "P749", "P355", "P527", "P199"):
            assert f"wdt:{predicate}" in template


def test_registrable_domain_normalizes_subdomains():
    assert registrable_domain("https://www.nyu.edu/") == "nyu.edu"
    assert registrable_domain("https://med.nyu.edu/biology") == "nyu.edu"
    assert registrable_domain("nyu.edu") == "nyu.edu"
    assert registrable_domain(None) is None
    assert registrable_domain("") is None


def test_same_registrable_domain():
    assert same_registrable_domain("https://www.nyu.edu", "https://med.nyu.edu/x") is True
    assert same_registrable_domain("https://nyu.edu", "https://stanford.edu") is False
    assert same_registrable_domain(None, "https://nyu.edu") is False


def test_registrable_domain_handles_compound_public_suffixes():
    # ac.uk / edu.au are public suffixes: unrelated institutions under them must
    # not collapse to the same registrable domain.
    assert registrable_domain("https://www.cs.ox.ac.uk/") == "ox.ac.uk"
    assert registrable_domain("https://eng.cam.ac.uk/") == "cam.ac.uk"
    assert registrable_domain("https://sydney.edu.au/") == "sydney.edu.au"
    # A bare public suffix has no registrable part.
    assert registrable_domain("https://ac.uk/") is None


def test_same_registrable_domain_distinguishes_uk_universities():
    # Two different UK universities share the ac.uk public suffix but must not be
    # treated as the same institution.
    assert same_registrable_domain("https://www.ox.ac.uk", "https://www.cam.ac.uk") is False
    assert same_registrable_domain("https://www.ox.ac.uk", "https://cs.ox.ac.uk/x") is True


def test_classify_search_match_unparented_confirmed_is_orphan():
    # Unparented, but its website confirms it belongs to this institution:
    # adopt the disconnected orphan instead of creating a duplicate.
    assert (
        classify_search_match("Q5", "Q1", set(), set(), institution_confirmed=True)
        == "exists_orphan"
    )


def test_model_for_provider_maps_each_provider(monkeypatch):
    from wikidata_discover import config

    monkeypatch.setattr(config, "LLM_MODEL", "gpt-test")
    monkeypatch.setattr(config, "ANTHROPIC_MODEL", "claude-test")
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-test")

    from wikidata_discover.discovery import model_for_provider

    assert model_for_provider("openai") == "gpt-test"
    assert model_for_provider("anthropic") == "claude-test"
    assert model_for_provider("gemini") == "gemini-test"
    # Unknown/None falls back to the configured default model.
    assert model_for_provider(None) == "gpt-test"
    assert model_for_provider("mystery") == "gpt-test"


def test_classify_search_match_direct_child_is_linked():
    assert classify_search_match("Q2", "Q1", {"Q2"}, None) == "exists_linked"


def test_classify_search_match_unparented_is_missing():
    # Exists but has no parents at all: no evidence it belongs to this parent, so
    # create a new unit rather than risk linking another institution's entity.
    assert classify_search_match("Q5", "Q1", set(), set()) == "missing"


def test_classify_search_match_already_under_this_parent_is_linked():
    assert classify_search_match("Q5", "Q1", set(), {"Q1"}) == "exists_linked"


def test_classify_search_match_parented_elsewhere_is_missing():
    # Already a child of a different unit (e.g. another school): do not re-parent.
    assert classify_search_match("Q5", "Q1", set(), {"Q9"}) == "missing"


def test_classify_search_match_joint_under_other_parent_is_orphan():
    # Cross-listed unit that already exists under one of its claimed joint
    # parents (Q9): add the current parent to it instead of duplicating.
    assert (
        classify_search_match("Q5", "Q1", set(), {"Q9"}, joint_parent_qids=["Q9"])
        == "exists_orphan"
    )


def test_classify_search_match_parented_elsewhere_not_joint_is_missing():
    # Parented under an unrelated unit and not claimed as joint: don't hijack it.
    assert (
        classify_search_match("Q5", "Q1", set(), {"Q9"}, joint_parent_qids=["Q8"])
        == "missing"
    )


def test_classify_search_match_unknown_parents_is_missing():
    # Parent lookup failed: fail safe rather than risk an incorrect link.
    assert classify_search_match("Q5", "Q1", set(), None) == "missing"


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


def test_extract_joint_parent_names_preserves_commas_in_labels():
    # A non-schema provider may return a single string; commas inside a label
    # must not be treated as a delimiter.
    division = {"parent_names": "College of Arts, Media and Design"}

    assert extract_joint_parent_names(division, "School of Engineering") == [
        "College of Arts, Media and Design",
    ]


def test_extract_joint_parent_names_splits_on_pipe_and_semicolon():
    division = {"joint_with": "School of Science; School of Engineering | School of Law"}

    assert extract_joint_parent_names(division, "School of Arts") == [
        "School of Science",
        "School of Engineering",
        "School of Law",
    ]


def test_downward_parent_qids_parses_and_is_best_effort(monkeypatch):
    from wikidata_discover import discovery as disc

    d = disc.Discovery.__new__(disc.Discovery)
    d._downward_parents_cache = {}
    monkeypatch.setattr(
        disc,
        "execute_sparql_bindings",
        lambda q: [
            {"parent": {"value": "http://www.wikidata.org/entity/Q9"}},
            {"parent": {"value": "http://www.wikidata.org/entity/Q8"}},
        ],
    )
    assert d._downward_parent_qids("Q5") == {"Q9", "Q8"}

    # A lookup failure yields an empty set rather than aborting the run.
    d2 = disc.Discovery.__new__(disc.Discovery)
    d2._downward_parents_cache = {}

    def boom(_q):
        raise RuntimeError("sparql down")

    monkeypatch.setattr(disc, "execute_sparql_bindings", boom)
    assert d2._downward_parent_qids("Q5") == set()


def test_downward_parent_query_covers_p355_p527_p199():
    from wikidata_discover.discovery import PARENT_DOWNWARD_SPARQL_TEMPLATE

    for predicate in ("P355", "P527", "P199"):
        assert f"wdt:{predicate}" in PARENT_DOWNWARD_SPARQL_TEMPLATE


def test_parent_resolution_choices_includes_downward_sibling_parents(monkeypatch):
    from wikidata_discover import discovery as disc

    d = disc.Discovery.__new__(disc.Discovery)
    d.university_qid = "Q0"

    # Current parent Q2 (a department) has no child-side parents, but school Q1
    # lists it via a downward edge (P355/P527/P199); Q1's children include
    # sibling department Q3, which must become an available choice for
    # joint-parent resolution.
    monkeypatch.setattr(d, "_existing_parent_qids", lambda qid: set())
    monkeypatch.setattr(d, "_downward_parent_qids", lambda qid: {"Q1"})
    children = {
        "Q0": [("Q1", "School of X")],
        "Q1": [("Q2", "Department A"), ("Q3", "Department B")],
    }
    monkeypatch.setattr(d, "get_existing_children", lambda qid: children.get(qid, []))

    choices = d.parent_resolution_choices("Q2", [("Q9", "Program P")])
    qids = {qid for qid, _ in choices}
    assert "Q3" in qids


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
