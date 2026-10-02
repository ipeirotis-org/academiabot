# Wikidata Discovery Toolkit

A lightweight Python CLI suite for querying and synchronizing Wikidata entities.  
It provides two core commands:

1. **`harvest`** – Fetch and persist a full list of all U.S. universities (Q-IDs, labels, and websites) from Wikidata to JSON for downstream analysis.
2. **`discover`** – Identify missing top-level academic or administrative units (“divisions”) of a university by combining SPARQL queries, LLM extraction (OpenAI with web search; Anthropic and Gemini as fallbacks without web search), and Wikidata API lookups, then output a CSV of items to add.

See `TASKS.md` for project status and what to work on next, and `AGENTS.md` for the code map and conventions.

---

## Details

- **CSV export** of missing divisions ready for batch Wikidata edits.
- **JSON export** of U.S. universities for offline reuse.
- **Configurable** via environment variables (`.env`):
  - `OPENAI_API_KEY` – Your OpenAI API key (required)
  - `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` – Optional fallbacks. `discover` uses the first provider that returns results (OpenAI first). The multi-provider ensemble with a judge is available in `eval/run_eval.py` and `LLMHelper.extract_divisions_ensemble`, but is not yet wired into `discover`.
  - `LLM_MODEL`, `ANTHROPIC_MODEL`, `GEMINI_MODEL` – Override default models
  - `WD_BOT_USERAGENT` – Custom `User-Agent` for Wikidata/SPARQL requests (defaults to `AcademiaBot/1.0`)
- **Rich** console output and tables for easy debugging.

---

## Usage

The commands share a single entrypoint script. Run them as Python modules from the project root:

```
python -m wikidata_discover.scripts.wikidata_division_discover <command> [options]
```

### 1. Harvest all U.S. universities

```
python -m wikidata_discover.scripts.wikidata_division_discover harvest
```

* Queries Wikidata for every U.S. university (P31/P279 → Q3918 & P17 → Q30).
* Saves raw JSON to universities_us.json.
* Prints a summary table of Q-IDs, labels, and websites.

### 2. Discover missing divisions

```
python -m wikidata_discover.scripts.wikidata_division_discover discover Q49210
```

* Q49210 – Wikidata Q-ID of the target university (e.g. New York University).
* Outputs a table of each candidate unit and its match status.
* Writes missing_divisions_<QID>.csv if any units are not yet linked in Wikidata.

#### Options

* --llm MODEL – Override the default OpenAI model (default: gpt-4o).

### 3. Evaluate extraction quality

```
python -m wikidata_discover.eval.run_eval
python -m wikidata_discover.eval.run_eval --providers openai anthropic --universities Q49210 Q49088
```

Scores each provider and judge combination against the hand-built ground truth in `wikidata_discover/eval/ground_truth.py`.

### Tests

```
python -m pytest tests -q
```
