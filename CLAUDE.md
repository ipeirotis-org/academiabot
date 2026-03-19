# CLAUDE.md

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
│   ├── llm_helpers.py           # OpenAI Responses API: extract_divisions, choose_match
│   ├── sparql_helpers.py        # Thin wrapper around SPARQLWrapper
│   ├── wikidata_api.py          # wbsearchentities wrapper
│   ├── to_qs_wikidata.py        # Export missing entities as QuickStatements
│   ├── requirements.txt
│   ├── results/                 # Output CSVs + universities_us.json
│   └── scripts/
│       └── wikidata_division_discover.py   # Entrypoint
└── misc_scripts/                # Legacy hierarchy scripts (not imported by main package)
```

## How to run

```bash
pip install -r wikidata_discover/requirements.txt
# Set OPENAI_API_KEY in .env
python -m wikidata_discover.scripts.wikidata_division_discover harvest
python -m wikidata_discover.scripts.wikidata_division_discover discover Q49210  # NYU
```

## Tech stack

- Python 3.11+
- OpenAI Responses API (`client.responses.create`) with JSON schema output and `web_search_preview` tool
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

## Data model (target hierarchy)

```
University (Q3918)
  └─ P749 ─ College/School (Q31855 or Q3918)
       └─ P749 ─ Department (Q1183543 / Q2467461)
            └─ P749 ─ Program / Lab / Center (Q1664727 / Q4830453)
                 └─ P108 ─ Faculty/Researcher
```

Use **P749 (parent organization)** as the primary relationship. Add P361 as supplementary only. For dual-parent units (joint departments), add a second P749 with rank=normal and qualifiers.

## Current pipeline

1. `harvest`: SPARQL fetches all U.S. universities (P31/P279 -> Q3918, P17 -> Q30)
2. `discover <QID>`: For a given university:
   a. Fetch university label + website from Wikidata
   b. LLM extracts candidate top-level units (schools/colleges) with web search
   c. For each candidate: fuzzy-match against existing Wikidata children (rapidfuzz)
   d. Unmatched candidates go to LLM `choose_match` for disambiguation
   e. Results classified as: exists_linked, exists_orphan, or missing
   f. Missing entities exported to CSV + QuickStatements file

## LLM integration details

- `llm_helpers.py` uses OpenAI Responses API (not Chat Completions)
- `extract_divisions()`: structured JSON output with `web_search_preview` tool, `reasoning.effort = "high"`
- `choose_match()`: single-token classification (QID / ORPHAN:QID / NONE)
- Model configured in `config.py` as `LLM_MODEL`
- Future: support Anthropic Claude API as alternative provider

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
6. ~~CLI `--llm` override is broken (imports `config` instead of `wikidata_discover.config`)~~ Fixed
7. No tests exist

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
- **Authentication:** Handled automatically via the `cloud-bootstrap` skill and SessionStart hook (`.claude/hooks/cloud-auth.sh`)
- **New team members:** The agent handles onboarding via the cloud-bootstrap "Add Team Member" flow
- **Permission escalation:** Ask the agent to escalate; it will propose roles and ask you to approve via `gcloud`

## Secret Manager (API keys)

All LLM API keys are stored in **GCP Secret Manager** under project `wikidata-academia`. This is the canonical source for keys used by Cloud Functions, Cloud Run, and other deployed services. Local development can still use `.env` as a fallback.

| Secret name | Description | Accessed by |
|-------------|-------------|-------------|
| `openai-api-key` | OpenAI API key for GPT-4o / Responses API | `llm_helpers.py`, Cloud Functions |
| `anthropic-api-key` | Anthropic API key for Claude models | Future: multi-provider LLM support |
| `gemini-api-key` | Google Gemini API key | Future: Vertex AI provider in `llm_helpers.py` |

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

## Important: always check TASKS.md for current phase and priorities.
