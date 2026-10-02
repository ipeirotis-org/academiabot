"""Lightweight ShEx-inspired validation of generated QuickStatements.

The canonical shapes live in ``shapes/academia.shex`` (one shape per hierarchy
level). Full ShEx validation needs an RDF graph and a ShEx engine; here we parse
QuickStatements blocks directly and enforce the same core constraints so a batch
can be checked before it is written/uploaded:

  - a CREATE block must declare a label (Len) and a P31 in an allowed org-unit
    class,
  - every generated unit must have at least one P749 (parent organization),
  - any P856 (official website) must be a URL.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from wikidata_discover.to_qs_wikidata import TYPE_MAP

SHEX_PATH = Path(__file__).parent / "shapes" / "academia.shex"

UNIVERSITY_CLASS = "Q3918"

# Classes a generated organizational unit may legitimately be an instance of:
# the export TYPE_MAP targets plus the university class and the extra
# program-level classes named in academia.shex.
ALLOWED_UNIT_CLASSES = set(TYPE_MAP.values()) | {
    UNIVERSITY_CLASS,
    "Q31855",   # higher-education institution (school/college)
    "Q1183543",  # academic department
    "Q4830453",  # program (business/academic)
    "Q1664727",  # institute
}

_PROPERTY_RE = re.compile(r"^P\d+$")


def load_shex() -> str:
    """Return the raw ShEx schema text (the human-readable source of truth)."""
    return SHEX_PATH.read_text()


@dataclass
class QSBlock:
    """One QuickStatements block: a CREATE entity or edits to an existing QID."""

    subject: str
    is_create: bool
    label: Optional[str]
    properties: Dict[str, List[str]] = field(default_factory=dict)
    lines: List[str] = field(default_factory=list)

    def describe(self) -> str:
        return self.label or self.subject


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    return value.replace('\\"', '"')


def _build_block(lines: List[str]) -> QSBlock:
    is_create = lines[0].strip() == "CREATE"
    subject = "CREATE" if is_create else lines[0].split("|", 1)[0].strip()
    label: Optional[str] = None
    properties: Dict[str, List[str]] = {}

    for line in lines:
        if line.strip() == "CREATE":
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        prop, value = parts[1], parts[2]
        if prop == "Len":
            label = _unquote(value)
        elif _PROPERTY_RE.match(prop):
            properties.setdefault(prop, []).append(value.strip())
        # Den/Aen and other label/description terms carry no shape constraint.

    return QSBlock(subject, is_create, label, properties, list(lines))


def parse_qs_blocks(lines: List[str]) -> List[QSBlock]:
    """Split QuickStatements lines into blocks on blank-line separators."""
    blocks: List[QSBlock] = []
    current: List[str] = []
    for line in lines:
        if line.strip() == "":
            if current:
                blocks.append(_build_block(current))
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(_build_block(current))
    return blocks


def _looks_like_url(value: str) -> bool:
    # Reuse the exporter's validity check so validation and export agree on what
    # counts as a usable P856 (a bare scheme like "https://not a url" is invalid).
    from wikidata_discover.to_qs_wikidata import is_valid_http_url

    return is_valid_http_url(_unquote(value))


def hard_violations(block: QSBlock) -> List[str]:
    """Return only violations that must invalidate a block (label, P31, P749).

    An optional bad P856 is not included here: it should not suppress an
    otherwise valid CREATE/link. See ``website_violations``.
    """
    violations: List[str] = []

    if block.is_create:
        if not block.label:
            violations.append("missing label (Len)")
        p31 = block.properties.get("P31", [])
        if not p31:
            violations.append("missing P31 (instance of)")
        else:
            bad = [c for c in p31 if c not in ALLOWED_UNIT_CLASSES]
            if bad:
                violations.append(f"P31 value(s) not an allowed org-unit class: {bad}")

    if not block.properties.get("P749"):
        violations.append("missing P749 (parent organization)")

    return violations


def website_violations(block: QSBlock) -> List[str]:
    """Return soft violations for malformed optional P856 websites."""
    return [
        f"P856 website is not a URL: {website}"
        for website in block.properties.get("P856", [])
        if not _looks_like_url(website)
    ]


def validate_block(block: QSBlock) -> List[str]:
    """Return all constraint violations for a block (hard + soft; empty if clean)."""
    return hard_violations(block) + website_violations(block)


def _is_bad_website_line(line: str) -> bool:
    parts = line.split("|")
    return len(parts) >= 3 and parts[1] == "P856" and not _looks_like_url(parts[2])


def strip_bad_website_lines(lines: List[str]) -> List[str]:
    """Drop only the malformed P856 lines, keeping the rest of the block intact."""
    return [line for line in lines if not _is_bad_website_line(line)]


@dataclass
class ValidationReport:
    total_blocks: int
    valid_blocks: int
    invalid: List[Tuple[int, str, List[str]]]  # (block_index, describe, violations)

    @property
    def ok(self) -> bool:
        return not self.invalid


def validate_quickstatements(lines: List[str]) -> ValidationReport:
    """Validate every block and return a report of violations."""
    blocks = parse_qs_blocks(lines)
    invalid: List[Tuple[int, str, List[str]]] = []
    for index, block in enumerate(blocks):
        violations = validate_block(block)
        if violations:
            invalid.append((index, block.describe(), violations))
    return ValidationReport(
        total_blocks=len(blocks),
        valid_blocks=len(blocks) - len(invalid),
        invalid=invalid,
    )


def filter_valid_quickstatements(
    lines: List[str],
) -> Tuple[List[str], ValidationReport]:
    """Return only the lines of valid blocks, plus the validation report.

    Invalid blocks are dropped so a malformed statement never reaches Wikidata;
    the report lets callers surface what was dropped and why.
    """
    blocks = parse_qs_blocks(lines)
    valid_lines: List[str] = []
    invalid: List[Tuple[int, str, List[str]]] = []
    for index, block in enumerate(blocks):
        hard = hard_violations(block)
        if hard:
            invalid.append((index, block.describe(), hard))
            continue
        # Keep the block, but drop any malformed optional P856 line so a bad
        # website does not suppress an otherwise valid unit.
        valid_lines.extend(strip_bad_website_lines(block.lines))
        valid_lines.append("")
    report = ValidationReport(
        total_blocks=len(blocks),
        valid_blocks=len(blocks) - len(invalid),
        invalid=invalid,
    )
    return valid_lines, report
