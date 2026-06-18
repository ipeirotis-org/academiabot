"""Unit tests for LLM prompt helpers."""

from wikidata_discover.llm_helpers import _example_unit_type


def test_example_unit_type_varies_by_level():
    # The prompt example should match the level being extracted so a provider
    # does not anchor on a top-level type while extracting deeper units.
    assert _example_unit_type(1) == "school"
    assert _example_unit_type(2) == "department"
    assert _example_unit_type(3) == "program"
    assert _example_unit_type(5) == "program"
