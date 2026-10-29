# School status report: run us-tier1

Generated 2026-10-29 from BigQuery and the Wikidata snapshots. No Wikidata query was sent.

## What the data is

- Run us-tier1 ran on 2026-10-03. It asked an LLM for the schools and colleges of each of
  1,519 U.S. universities. This is the school level only, not departments.
- 9,126 of 9,190 proposed units (99.3%) came from OpenAI.
- Each proposed unit was compared with Wikidata. "Linked" means Wikidata already has the
  unit under the university. "Orphan" means Wikidata has the unit, but it is not linked to
  the university. "Missing" means the search found no item for it.
- "Units on Wikidata" comes from the snapshot of each university, taken 2026-10-20 and 2026-10-21.

## Scope rule (Panos, 2026-10-29)

- In scope: schools, campuses, departments, and degree programs (online programs included).
- Out of scope: offices, for example the registrar, student services, career services, and
  administrative units.
- Each unit gets a type_group from the words of its unit_type: school, campus, department,
  program, office, or other. "Other" (centers, institutes, libraries, and similar) needs a
  person to check it.

## Headline counts

- Universities with proposed units: 1,518 of 1,519.
- Proposed units: 9,190. Linked: 2,327 (25.3%). Orphan:
  450 (4.9%). Missing: 6,413 (69.8%).
  Unresolved: 0.
- Universities with no linked unit: 855.
- In scope: 8,755 (95.3%): 2,279 linked, 435 orphan, 6,041 missing.
- Out of scope (offices): 154 (1.7%): 4 linked, 1 orphan, 149 missing.
- To check (other): 281 (3.1%): 44 linked, 14 orphan, 223 missing.
- By type_group:
  - school: 7,141 (77.7%): 2,129 linked, 394 orphan, 4,618 missing
  - campus: 754 (8.2%): 144 linked, 20 orphan, 590 missing
  - department: 540 (5.9%): 1 linked, 16 orphan, 523 missing
  - program: 320 (3.5%): 5 linked, 5 orphan, 310 missing
  - office: 154 (1.7%): 4 linked, 1 orphan, 149 missing
  - other: 281 (3.1%): 44 linked, 14 orphan, 223 missing

## Week 3 baseline, recounted under the scope rule

Anya checked 20 random "missing" units by hand in week 3. Under Panos's rule:

- 1 is already on Wikidata: Penn State's graduate school (Q15265896).
- 2 are out of scope: an office of the registrar and a student services unit.
- 17 are in scope and missing: 12 schools, 1 campus, 3 degree programs, and 1 department.

The week 3 count was 6 "not schools". Under the new rule, 4 of those 6 are in scope: a campus
and 3 programs. With 20 units the true rates are uncertain: about 1% to 24% already exist, 3%
to 30% are out of scope, and 64% to 95% are in scope and missing.

## Caveats

1. "Missing" is too high. The baseline above found 1 of 20 already on Wikidata, and the search
   that made "missing" checks only the start of each label.
2. "Orphan" matches are not reliable. In Anya's week 1 spot check, 3 of 4 orphans were matched
   to the wrong Wikidata item.
3. Linked rows have no matched QID. The run did not save which item a linked unit matched, so
   those cells are empty in the workbook.
4. Not included: Kensington College (Q16998232). Its run failed, so it has no proposed units.
5. type_group comes from the words the LLM gave as unit_type, not from the unit's own page. In
   the 20 baseline rows it gave the right scope in all 20, but the right group in only 18.
6. "Units on Wikidata" counts every item linked to the university, not only schools. Endowed
   chairs, research centers, and departments are included, so it is not comparable with the
   number of proposed units.

## Files

- `school_status_report.xlsx`: sheets Summary, By university, and All schools.
- Rebuild both files: `python -m wikidata_discover.school_status_report`.
