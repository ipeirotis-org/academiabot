# Wikidata Discovery Toolkit

A lightweight Python CLI suite for querying and synchronizing Wikidata entities.  
It provides two core commands:

1. **`harvest`** – Fetch and persist a full list of all U.S. universities (Q-IDs, labels, and websites) from Wikidata to JSON for downstream analysis.
2. **`discover`** – Identify missing top-level academic or administrative units (“divisions”) of a university by combining SPARQL queries, OpenAI LLM prompts, and Wikidata API lookups, then output a CSV of items to add.  

---

## Details

- **CSV export** of missing divisions ready for batch Wikidata edits.
- **JSON export** of U.S. universities for offline reuse.
- **Configurable** via environment variables (`.env`):
  - `OPENAI_API_KEY` – Your OpenAI API key  
  - `WD_BOT_USERAGENT` – Custom `User-Agent` for Wikidata/SPARQL requests (defaults to `AcademiaBot/1.0`)
- **Rich** console output and tables for easy debugging.

---

## Usage

The commands share a single entrypoint script. Run them as Python modules from the project root:

```
python3 -m scripts.wikidata_division_discover <command> [options]
```

### 1. Harvest all U.S. universities

```
python3 -m scripts.wikidata_division_discover harvest
```

* Queries Wikidata for every U.S. university (P31/P279 → Q3918 & P17 → Q30).
* Saves raw JSON to universities_us.json.
* Prints a summary table of Q-IDs, labels, and websites.

### 2. Discover missing divisions

```
python3 -m scripts.wikidata_division_discover discover Q49210
```

* Q49210 – Wikidata Q-ID of the target university (e.g. New York University).
* Outputs a table of each candidate unit and its match status.
* Writes missing_divisions_<QID>.csv if any units are not yet linked in Wikidata.

#### Options

* --llm MODEL – Override the default OpenAI model (default: gpt-4o).

