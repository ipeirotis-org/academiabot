# AGENTS.md

Instructions for AI coding agents working in this repo. `CLAUDE.md` imports this file.

## Project

**AcademiaBot** populates Wikidata with the full organizational hierarchy of universities worldwide (university > college/school > department > program) and connects faculty/researcher entities to their departments. We use LLMs for entity discovery, disambiguation, and matching, and the Wikidata API + SPARQL for reads/writes.

## Repository layout

```
academiabot/
├── wikidata_discover/           # Main package
│   ├── cli.py                   # argparse CLI: "harvest" and "discover" commands
│   ├── config.py                # Env vars, constants (OPENAI_API_KEY, LLM_MODEL, SPARQL_ENDPOINT)
│   ├── discovery.py             # Core Discovery class: orchestrates LLM + SPARQL + matching
│   ├── harvester.py             # SPARQL harvest of all U.S. universities to JSON
│   ├── hierarchy.py             # BFS crawler over P527/P355/P199/P361/P749
│   ├── llm_helpers.py           # LLMHelper: per-provider extract_divisions_*, ensemble, judge_union, choose_match
│   ├── sparql_helpers.py        # Thin wrapper around SPARQLWrapper
│   ├── wikidata_api.py          # wbsearchentities wrapper
│   ├── to_qs_wikidata.py        # Export missing entities as QuickStatements
│   ├── batch.py                 # Resumable batch over many QIDs; uploads every artifact to the bucket
│   ├── cloud/collect_function.py # Cloud Function (gen 2) entry point: one time slice of a run, on a schedule
│   ├── requirements.txt
│   ├── eval/                    # Ground truth for 12 universities + run_eval.py harness
│   ├── results/                 # Output CSVs, universities_us.json, LLM cache
│   └── scripts/
│       ├── wikidata_division_discover.py   # Entrypoint
│       └── batch_collect.py                # CLI wrapper around batch.py
├── deploy/                      # deploy_collect_function.sh: Cloud Function + paused hourly Scheduler job
├── docs/                        # BACKGROUND.md (origins, decisions); later REVIEW_GUIDE.md, MODELING_RULES.md
├── tests/                       # pytest unit tests (fuzzy matching)
└── misc_scripts/                # Legacy hierarchy scripts (deprecated, not imported)
```

## How to run

```bash
pip install -r wikidata_discover/requirements.txt pytest
# Copy env.example to .env and set at least one provider key (OPENAI_API_KEY preferred; all three for the eval harness)
python -m wikidata_discover.scripts.wikidata_division_discover harvest
python -m wikidata_discover.scripts.wikidata_division_discover discover Q49210  # NYU
python -m wikidata_discover.eval.run_eval      # precision/recall against ground truth
python -m pytest tests -q
```

## Tech stack

- Python 3.11+
- Three LLM providers with structured JSON output: OpenAI Responses API (with `web_search_preview`),
  Anthropic Messages API and Google Gemini (`google-genai`), both currently without web search or grounding
- SPARQLWrapper for Wikidata SPARQL endpoint
- rapidfuzz for fuzzy name matching
- pandas for CSV I/O
- rich for console output

## Key Wikidata properties

| Property | Meaning | Usage |
|----------|---------|-------|
| P31 | instance of | Classify entities (Q3918=university, Q1183543=academic dept) |
| P279 | subclass of | Type hierarchy |
| P749 | parent organization | **Primary relationship**: school -> university, dept -> school |
| P361 | part of | Alternative/supplementary upward link |
| P527 | has part | Downward: university -> schools |
| P355 | has subsidiary | Downward: org -> sub-org |
| P199 | business division | Downward: org -> division |
| P856 | official website | QA and verification |
| P1771 | IPEDS ID | U.S. institution identifier |
| P108 | employer | Researcher -> institution |
| P39 | position held | Faculty role |
| P101 | field of work | Department/researcher discipline |
| P3418 | academic discipline | More specific than P101 |
| P1960 | Google Scholar author ID | Researcher profile link |
| P496 | ORCID iD | Researcher identifier |
| P6782 | ROR ID | Research Organization Registry identifier for institutions |
| P571 | inception | When a unit was founded (optional) |

## Data model (target hierarchy)

```
University (Q3918)
  └─ P749 ─ College/School (Q31855 or Q3918)
       └─ P749 ─ Department (Q1183543 / Q2467461)
            └─ P749 ─ Program / Lab / Center (Q1664727 / Q4830453)
                 └─ P108 ─ Faculty/Researcher
```

Use **P749 (parent organization)** as the primary relationship. Add P361 as supplementary only. For dual-parent units (joint departments), add a second P749 with rank=normal and qualifiers.

Minimum statement set for any new item: label, English description, P31, P749, P17, and P856 if known. Universities also get P1771. One QuickStatements file per hierarchy level. Reasoning for these choices is in `docs/BACKGROUND.md`.

## Current pipeline

1. `harvest`: SPARQL fetches all U.S. universities (P31/P279 -> Q3918, P17 -> Q30)
2. `discover <QID>`: For a given university:
   a. Fetch university label + website from Wikidata
   b. `extract_divisions_best_available()` tries OpenAI, then Anthropic, then Gemini, and returns the
      first non-empty result. Only OpenAI has web search. The ensemble + judge is NOT used here yet.
   c. (eval only) `extract_divisions_ensemble()` runs two generators and `judge_union()`; see known issue 9
   d. For each candidate: fuzzy-match against existing Wikidata children (rapidfuzz)
   e. Unmatched candidates go to LLM `choose_match` for disambiguation
   f. Results classified as: exists_linked, exists_orphan, missing, or unresolved (Wikidata could not be
      checked for that candidate; it is reported but never exported)
   g. Results exported to CSV + QuickStatements: missing entities as CREATE blocks, orphans as a single
      statement linking the existing QID to the university

## LLM integration details

- `extract_divisions_openai/anthropic/gemini()`: one per provider, same output shape. Only the OpenAI call
  enables a web search tool; the other two answer from model knowledge, so their cited URLs are unverified
- `extract_divisions_best_available()`: first-success fallback (OpenAI, then Anthropic, then Gemini). This is
  what `discover` calls. It does not run all providers and does not call the judge
- `extract_divisions_ensemble()`: OpenAI + Anthropic generate, Gemini judges. Used by the eval harness only
- `judge_union()`: one provider reviews the union of all candidates and keeps only real units
- `choose_match()`: single-token classification (QID / ORPHAN:QID / NONE)
- Responses are cached in `results/cache/`, keyed by (university, provider, model)
- Models configured in `config.py`: `LLM_MODEL` (OpenAI), `ANTHROPIC_MODEL`, `GEMINI_MODEL`; all overridable in `.env`
- Eval on 12 universities: best config is Anthropic judge over OpenAI + Gemini, about 0.95 precision and recall
  (see `eval/results_summary.csv`)

## Working norms (read first)

Students on this project direct agents; they do not write most of the code. There are two
students with two tracks in `TASKS.md`: Anya builds the pipeline (section 4), Shuo builds the
checks and the human review (section 5). So:

- Work on exactly the track and week in `TASKS.md` that the student names. Do not start the next one.
- The "you check it by" cell for that week is the acceptance test. Make it pass and show it passing.
- Before writing code, give a short plan (five lines or fewer) and wait for a go-ahead.
- Explain what you did in plain language. Assume the reader can run a command and open a CSV but will not read a diff.
- Run `python -m pytest tests -q` before saying anything is done. Add a test for every behavior you add.
- At the end of a session: tick the boxes you completed in `TASKS.md`, add anything a future agent needs to this file, and leave the student a three-line summary.
- Never upload to Wikidata, never run QuickStatements batches, never write to the Wikidata API. Humans do that step.
- Never commit `.env` or any API key.
- One milestone per branch and pull request.

## Coding conventions

- All SPARQL goes through `sparql_helpers.py` (never construct SPARQLWrapper directly)
- All LLM calls go through `LLMHelper` static methods in `llm_helpers.py`
- Use `config.console` (rich Console) for user-facing output
- Keep 0.3s sleep between SPARQL requests (polite crawling)
- Output files go to `wikidata_discover/results/`
- Never use em-dashes in code comments, docstrings, or output strings
- Write functions that are independently testable (separate logic from I/O)
- For new LLM prompts, follow the pattern in `llm_helpers.py` (structured JSON schema)

## Known issues

1. ~~`config.py` hardcodes `LLM_MODEL = "gpt-5"` but README says "gpt-4o"~~ Fixed: reads from `.env` with default `gpt-4o`
2. ~~`misc_scripts/hierarchy.py` uses broken relative imports~~ Fixed: scripts are standalone; deprecated in favor of `wikidata_discover.hierarchy`
3. ~~No rate limiting on `wikidata_api.quick_wd_search()`~~ Fixed: 0.3s delay added
4. ~~No retry/backoff on API failures~~ Fixed: tenacity exponential backoff on SPARQL and Wikidata API
5. ~~`to_qs_wikidata.py` caps at 10 items (`missing[:10]`) with no config~~ Fixed: configurable `max_items` param, defaults to all
6. ~~CLI `--llm` override is broken~~ Fixed twice: `cli.py` now sets `config.LLM_MODEL`, and `llm_helpers.py`
   reads model names from `config` at call time instead of capturing them at import
7. ~~No tests exist~~ Fixed: `tests/test_fuzzy.py` covers `normalize_name` and `is_fuzzy_match`
8. Discovery only goes one level deep (schools). Departments are the current work; see TASKS.md
9. `discover` uses `extract_divisions_best_available()` (first provider that answers), not the ensemble that
   scored best in eval. Wiring the ensemble into `discover` is a Milestone 2 task
10. Web search is enabled only for OpenAI. Anthropic and Gemini extraction has no search or grounding tool
11. `to_qs_wikidata.py` links new items to the university with P361, but the data model says P749 is primary.
    Fix when the exporter is reworked in Milestone 3
12. ~~`choose_match()` could never return an orphan: it compared the whole `ORPHAN:QID` token to bare QIDs~~
    Fixed: `parse_match_answer()` handles QID, ORPHAN:QID, and NONE, with tests

## Cloud Credentials

- **Provider:** GCP
- **Project:** `wikidata-academia`
- **Service account:** `claude-agent@wikidata-academia.iam.gserviceaccount.com`
- **Roles granted:**
  - `roles/bigquery.dataEditor` -- read/write collected data in BigQuery
  - `roles/bigquery.jobUser` -- run BigQuery queries
  - `roles/storage.objectAdmin` -- read/write data files in GCS buckets
  - `roles/logging.viewer` -- view logs for debugging
  - `roles/cloudfunctions.developer` -- deploy Cloud Functions for data collection
  - `roles/cloudscheduler.admin` -- schedule recurring data collection jobs
  - `roles/iam.serviceAccountUser` -- required for deploying Cloud Functions as the service account
  - `roles/secretmanager.secretAccessor` -- securely access API keys
  - `roles/secretmanager.admin` -- create and manage secrets in Secret Manager
  - `roles/aiplatform.user` -- use Vertex AI / Gemini for entity discovery and judging
  - `roles/run.developer` -- deploy Cloud Run services for long-running tasks
  - `roles/pubsub.editor` -- event-driven pipelines between collection, verification, and writing stages
  - `roles/cloudfunctions.invoker` -- allow scheduler and other functions to trigger Cloud Functions
- **Multi-user setup:** Each team member has their own `.cloud-credentials.<email>.enc` file, encrypted with their personal passphrase
- **Authentication:** Handled automatically via the `cloud-bootstrap` skill and SessionStart hook (`.claude/hooks/cloud-auth.sh`). The hook matches the credentials file to `git config user.email`, so that must be set to the team member's email.
- **Cross-project access:** The service account also has READER on dataset `nyu-datasets.academiabot` (granted 2026-10-02). See "BigQuery access" below.
- **New team members:** The agent handles onboarding via the cloud-bootstrap "Add Team Member" flow
- **Permission escalation:** Ask the agent to escalate; it will propose roles and ask you to approve via `gcloud`

## Run log (planned, Anya week 2)

Every run must be reproducible. Anya's week 2 creates the first four tables below in dataset
`academiabot` of project `wikidata-academia`, with large text in Cloud Storage and the GCS path
kept in the row. The fifth table, `reviews`, is Shuo's week 6; it is specified here so both
tracks build to the same shape.

| Table | One row per | Must contain |
|---|---|---|
| `runs` | command invocation | run_id, who, git commit, the exact command and arguments (subcommand, QIDs, flags), config (providers, models, depth), start and end time, outcome |
| `llm_calls` | API call | llm_call_id, run_id, provider, model, purpose (extract, judge, match, verify), prompt hash, GCS paths to the full prompt and the raw response, tokens, latency, cache hit |
| `evidence` | web page fetched | evidence_id, run_id, url, fetched_at, http_status, content hash, GCS path to the snapshot, unit names found on the page |
| `candidates` | unit proposed | candidate_id, run_id, parent_qid, name, unit_type, status (linked, orphan, missing, unresolved), matched_qid, source_url, llm_call_ids, evidence_ids |
| `reviews` | one reviewer's verdict on one candidate | review_id, candidate_id, reviewer, source (expert, prolific), verdict (accept, reject, fix), corrected_value, url_checked, notes, reviewed_at |

Every table has its own stable id so that a candidate's `llm_call_ids` and `evidence_ids`
resolve to exact rows. Reviews are append-only: a second reviewer adds a row, never
overwrites one, so agreement between reviewers can be computed. The export honors the
reviews a protocol says it should (for example, two accepts and no reject).

Rules: write the raw LLM response to storage before parsing it. Cache keys include the prompt
hash. Local JSON under `results/runs/` is the fallback when GCP is unreachable. Keys come from
Secret Manager when `.env` has none (see "Secret Manager" below).

## Running collection in the cloud

Collection runs should not depend on a laptop or a sandbox session. `wikidata_discover/batch.py`
holds the resumable batch logic; `scripts/batch_collect.py` runs it from a terminal and
`cloud/collect_function.py` runs it as a Cloud Function (gen 2, HTTP, 60 minute timeout) that
processes one time slice per invocation and resumes from the run log in the bucket. Cloud
Scheduler calls it hourly. State and artifacts live only in `gs://academiabot/runs/<run_id>/`.

- Deploy or update: `bash deploy/deploy_collect_function.sh` (creates the scheduler job PAUSED).
- Start collecting: `gcloud scheduler jobs resume academiabot-collect-hourly --location=us-east1`.
  Only a person does this; it spends LLM credit.
- Stop: `gcloud scheduler jobs pause academiabot-collect-hourly --location=us-east1`.
- Progress: read `runs/<run_id>/log.jsonl` in the bucket. One line per university attempt.
  The last record for a QID wins; a QID is done only when its last record is ok and uploaded.
- Request body (all optional): `run_id`, `list_object`, `max_universities` (60), `time_budget_s`
  (3000), `reserve_s` (600: no university starts unless that much budget, or the longest
  university so far, is left), `qids` (explicit list, still de-duplicated and capped).
- LLM cache files that an unfinished university used are restored from the bucket before a
  retry, so a retry on a fresh instance reuses the same LLM answers.
- One-time prerequisite for a project owner: enable the Cloud Functions, Cloud Run, Cloud Build,
  Artifact Registry, Cloud Scheduler, and Eventarc APIs. The service account cannot enable APIs.
- The `gcloud` CLI in a Claude Code cloud session needs `env -u CLOUDSDK_AUTH_ACCESS_TOKEN` in
  front of it, because the session proxy sets that variable to a placeholder.
- Wikidata rate limits are per IP, so one instance at a time (`--max-instances=1`), about 60
  universities per hour. The full U.S. list is roughly two days of hourly slices.

## BigQuery access

The earlier crowdsourced hierarchy (about 16,000 organizations) lives in
`nyu-datasets.academiabot.organization`. The service account has READER on that dataset.
Schema and caveats are in `docs/BACKGROUND.md`.

Query it with the Python client. The `bq` command-line tool returns "Invalid Credentials"
behind the cloud session proxy, so do not use it here.

```python
from google.cloud import bigquery
client = bigquery.Client()   # uses GOOGLE_APPLICATION_CREDENTIALS set by the SessionStart hook
rows = client.query("SELECT name FROM `nyu-datasets.academiabot.organization` WHERE parent_id = 1519").result()
```

The SessionStart hook decrypts the service account key to a private, uniquely named temp
file for this session only, exports `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT`,
and `REQUESTS_CA_BUNDLE`, and the SessionEnd hook deletes the file. If the client import fails on the container's system Python with a `_cffi_backend`
or `packaging` error, run this once, then `pip install -r wikidata_discover/requirements.txt`:

```bash
pip install --ignore-installed packaging cffi cryptography
```

## Secret Manager (API keys)

All LLM API keys are stored in **GCP Secret Manager** under project `wikidata-academia`. This is the canonical source for keys used by Cloud Functions, Cloud Run, and other deployed services. Local development can still use `.env` as a fallback.

| Secret name | Description | Accessed by |
|-------------|-------------|-------------|
| `openai-api-key` | OpenAI API key for GPT-4o / Responses API | `llm_helpers.py`, Cloud Functions |
| `anthropic-api-key` | Anthropic API key for Claude models | `llm_helpers.py` (`ANTHROPIC_API_KEY`) |
| `gemini-api-key` | Google Gemini API key | `llm_helpers.py` (`GOOGLE_API_KEY`) |

**Accessing secrets in code** (via `google-cloud-secret-manager`):
```python
from google.cloud import secretmanager

def get_secret(secret_id: str, project_id: str = "wikidata-academia") -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")
```

**Accessing secrets via gcloud**:
```bash
gcloud secrets versions access latest --secret=openai-api-key --project=wikidata-academia
```

**Key resolution order** (planned for `config.py`):
1. Environment variable (e.g., `OPENAI_API_KEY`) -- for local dev and CI
2. Secret Manager -- for deployed services (Cloud Functions, Cloud Run)
3. `.env` file -- fallback for local development

**Rotating a key**: Create a new version, then disable the old one:
```bash
printf "new-key-value" | gcloud secrets versions add openai-api-key --project=wikidata-academia --data-file=-
gcloud secrets versions disable <old-version-number> --secret=openai-api-key --project=wikidata-academia
```

## Important: always check TASKS.md for current milestones and priorities.
