# TODO: Wikidata — AcademiaBot

> **Goal**: Build a reliable tool for discovering and cataloging missing university organizational units in Wikidata.
>
> Last reviewed: 2026-02-15.

---

## Bugs / Fixes

- [ ] Fix model version mismatch: `config.py` hardcodes `gpt-5` but README documents `gpt-4o`
  - Decide on default model; update both config and README to match
  - Verify the OpenAI API call signature works with chosen model (`client.responses.create()` may be model-specific)

- [ ] Fix relative imports in `misc_scripts/hierarchy.py`
  - Uses `from sparql_helpers import ...` — should be `from wikidata_discover.sparql_helpers import ...`

---

## High Priority

- [ ] Add rate limiting for Wikidata Search API calls
  - SPARQL has 0.3s polite sleep but `wikidata_api.quick_wd_search()` has none
  - Risk of being blocked by Wikidata if running large batches

- [ ] Add error handling for LLM JSON schema validation
  - `llm_helpers.py` expects strict JSON with `name`, `unit_type`, `city`, `state`, `website`
  - LLM may return incomplete or malformed data; currently no fallback

- [ ] Run discovery for a batch of major U.S. universities and review results
  - Test with NYU (Q49210), Columbia (Q49088), MIT (Q49108), Stanford (Q41506)
  - Validate output quality and identify false positives/negatives

---

## Enhancements

- [ ] Expand beyond U.S. universities
  - Current SPARQL query filters P17 = Q30 (United States)
  - Add support for other countries or make country configurable via CLI

- [ ] Add batch discovery mode
  - Currently processes one university at a time
  - Add `discover --batch` to process all universities from harvested JSON

- [ ] Generate QuickStatements output for batch Wikidata edits
  - Current output is CSV; could directly generate QS V1 commands for import
  - Would streamline the Wikidata editing workflow

- [ ] Add logging framework
  - Currently all output via `rich.console`
  - Add Python `logging` for long operations and debugging

---

## Documentation

- [ ] Update README with accurate setup instructions
  - Document `.env` file requirements
  - Add example output screenshots
  - Clarify which OpenAI models are supported

---

## Future / Low Priority

- [ ] Add tests for SPARQL query parsing and LLM matching logic
- [ ] Handle SIGINT gracefully during long SPARQL queries
- [ ] Cache LLM responses to avoid redundant API calls on re-runs

---

_Last updated: 2026-02-15_
