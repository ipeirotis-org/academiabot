# Wikidata Discovery Toolkit

A lightweight Python CLI suite for querying and synchronizing Wikidata entities.  
It provides two core commands:

1. **`discover`** – Identify missing top-level academic or administrative units (“divisions”) of a university by combining SPARQL queries, OpenAI LLM prompts, and Wikidata API lookups, then output a CSV of items to add.  
2. **`harvest`** – Fetch and persist a full list of all U.S. universities (Q-IDs, labels, and websites) from Wikidata to JSON for downstream analysis.

---

## Features

- **Modular design** with clear separation of concerns:
  - SPARQL helper functions (`sparql_helpers.py`)
  - Wikidata API lookups (`wikidata_api.py`)
  - OpenAI LLM prompts and parsing (`llm_helpers.py`)
  - Core discovery logic (`discovery.py`)
  - University harvester logic (`harvester.py`)
  - Unified CLI entrypoint (`cli.py`)
- **Configurable** via environment variables (`.env`):
  - `OPENAI_API_KEY` – Your OpenAI API key  
  - `WD_BOT_USERAGENT` – Custom `User-Agent` for Wikidata/SPARQL requests (defaults to `DivisionDiscoverBot/3.0`)
- **Rich** console output and tables for easy debugging.
- **CSV export** of missing divisions ready for batch Wikidata edits.
- **JSON export** of U.S. universities for offline reuse.

---

## Prerequisites

- Python 3.8 or newer  
- [pip](https://pip.pypa.io/)  
- An OpenAI API key (for `discover`)

---

## Installation

1. Clone this repo:

   ```bash
   git clone https://github.com/your-org/wikidata_discover.git
   cd wikidata_discover

## Usage

All commands share a single entrypoint script. Run them as Python modules from the project root:

```
python3 -m scripts.wikidata_division_discover <command> [options]
```

### 1. Discover missing divisions

```
python3 -m scripts.wikidata_division_discover discover Q49210
```

* Q49210 – Wikidata Q-ID of the target university (e.g. New York University).
* Outputs a table of each candidate unit and its match status.
* Writes missing_divisions_<QID>.csv if any units are not yet linked in Wikidata.

#### Options

* --llm MODEL – Override the default OpenAI model (default: gpt-4o).

### 2. Harvest all U.S. universities

```
python3 -m scripts.wikidata_division_discover harvest
```

* Queries Wikidata for every U.S. university (P31/P279 → Q3918 & P17 → Q30).
* Saves raw JSON to universities_us.json.
* Prints a summary table of Q-IDs, labels, and websites.