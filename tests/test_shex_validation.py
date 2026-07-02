"""Unit tests for ShEx-inspired QuickStatements validation."""

from wikidata_discover.shex_validation import (
    filter_valid_quickstatements,
    load_shex,
    parse_qs_blocks,
    validate_block,
    validate_quickstatements,
)


VALID_CREATE = [
    "CREATE",
    'LAST|Len|"School of Law"',
    'LAST|Den|"school within Example University"',
    "LAST|P31|Q31855",
    'LAST|P856|"https://law.example.edu"',
    "LAST|P749|Q1",
    "",
]


def test_valid_create_block_passes():
    report = validate_quickstatements(VALID_CREATE)
    assert report.ok
    assert report.valid_blocks == 1


def test_missing_p749_is_flagged():
    lines = [
        "CREATE",
        'LAST|Len|"Orphan School"',
        "LAST|P31|Q31855",
        "",
    ]
    block = parse_qs_blocks(lines)[0]
    violations = validate_block(block)
    assert any("P749" in v for v in violations)


def test_missing_label_is_flagged():
    lines = ["CREATE", "LAST|P31|Q31855", "LAST|P749|Q1", ""]
    block = parse_qs_blocks(lines)[0]
    assert any("label" in v for v in validate_block(block))


def test_disallowed_p31_is_flagged():
    lines = ["CREATE", 'LAST|Len|"Weird"', "LAST|P31|Q5", "LAST|P749|Q1", ""]
    block = parse_qs_blocks(lines)[0]
    assert any("P31" in v for v in validate_block(block))


def test_bad_website_is_flagged():
    lines = [
        "CREATE",
        'LAST|Len|"School"',
        "LAST|P31|Q31855",
        "LAST|P749|Q1",
        'LAST|P856|"not-a-url"',
        "",
    ]
    block = parse_qs_blocks(lines)[0]
    assert any("website" in v for v in validate_block(block))


def test_existing_entity_link_block_passes():
    # Adding P749 to an existing QID needs no label/P31.
    lines = ["Q42|P749|Q1", ""]
    report = validate_quickstatements(lines)
    assert report.ok


def test_filter_drops_only_invalid_blocks():
    lines = VALID_CREATE + [
        "CREATE",
        'LAST|Len|"No Parent School"',
        "LAST|P31|Q31855",
        "",
    ]
    valid_lines, report = filter_valid_quickstatements(lines)
    assert report.total_blocks == 2
    assert report.valid_blocks == 1
    text = "\n".join(valid_lines)
    assert "School of Law" in text
    assert "No Parent School" not in text


def test_shex_file_loads_and_defines_shapes():
    schema = load_shex()
    for shape in ("<University>", "<School>", "<Department>", "<Program>"):
        assert shape in schema
