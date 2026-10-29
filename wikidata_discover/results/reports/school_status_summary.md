# School status report: run us-tier1

Generated 2026-10-29 from BigQuery and the Wikidata snapshots. No Wikidata query was sent.

## What the data is

- Run us-tier1 ran on 2026-10-03. It asked an LLM for the schools and colleges of each of
  1,519 U.S. universities. This is the school level only, not departments.
- 9,126 of 9,190 proposed schools (99.3%) came from OpenAI.
- Each proposed school was compared with Wikidata. "Linked" means Wikidata already has the
  school under the university. "Orphan" means Wikidata has the school, but it is not linked to
  the university. "Missing" means the search found no item for it.
- "Units on Wikidata" comes from the snapshot of each university, taken 2026-10-20 and 2026-10-21.

## Headline counts

- Universities with proposed schools: 1,518 of 1,519.
- Proposed schools: 9,190.
- Linked: 2,327 (25.3%). Orphan: 450 (4.9%).
  Missing: 6,413 (69.8%). Unresolved: 0.
- Universities with no linked school: 855.
- Most common unit types, as the AI gave them: college (3,404), school (2,943), campus (700), department (405), academic division (260), division (240).
- 1,216 proposed schools have a unit type such as campus, office, program, or
  service. The workbook flags them as "likely not a school".

## Caveats

1. "Missing" is too high. A hand check of 20 random "missing" units found that 1 is already on
   Wikidata (Penn State's graduate school, Q15265896) and 6 are not schools. With 20 units, the
   true rates are uncertain: about 1% to 24% already exist, and 15% to 52% are not schools.
2. "Orphan" matches are not reliable. In Anya's week 1 spot check, 3 of 4 orphans were matched
   to the wrong Wikidata item.
3. Linked rows have no matched QID. The run did not save which item a linked school matched, so
   those cells are empty in the workbook.
4. Not included: Kensington College (Q16998232). Its run failed, so it has no proposed schools.
5. "Units on Wikidata" counts every item linked to the university, not only schools. Endowed
   chairs, research centers, and departments are included, so it is not comparable with the
   number of proposed schools.

## Files

- `school_status_report.xlsx`: sheets Summary, By university, and All schools.
- Rebuild both files: `python -m wikidata_discover.school_status_report`.
