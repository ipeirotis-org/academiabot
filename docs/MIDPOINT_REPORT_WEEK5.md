# Midpoint report: the hierarchy file (Anya, week 5)

Date: 2026-11-18. University: New York University (NYU, Q49210). Branch `anya-week-5-6`.

## What exists now

`discover Q49210 --depth 2` writes one hierarchy file for a university:
`results/hierarchy/Q49210.json`, and a copy in the bucket under the run folder. The same
nodes go into the BigQuery table `nodes`. A node is one unit: the university, a school, a
campus, a department, a program, or a center.

- Level 1 is the units of the university. Level 2 is the units of each school and campus.
- Every unit that Wikidata has under NYU, and that the modeling rules put in scope, is in
  the file, also when the AI did not name it.
- Each node has a node id from a register in the bucket
  (`gs://academiabot/hierarchy/Q49210/ids.json`). A unit keeps its id when it is renamed,
  moved, or given a QID.
- A study-away site (NYU Florence, NYU Paris, and 11 more) is kept, with the note
  "children not looked up (study-away site)".
- A unit that the Wikidata snapshot does not have is "uncertain", with the reason
  "not in the snapshot; full search in week 6".

## Runs

| Run | Code | What | Nodes | Time |
|---|---|---|---|---|
| `discover-20261118T021635Z-77be63` | `90e2552` | Pilot: level 2 for Stern, Courant, Steinhardt, NYU Shanghai | 182 | 10 min |
| `discover-20261118T033819Z-768cb9` | `e41c83b` | Full NYU run | 624 | 27 min |
| `discover-20261118T083854Z-51d762` | `57433ff` | Second run (check 5, first try) | 624 | 24 min |
| `discover-20261118T090518Z-e73506` | `8425ed6` | After the certificate fix | 624 | 22 min |
| `discover-20261118T092730Z-283d9b` | `8425ed6` | Second run at the same code (check 5) | 624 | 12 min |

## Cost and time per university

The numbers are for the full NYU run. NYU is a large university: 25 schools and campuses
were looked up at level 2.

| Item | Value |
|---|---|
| Time | 27 minutes. 14 minutes were AI calls; the rest was Wikidata queries and page fetches |
| AI calls | 21 fresh Anthropic calls (claude-opus-4-6 with web search) and 5 cached answers |
| Tokens | 1,176,754 input and 62,517 output |
| Web searches | 96 |
| Cost as run | about $8.40: $7.45 for tokens and $0.96 for searches |
| Cost with no cache | about $10.50 (the 5 cached answers cost about $2 when they were made) |
| Cost of a rerun | about $0: every answer comes from the cache |

Prices: $5 per million input tokens and $25 per million output tokens for claude-opus-4-6,
and $10 per 1,000 web searches.

## Share of units with a saved page

A unit has a saved page when its source page was fetched with outcome "ok" and saved in
the run log.

| | All nodes | Named by the AI | Only on Wikidata |
|---|---|---|---|
| Before the certificate fix (`768cb9`) | 322 of 624 (51.6%) | 315 of 569 | 7 of 55 |
| After the certificate fix (`e73506`) | 338 of 624 (54.2%) | 330 of 569 | 8 of 55 |

Why the other units have no saved page (after the fix):

- 241 nodes cite a page that the server refused. Most are nyu.edu pages that answer HTTP 405
  to the fetcher (62 pages). The fetcher does not pose as a browser, so this stays.
- 42 nodes are only on Wikidata and have no website there (no P856). The page search for
  them was not built (a known limitation, decided on 2026-11-18).
- 3 nodes cite a page that failed for another reason (a timeout, a closed connection).

The certificate fix: five nyu.edu hosts sent only their own certificate. The fetcher now
also trusts the missing intermediate certificate, "InCommon RSA OV SSL CA 3", from its
official source. Certificate checking stays on. Tests check that a trusted root signed it.

## Checks (TASKS.md week 5)

1. Run `discover --depth 2 Q49210`. Done: the runs above.
2. Ten departments at random (seed 20261119) from Stern, Courant, and Steinhardt, checked
   by hand by Anya:
   - School: 10 of 10 right.
   - Type: 10 of 10 right (all are departments).
   - QID: 6 of 10 right. Finance (Q128602068) is right. Nutrition and Food Studies,
     Occupational Therapy, Art and Art Professions, Technology, Operations, and Statistics,
     and Management and Organizations are rightly empty: a search of Wikidata found no item.
   - 4 of 10 wrong: the QID is empty, but Wikidata has an item attached directly to NYU, not
     to the school: Marketing (Q101027500), Mathematics (Q101027501), Applied Psychology
     (Q101027484), and Teaching and Learning (Q101027519). Week 5 missed them because it
     matches a unit only against the snapshot items under its own school.
3. Every node has a source URL or a flag. Passed in every run.
4. Every in-scope school in the snapshot is in the file, with its departments looked up.
   Passed: 27 of 27 are in the file. 25 were looked up; NYU Paris and NYU Florence are
   study-away sites.
5. A second run keeps every node id. Passed: 624 of 624 between `e73506` and `283d9b`.
   The first try (`51d762`) kept 622 of 624: the Furman Center is in the tree twice with
   one QID (under Wagner and under Law). The register now lets the parent decide, and both
   nodes got their first ids back. Two more fixes came from this check: the node version
   no longer changes when only the evidence row ids change, or when only the raw HTML
   changes (nyu.edu adds a new encrypted token to every page).

## Oddities for weeks 6 and 7

- A department under two schools: the Furman Center (Q594306) is under Wagner (P749) and
  under the Law School (P355). It is two nodes today; week 7 makes it one node with two
  parents.
- A unit with two parents in two universities: NYU Shanghai is under NYU and under East
  China Normal University. Both are kept in `parent_ids`.
- Departments attached directly to NYU instead of their school: 33 Wikidata items, such as
  "New York University Department of Accounting", hang directly under NYU, not under their
  school. The AI puts the same departments under Stern, Steinhardt, or Courant. Week 5
  matches only under the school, so it misses them: 4 of the 10 departments in check 2.
  13 such pairs are listed in the week 5 notes; week 6 searches and merges them.
- Two departments with the same name: Stern's Department of Economics is not the Arts and
  Science Department of Economics (Q101027493). Shuo's ground truth agrees.
- Same name, other campus: NYU Shanghai's "Arts and Sciences" and "Center for Data Science"
  share names with NYU New York units. They are different units.
- Not units: Wikidata lists law journals and the Root-Tilden Scholarship under the Law
  School. They are left out of the file, with the reason.
- Wrong types on Wikidata: "New York University Department of Accounting" has the type
  "business school". The code reads the label before the type.
- NYU's Faculty of Arts and Science (Q107684046) contains the College and the Graduate School
  of Arts and Science, which also sit directly under NYU, so these schools and their
  departments appear two or three times in the tree.

## Known limitations

- Units only on Wikidata without a P856 website have no saved page (42 nodes).
- Rank and qualifiers of each parent link are week 7. Today a link is only "present",
  "missing", or "not applicable".
- Countries are names, not QIDs, for units the AI named.
- The check across registers (one unit in two universities) is not built; the run records
  the generation of `cross_register.json` (0: it does not exist yet).
