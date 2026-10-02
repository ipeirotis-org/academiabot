# Background and design decisions

Where this project came from, and the decisions we made early that still hold. This file
replaces two older Google Docs ("Citation Analysis Project" and "Scholar+Wikidata Project:
Notes and Tasks"), which are now deprecated. Everything from them that still matters is here.

## Origins

The project began as a system for comparing researchers' Google Scholar profiles against
peer benchmarks (all faculty, top-20 schools, discipline-specific). To do that well we
needed to know which department each researcher belongs to, which needed a clean hierarchy
of universities, schools, and departments. Building that hierarchy, in Wikidata so that
everyone can use it, became the project.

The earlier effort collected its hierarchy through paid crowdsourcing. Figures recorded at
the time:

| Item | Value |
|---|---|
| Spend on crowd tasks since 2015 | about $28,500 |
| Organizations collected | about 16,000 |
| Author profiles collected | about 100,000 |
| Amortized cost per entity, all tasks | about 20 cents |

That dataset still exists, in BigQuery:

```
nyu-datasets.academiabot.organization
```

The project's service account has read access to it. See "BigQuery access" in AGENTS.md
for how to query it from a Claude Code session (use the Python client, not the bq tool).

**What is in it.** One table of about 16,000 organizations, each with an integer id, a
name, a parent id, a location, a website, the URL of the page that listed its children or
faculty, and (for 15 rows only) a Google Scholar organization id. The hierarchy is up to
five levels deep:

| Depth | Rows | Typically |
|---|---|---|
| 0 | 170 | universities |
| 1 | 1,189 | schools and colleges |
| 2 | 6,982 | departments |
| 3 | 5,380 | divisions, centers, programs |
| 4 | 1,990 | sub-units of those |

**What NYU looks like in it.** NYU (id 32) has 24 children. Some are real schools (Stern,
Courant, Steinhardt, Tisch, Law, Tandon, Wagner, Dentistry, Nursing, Medicine, Arts and
Sciences, School of Professional Studies). Others are administrative rows that are not
academic units (Office of the Provost, President, University Life, Libraries), and some
are abbreviations (TSOA for Tisch, IFA, ISAW, Silver SSW). Stern has 9 children, of which
7 are departments, 1 is a program, and 1 is a duplicate row for the school itself.
Courant has only 3 children. Steinhardt has 11, which looks close to complete.

**How to use it.** It is a good starting list for a school's departments and for finding
the faculty-listing page (the children source URL column is mostly faculty pages). It is
not a ground truth: it was collected by crowd workers over several years starting in
2015, it mixes administrative and academic units, names are sometimes abbreviations, and
most website URLs are plain http and may have moved. Treat every row as a candidate that
still needs a person to confirm it against the school's current website.

## The human task design we started from

The crowdsourcing design broke the work into four small human tasks. The LLM pipeline now
does most of this automatically, but the decomposition is still the right way to think about
the human review step (TASKS.md, Milestone 6), because each task has a clear yes/no answer.

1. **Enrich an entity.** Given a unit, record its name, location, website, parent, and
   identifiers (Wikidata QID, Google Scholar organization ID).
2. **Find the children page.** Given a unit, provide the URL that lists its sub-units
   (a university's schools page, a school's departments page), or mark that it has none.
   If it has none, provide the URL of its faculty list instead.
3. **Check completeness.** Show the list of children we have and the URL from task 2.
   Is the list complete? Yes or no.
4. **Add the missing children.** If no, name the missing ones. Each new name goes back to
   task 1 with the correct parent.

In the review sheet, the "source URL" column is task 2, the verdict column is task 3, and
the "fix" verdict with a corrected value is task 4.

## Decisions that still hold

**P749 (parent organization) is the primary link, not P361 (part of).** The earliest notes
used P361 because it reads naturally ("Harvard College is part of Harvard"). We switched to
P749 because it is the recommended inverse of P355 (has subsidiary), it is what Wikidata's
own organization modeling uses, and it makes the whole chain traversable with one SPARQL
property path (`?dept wdt:P749+ ?univ`). P361 may be added as a supplementary statement.
Joint units get a second P749 with normal rank and qualifiers.

**Minimum statement set for a new item.** Label, English description, P31 (instance of),
P749 (parent), P17 (country), and P856 (website) if known. Universities also get P1771
(IPEDS ID). Nothing less, so that every item we create is identifiable and attached.

**One QuickStatements file per hierarchy level.** Schools in one batch, departments in
another. If a batch has to be rolled back, the damage is contained to one level.

**Pilot before scale.** Run the full loop on a handful of diverse universities before any
batch processing. This was done for 12 universities at the school level; see
`wikidata_discover/eval/`.

**Faculty come last.** Researcher-to-department links (P108 employer, P39 position held)
are uploaded only after the department hierarchy is stable.

## Reference sources and identifiers

Sources worth knowing about when building ground truth or reconciling:

- **IPEDS** (NCES): every accredited U.S. institution with a unit ID. Wikidata property P1771.
- **ROR** (Research Organization Registry, ror.org): open registry of research organizations,
  successor to GRID. Wikidata property P6782. Useful for non-U.S. institutions and some schools.
- **Accreditation bodies** (AACSB for business, ABET for engineering, LCME for medicine):
  authoritative rosters of accredited schools and programs, good for confirming names.
- **Wikidata EntitySchemas** E44 and E45 describe how universities and their parts are
  expected to be modeled. Check them before proposing new statement patterns.
- **Researcher identifiers** to collect when faculty linking begins: ORCID (P496), Google
  Scholar author ID (P1960), DBLP (P2456), and where relevant SSRN and PubMed author IDs.

## Community

Before any large upload, post the data model and a sample batch on the WikiProject
Universities talk page and ask for review. Wikidata editors will revert what they do not
understand, and early goodwill is cheap.
