# Run log schema

The run log records every `discover` and batch run in BigQuery, dataset
`wikidata-academia.academiabot`. It has 4 tables. Large texts (prompts, raw LLM responses,
page snapshots) are in the bucket `gs://academiabot/runs/<run_id>/`, and each row keeps the
path. The code is `wikidata_discover/run_log.py`. Anya built it in week 2 (2026-10-14).
Week 3 added the columns `legacy` and `source_file` to `llm_calls` and `candidates`. Rows
written before that have `legacy` false.

Types are BigQuery types. "List" means a REPEATED column. A column marked "JSON" holds JSON
text in a STRING column. Read it with `JSON_VALUE(config, '$.depth')`.

## runs

One row is one invocation of a command. A batch run in the cloud keeps one run_id across
many scheduled invocations, so it has one row per invocation.

| Column | Type | Meaning |
|---|---|---|
| run_id | STRING, required | Name of the run, for example `discover-20261014T164359Z-942cfd` |
| invocation_id | STRING, required | Id of this invocation inside the run |
| who | STRING | Person or service account that started the run |
| git_commit | STRING | Code version. `<sha>-dirty-<hash>` means uncommitted changes |
| command | STRING | `discover` or `batch` |
| args | STRING, JSON | The exact command arguments |
| qids | STRING, list | Universities the command was asked to process |
| config | STRING, JSON | Providers with a key, models, depth, and where each API key came from (`env`, `dotenv`, `secret_manager`). Never the key itself |
| host | STRING | `local`, or the Cloud Function name |
| started_at, ended_at | TIMESTAMP | Start and end of the invocation (UTC) |
| outcome | STRING | `ok`, `failed`, or `stopped_for_time` (batch only) |
| summary | STRING, JSON | Counts and the QIDs finished |
| source_patch_path | STRING | Saved patch of uncommitted code, so the exact code can be rebuilt |
| storage_problems | STRING, JSON | Bucket or BigQuery error, if one happened. Empty when all writes worked |

## llm_calls

One row is one request to an LLM, or one answer read from the cache. A retry is a new
row, and a failed attempt keeps its row with `error`.

| Column | Type | Meaning |
|---|---|---|
| llm_call_id | STRING, required | Id of this call |
| run_id, invocation_id | STRING, required | The run and invocation that made the call |
| university_qid | STRING | University the call was about |
| provider | STRING | `openai`, `anthropic`, or `gemini` |
| model | STRING | Model name, for example `gpt-4o` |
| purpose | STRING | `extract` (list the units), `match` (pick the Wikidata item), or `judge` (review the union of lists) |
| attempt | INTEGER | Attempt number for this request (1, 2, 3). Empty on a cache hit |
| request_config | STRING, JSON | Tools (for example `web_search_preview`), extraction mode, sample number, output limit |
| cache_key | STRING | Name of the cache file for this request |
| prompt_hash | STRING | Short hash of the prompt. Same hash means same prompt |
| prompt_path | STRING | Full prompt in the bucket. A match prompt includes `choices`, every Wikidata item the model was shown |
| response_path | STRING | Raw response in the bucket, saved before the answer is parsed |
| raw_response_available | BOOLEAN | False when no raw response exists |
| cache_hit | BOOLEAN | True when the answer came from the cache and no request was sent |
| cached_from_llm_call_id | STRING | On a cache hit: the earlier call that made the answer |
| tokens_in, tokens_out | INTEGER | Tokens sent and received. Empty on a cache hit |
| latency_ms | INTEGER | Time the request took, in milliseconds |
| error | STRING | Error text when the attempt failed |
| note | STRING | Explanation when no raw response exists |
| called_at | TIMESTAMP | Time of the call (UTC) |
| legacy | BOOLEAN | True for a row imported from a run before the run log (run `us-tier1`). Such a row has no prompt and no raw response |
| source_file | STRING | For a legacy row: the original file in the bucket that the row came from. Empty otherwise |

## evidence

One row is one web page that the pipeline fetched and saved. The table is empty until pages
are fetched (Anya's week 4).

| Column | Type | Meaning |
|---|---|---|
| evidence_id | STRING, required | Id of this fetch |
| run_id, invocation_id | STRING, required | The run and invocation that fetched the page |
| url | STRING | Address of the page |
| fetched_at | TIMESTAMP | Time of the fetch (UTC) |
| http_status | INTEGER | Web server answer, for example 200 (found) or 404 (not found) |
| content_hash | STRING | SHA-256 hash of the page. Same hash means same content |
| snapshot_path | STRING | Saved copy of the page in the bucket |
| unit_names | STRING, list | Unit names found on the page |

## candidates

One row is one unit (school, college, institute) that the LLM proposed for a university,
with the result of the check against Wikidata.

| Column | Type | Meaning |
|---|---|---|
| candidate_id | STRING, required | Id of this row |
| run_id, invocation_id | STRING, required | The run and invocation that proposed the unit |
| university_qid | STRING | The university |
| parent_qid | STRING | QID of the parent unit. Today always the university |
| parent_candidate_id | STRING | Parent's candidate_id when the parent has no QID. Empty today |
| name | STRING | Name the LLM gave |
| unit_type | STRING | Type the LLM gave, as free text (for example "College") |
| status | STRING | `linked` (on Wikidata and linked to the university), `orphan` (on Wikidata, not linked), `missing` (not found on Wikidata), `unresolved` (could not be checked) |
| matched_qid, matched_label | STRING | The Wikidata item it was matched to, and that item's label |
| match_method | STRING | `fuzzy` (name comparison), `llm` (an LLM picked the item), `none` (no match) |
| match_choices | STRING, JSON | Every Wikidata item the match step considered. `source` is `linked_child` (already linked to the university) or `search` (found by name search) |
| source_url | STRING | Page the LLM cited for the unit |
| website | STRING | Unit website the LLM gave |
| provider | STRING | Provider whose list proposed the unit |
| llm_call_ids | STRING, list | The extract call and any match calls behind this row |
| evidence_ids | STRING, list | Saved pages behind this row. Empty until week 4 |
| created_at | TIMESTAMP | Time the row was made (UTC) |
| legacy | BOOLEAN | True for a row imported from a run before the run log (run `us-tier1`). Such a row has no saved prompt, response, or page |
| source_file | STRING | For a legacy row: the original CSV or cache file in the bucket that the row came from. Empty otherwise |

## How the tables link

- `run_id` and `invocation_id` connect every row to one `runs` row: who ran it, with
  which code, command, and settings.
- `candidates.llm_call_ids` lists `llm_calls.llm_call_id` values. They give the prompt
  and raw response behind each unit and each match decision.
- `candidates.evidence_ids` will list `evidence.evidence_id` values from week 4.
- `llm_calls.cached_from_llm_call_id` points a cache hit to the call that first made the
  answer, which can be in an earlier run.

Example: all units of one run and the calls behind them.

```sql
SELECT c.name, c.status, c.matched_qid, l.purpose, l.response_path
FROM `wikidata-academia.academiabot.candidates` c,
     UNNEST(c.llm_call_ids) AS id
JOIN `wikidata-academia.academiabot.llm_calls` l ON l.llm_call_id = id
WHERE c.run_id = 'discover-20261014T164359Z-942cfd'
```

## Known limitations

- **No raw response for old answers.** Answers cached before 2026-10-14 have only the parsed
  answer. Their rows have `raw_response_available` false and a `note`.
- **Old runs are not in the tables.** Run `us-tier1` (2026-10-03, 9,190 candidates) predates
  the run log. Anya's week 3 imports it.
- **Evidence is empty.** No page is fetched yet, so `evidence` has no rows and every
  `evidence_ids` list is empty.
- **One level only.** Every candidate's parent is the university. Departments come later.
- **unit_type is free text.** The LLM chooses the word. `docs/MODELING_RULES.md` (week 4)
  will set the allowed types.
- **Local fallback is not imported.** When BigQuery or the bucket fails, rows go to
  `wikidata_discover/results/runs/<run_id>/runlog/<invocation_id>/`. Nothing copies them
  into BigQuery yet. When only the bucket fails, the rows are in both places with the same
  ids, so remove duplicates by id.
- **A killed process has no runs row.** The runs row is written at the end. Rows of
  finished universities are written after each university, so they remain.
- **Batch runs do not record key sources.** Only `discover` puts `key_sources` in `config`.
- **BigQuery does not enforce ids.** Ids are unique by construction, but BigQuery has no
  primary key. The tables have no partitions, which is fine at today's size.
- **New columns need a schema change.** Runs never create or change tables. Add the column
  in `run_log.py` and in BigQuery together.
