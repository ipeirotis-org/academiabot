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
| command | STRING | `discover`, `batch`, `snapshot`, or `import-legacy` |
| args | STRING, JSON | The exact command arguments |
| qids | STRING, list | Universities the command was asked to process |
| config | STRING, JSON | Providers with a key, models, depth, and where each API key came from (`env`, `dotenv`, `secret_manager`). Never the key itself |
| host | STRING | `local`, or the Cloud Function name |
| started_at, ended_at | TIMESTAMP | Start and end of the invocation (UTC) |
| outcome | STRING | `ok`, `failed`, `stopped_for_time` (batch only), `partial` (snapshot: some universities failed and are retried on the next start), or `interrupted` (stopped by hand) |
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

One row is one attempt to fetch one cited web page (Anya's week 4, `wikidata_discover/fetcher.py`).
A page that was refused or did not arrive still gets a row, with its outcome and the reason.

| Column | Type | Meaning |
|---|---|---|
| evidence_id | STRING, required | Id of this fetch |
| run_id, invocation_id | STRING, required | The run and invocation that fetched the page |
| url | STRING | Address of the page |
| fetched_at | TIMESTAMP | Time of the fetch (UTC) |
| http_status | INTEGER | Web server answer, for example 200 (found) or 404 (not found) |
| content_hash | STRING | SHA-256 hash of the page bytes as received. Same hash means same content |
| snapshot_path | STRING | Saved copy of the page in the bucket: the exact bytes received, so its SHA-256 equals content_hash. The file ends in `.html`, `.pdf`, or `.bin` (any other type), and keeps the Content-Type the server sent. Rows written before 2026-11-04 saved a UTF-8 copy as `.html` |
| unit_names | STRING, list | Names of the units that cite this page and appear in its text |
| final_url | STRING | The last address asked for, after redirects |
| outcome | STRING | `ok`, `http_error` (the server answered with an error status), `refused` (a safety rule stopped it; nothing was fetched), `too_many_redirects` (more than 5), `too_large` (over 5 MB), `timeout` (over 20 seconds), or `error` (connection or TLS problem, or the connection closed before the whole page arrived; nothing is saved) |
| detail | STRING | Why, for any outcome but `ok`. For a refusal: the rule and, for a redirect, its number |
| content_type | STRING | The Content-Type the server sent |
| text_path | STRING | Saved visible text of the page in the bucket. HTML is decoded with the charset the page declares (header, then `<meta>`), else UTF-8. Empty for a PDF or another type that is not read |
| name_check | STRING | `done` when unit_names was looked for in the text. `skipped: <reason>` for a PDF or another type that is not read: an empty unit_names then does not mean "name not found". Empty when no page arrived |

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
| status | STRING | `linked` (on Wikidata and linked to the university), `orphan` (on Wikidata, not linked), `missing` (not found on Wikidata), `unresolved` (could not be checked), `unaligned` (a sub-unit from the week 4 `subunits` step, not yet compared with Wikidata), `pending` (week 5: on Wikidata, under a parent that has no QID yet), `uncertain` (week 5: not in the Wikidata snapshot; the full search is week 6) |
| matched_qid, matched_label | STRING | The Wikidata item it was matched to, and that item's label |
| match_method | STRING | `fuzzy` (name comparison), `llm` (an LLM picked the item), `none` (no match), `legacy_unknown` (legacy row: the old run did not save how it matched) |
| match_choices | STRING, JSON | Every Wikidata item the match step considered. `source` is `linked_child` (already linked to the university) or `search` (found by name search) |
| source_url | STRING | Page the LLM cited for the unit |
| website | STRING | Unit website the LLM gave |
| provider | STRING | Provider whose list proposed the unit |
| llm_call_ids | STRING, list | The extract call and any match calls behind this row |
| evidence_ids | STRING, list | Saved pages behind this row. Empty until week 4 |
| created_at | TIMESTAMP | Time the row was made (UTC) |
| legacy | BOOLEAN | True for a row imported from a run before the run log (run `us-tier1`). Such a row has no saved prompt, response, or page |
| source_file | STRING | For a legacy row: the original CSV or cache file in the bucket that the row came from. Empty otherwise |

## nodes

One row is one node of a hierarchy file (week 5, `discover --depth 2`): a unit of one
university's tree, with its stable node id. The same node id appears in later runs; each
run adds new rows and never changes old ones. The full file is
`runs/<run_id>/hierarchy/<QID>.json` in the bucket.

| Column | Type | Meaning |
|---|---|---|
| node_id | STRING, required | Stable id from the register `gs://academiabot/hierarchy/<QID>/ids.json`. The university is `root` |
| run_id, invocation_id | STRING, required | The run and invocation that wrote the node |
| university_qid | STRING | The university at the root of the tree |
| level | INTEGER | 0 for the university, 1 for its direct units, 2 below them |
| name | STRING | Name the unit uses (the LLM's name, or the Wikidata label for a unit only Wikidata has) |
| aliases | STRING, list | Other names: the Wikidata label and English aliases of the matched item |
| unit_type | STRING | school, department, program, center, campus, office, other (`docs/MODELING_RULES.md`) |
| type_source | STRING | `llm`, or `wikidata (...)` with the reason, for a unit only Wikidata has |
| in_scope | STRING | yes, no, or check (a person decides). Units with "no" are left out of the file |
| origin | STRING | `llm`, `snapshot` (only Wikidata has it), or `llm and snapshot` |
| parent_ids | STRING, list | node_ids of the parents. A parent in another university's tree is `<QID>/root` |
| parent_links | STRING, JSON | One entry per parent: parent_id, parent_qid, and `p749` (present, missing, or not applicable) |
| qid | STRING | The Wikidata item, or empty |
| country | STRING, JSON | label, qid, and source (a page, an address, or `wikidata:<QID> (P17)`) |
| node_version | STRING | Hash of everything a reviewer is shown. It changes when any of it changes |
| alignment | STRING | root, linked, orphan, pending, or uncertain (week 6 adds new) |
| alignment_reason | STRING | Why, in words: for example "not in the snapshot; full search in week 6" |
| alignment_candidates | STRING, JSON | The Wikidata items considered, how each was found, and which was chosen |
| wikidata_duplicates | STRING, JSON | Items that look like a second Wikidata item for the same unit (week 6) |
| merged_into | STRING | The surviving node_id when this node was merged into another (week 6) |
| website | STRING | The unit's website |
| description | STRING | The English description a new item would get |
| source_url | STRING | The page that names the unit as part of its parent |
| evidence_ids | STRING, list | `evidence` rows of the saved pages for this node |
| evidence | STRING, JSON | One entry per fact (unit, country): url, evidence_id, fetch outcome, name_on_page |
| children_lookup | STRING | Whether the sub-units were looked up, and why not: for example "children not looked up (study-away site)" |
| flags | STRING, list | Problems for a person: no source URL, no country, an unclear id match |
| candidate_ids | STRING, list | The `candidates` rows that proposed the unit (empty for a unit only Wikidata has) |
| llm_call_ids | STRING, list | The sub-unit call behind the unit |
| provider | STRING | Provider whose answer proposed the unit |
| created_at | TIMESTAMP | Time the row was made (UTC) |

## How the tables link

- `run_id` and `invocation_id` connect every row to one `runs` row: who ran it, with
  which code, command, and settings.
- `candidates.llm_call_ids` lists `llm_calls.llm_call_id` values. They give the prompt
  and raw response behind each unit and each match decision.
- `candidates.evidence_ids` will list `evidence.evidence_id` values from week 4.
- `nodes.candidate_ids`, `nodes.evidence_ids`, and `nodes.llm_call_ids` lead to the rows
  behind each node. `nodes.node_id` stays the same across runs.
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
- **Legacy rows are less traceable.** Run `us-tier1` (2026-10-03) was imported with
  `legacy` true: 9,190 candidates and 8,169 llm_calls. It kept no prompt, no raw response,
  and no page. A legacy linked row has `matched_qid` and `matched_label` empty, because
  the old run never saved which item it matched. A legacy orphan row has the QID from the
  CSV, and `matched_label` is the item's English label on the day of the import. Legacy
  rows have no `source_url` (the old answers cited no page) but keep the unit's `website`.
  Their `llm_call_ids` list only the extraction call: a match file cannot be traced back
  to its unit. Run `us-tier1` has no `runs` rows; its invocations are in
  `gs://academiabot/runs/us-tier1/invocations.jsonl`. Kensington College (Q16998232)
  failed in that run and has no candidates rows.
- **Evidence starts in week 4.** Legacy rows and the rows of `discover` have empty
  `evidence_ids`. The `subunits` step fills them.
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
