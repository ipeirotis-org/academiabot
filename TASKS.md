# TASKS.md

> **Mission**: Make Wikidata the definitive, queryable source for the full organizational
> hierarchy of every university worldwide, down to departments and faculty affiliations.
>
> **Current phase**: Phase 1 (Stabilize and fix existing code)
>
> Last updated: 2026-02-26

---

## Phase 0: Bugs and tech debt (do first)

- [x] **Fix LLM model config**: `LLM_MODEL` now read from `.env` with default `gpt-4o`. README updated.
- [x] **Fix CLI --llm override**: `cli.py` now imports `wikidata_discover.config` correctly.
- [x] **Fix misc_scripts imports**: Scripts were already standalone; added deprecation notices pointing to `wikidata_discover.hierarchy`.
- [x] **Add rate limiting to Wikidata Search API**: `quick_wd_search()` now has 0.3s polite delay.
- [x] **Add retry/backoff**: SPARQL and Wikidata API calls wrapped with `tenacity` exponential backoff (3 attempts).
- [x] **Remove hardcoded cap in QuickStatements export**: `export_quickstatements()` now accepts optional `max_items` param; defaults to all.
- [x] **Add basic error handling for LLM responses**: `extract_divisions()` now logs raw response on parse failure, retries once, then returns `[]` gracefully.
- [x] **Add `.env.example`** with all required/optional env vars documented.

---

## Phase 1: Stabilize and validate the discover pipeline

- [x] **Run pilot on 10 diverse universities**: NYU (Q49210), Columbia (Q49088), MIT (Q49108), Stanford (Q41506), UC Berkeley (Q168756), U Michigan (Q230492), Howard (Q2089472), Caltech (Q161562), U Texas Austin (Q49213), CUNY (Q762266). Document precision/recall of LLM extraction.
- [x] **Add validation/QA reporting**: After discovery, output a summary report: how many candidates found, how many matched, how many orphans, how many truly missing. Store in `results/` as JSON.
- [x] **Improve fuzzy matching**: Current `is_fuzzy_match` in `discovery.py` has aggressive partial matching (threshold 70 with partial_ratio can cause false positives). Tune thresholds, add unit tests for edge cases like "Stern School of Business" vs "Leonard N. Stern School of Business".
- [x] **Add caching for LLM responses**: Cache `extract_divisions` results keyed by (QID, model_version) to avoid redundant API calls during iteration. Use a JSON file in `results/cache/`.
- [x] **Add proper logging**: Replace ad-hoc `console.print` debug output with Python `logging` module. Keep `rich` for user-facing tables/progress only.

---

## Phase 2: Recursive depth (schools -> departments -> programs)

The current pipeline only discovers **top-level units** (schools/colleges) under a university. This phase extends it to work recursively.

- [ ] **Generalize `extract_divisions` to work at any level**: The LLM prompt currently says "top-level academic or administrative unit". Make it configurable: given any entity (school, department), extract its sub-units.
- [ ] **Add recursive discovery mode**: `discover --recursive Q49210` should:
  1. Discover schools/colleges under the university
  2. For each school (existing or newly found), discover departments
  3. For each department, discover programs/labs/centers
  4. Output the full tree
- [ ] **Add depth parameter**: `discover --depth 2 Q49210` to control how many levels deep to go (default 1 = schools only, 2 = schools+departments, 3 = full tree).
- [ ] **Create entity type detection**: When discovering sub-units, the LLM should classify each as school/department/program/lab/center and assign the correct P31 value. Update `to_qs_wikidata.py` TYPE_MAP accordingly.
- [ ] **Handle cross-listed/joint units**: Some departments belong to multiple schools. Detect and model with multiple P749 statements + qualifiers.

---

## Phase 3: Batch processing and automation

- [ ] **Add batch discovery mode**: `discover --batch` processes all universities from `universities_us.json`. Add progress tracking, resume capability (skip already-processed QIDs), and summary report.
- [ ] **Add batch QuickStatements generation**: Aggregate all missing entities across universities into a single uploadable QS batch, organized by hierarchy level (schools first, then departments).
- [ ] **Implement ShEx validation schema**: Write Shape Expressions (one shape per entity level) and store in the repo. Validate generated QS statements against the schema before export.
- [ ] **Add IPEDS reconciliation**: Download IPEDS CSV, match against Wikidata universities by IPEDS ID (P1771). Flag institutions missing from Wikidata entirely.
- [ ] **Add diff/update mode**: Compare current Wikidata state against last run. Only generate QS for genuinely new entities (not already uploaded in a previous batch).
- [ ] **Expand beyond U.S.**: Make country configurable. Add support for international institution identifiers (UK UCAS codes, EU ETER IDs, etc.).

---

## Phase 4: Faculty and researcher linking

- [ ] **Design faculty data model**: Researcher entity linked via P108 (employer) -> department QID, with P39 (position held) as qualifier. Add P1960 (Google Scholar author ID), P496 (ORCID).
- [ ] **Build faculty discovery pipeline**: Given a department QID:
  1. LLM + web search to find faculty listing page
  2. Extract faculty names, titles, and profile URLs
  3. Match against existing Wikidata person entities
  4. For unmatched: check Google Scholar / ORCID for existing profiles
  5. Output CSV of faculty to link or create
- [ ] **Google Scholar integration**: Given a faculty name + institution, find their Google Scholar profile. Extract: author ID, h-index, citation count, research interests.
- [ ] **Salary data integration** (U.S. public universities): Parse public salary databases (state-level data) and link to faculty entities. Add as P3457 (salary) with qualifiers for fiscal year.
- [ ] **Bulk faculty upload**: Generate QS statements for faculty-department links. Run only after department hierarchy is stable (Phase 3 complete).

---

## Phase 5: Governance, maintenance, and community

- [ ] **Scheduled IPEDS diff**: GitHub Action that quarterly compares IPEDS list vs Wikidata, flags new institutions.
- [ ] **Orphan detection dashboard**: Weekly SPARQL query for departments without parents, schools with broken links. Output as GitHub issue or report.
- [ ] **WikiProject Universities engagement**: Document the data model, post on WikiProject talk page, get community review before large batch uploads.
- [ ] **Bot approval**: If automating Wikidata edits, apply for bot flag per Wikidata bot policy. Document edit patterns and rate limits.
- [ ] **Monitoring and rollback**: Keep per-batch logs of all QS uploads. Build a rollback script that can undo a batch if issues are found.

---

## Infrastructure and tooling (ongoing)

- [ ] **Add tests**: Unit tests for `normalize_name`, `is_fuzzy_match`, SPARQL query construction, QS export format. Integration tests with mock LLM responses.
- [ ] **Add pyproject.toml / setup.py**: Proper Python packaging so the CLI can be installed as `academiabot`.
- [ ] **CI/CD**: GitHub Actions for linting (ruff/flake8), tests, and type checking (mypy).
- [ ] **Support multiple LLM providers**: Abstract `llm_helpers.py` to support both OpenAI and Anthropic Claude APIs. Make provider configurable via `.env`.
- [ ] **Add Wikidata write capability**: Currently only generates QS files. Add direct Wikidata API editing via `wikibaseintegrator` or `pywikibot` for approved bot operations.
- [ ] **Structured output validation**: Use pydantic models for LLM response schemas instead of raw dicts. Validate before processing.

---

## Useful SPARQL queries (reference)

```sparql
# Orphan departments (no parent)
SELECT ?dept ?deptLabel WHERE {
  ?dept wdt:P31 wd:Q1183543 .
  FILTER NOT EXISTS { ?dept wdt:P749 ?parent }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}

# University -> school -> department counts
SELECT ?univ ?univLabel (COUNT(DISTINCT ?school) AS ?nSchool) (COUNT(DISTINCT ?dept) AS ?nDept)
WHERE {
  ?univ wdt:P31 wd:Q3918 ; wdt:P17 wd:Q30 .
  OPTIONAL { ?school wdt:P749 ?univ ; wdt:P31 wd:Q31855 .
    OPTIONAL { ?dept wdt:P749 ?school ; wdt:P31 wd:Q1183543 . }
  }
}
GROUP BY ?univ ?univLabel
ORDER BY DESC(?nDept)

# All children of a specific university
SELECT ?child ?childLabel ?childTypeLabel WHERE {
  ?child wdt:P749 wd:Q49210 .
  OPTIONAL { ?child wdt:P31 ?childType . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
```

---

_Last updated: 2026-02-26_
