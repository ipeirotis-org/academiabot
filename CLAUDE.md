# CLAUDE.md — ipeirotis-org/academiabot

## Purpose

Wikidata Discovery Toolkit — a Python CLI that identifies missing organizational units (schools, colleges, faculties, divisions) within universities on Wikidata. Combines SPARQL queries, OpenAI LLM extraction, and Wikidata API search to produce actionable CSVs of missing entities ready for batch Wikidata edits.

## Architecture

```
academiabot/
├── wikidata_discover/          # Main application package
│   ├── cli.py                  # CLI entry point (argparse: "harvest" and "discover")
│   ├── config.py               # Configuration & env vars
│   ├── discovery.py            # Core Discovery class (find missing divisions)
│   ├── harvester.py            # Harvest all U.S. universities via SPARQL
│   ├── hierarchy.py            # BFS crawler for organizational hierarchy
│   ├── llm_helpers.py          # OpenAI integration & structured prompting
│   ├── sparql_helpers.py       # SPARQL query execution wrapper
│   ├── wikidata_api.py         # Wikidata entity search API
│   ├── requirements.txt        # Python dependencies
│   ├── results/                # Output directory (JSON + CSV)
│   └── scripts/
│       └── wikidata_division_discover.py  # CLI entrypoint script
└── misc_scripts/               # Legacy hierarchy crawlers
```

## How It Works

### Command 1: `harvest`
Runs a SPARQL query to fetch all U.S. universities (P31/P279 → Q3918, P17 → Q30). Outputs `universities_us.json`.

### Command 2: `discover Q<ID>`
1. Fetches the target university's label and website from Wikidata
2. Sends university info to OpenAI LLM → extracts candidate divisions (name, type, city, state, website)
3. For each candidate: searches Wikidata, checks existing children (P361/P355/P749), uses LLM to classify as linked / orphan / missing
4. Outputs console table + `missing_divisions_Q<ID>.csv`

## Tech Stack

- **Python 3** with `openai`, `SPARQLWrapper`, `pandas`, `rich`, `questionary`
- **OpenAI API** — GPT-4o/GPT-5 for entity extraction and matching
- **Wikidata SPARQL** endpoint (`https://query.wikidata.org/sparql`)
- **Wikidata API** (`https://www.wikidata.org/w/api.php`)

## Configuration

Environment variables (via `.env`):
- `OPENAI_API_KEY` — required
- `WD_BOT_USERAGENT` — default: `"AcademiaBot/1.0 (ipeirotis@example.com)"`

Hardcoded in `config.py`:
- `LLM_MODEL = "gpt-5"` (README says "gpt-4o" — mismatch)
- `SPARQL_ENDPOINT` — Wikidata query service

## Key Wikidata Predicates

| Predicate | Meaning | Used For |
|-----------|---------|----------|
| P31/P279 | instance of / subclass of | Identify universities |
| P17 | country | Filter for U.S. |
| P361 | part of | Upward hierarchy |
| P749 | parent organization | Upward hierarchy |
| P527/P355/P199 | has part / subsidiary / business division | Downward hierarchy |
| P856 | website | University URL |

## Known Issues

1. **Model version mismatch** — `config.py` says `gpt-5`, README says `gpt-4o`
2. **Relative imports in misc_scripts** — `hierarchy.py` uses `from sparql_helpers import ...` instead of full path
3. **No rate limiting** on Wikidata Search API calls (SPARQL has 0.3s sleep)
4. **Minimal error handling** for LLM JSON schema validation failures

## TODO.md

This repo's TODO.md feeds into the `Research: Wikidata` section of the main tasks repo (`ipeirotis/tasks`).
