"""Week 3 check 4: draw 20 random "missing" schools from the legacy dry run (Anya).

Usage: python -m wikidata_discover.scripts.check4_sample [--input CANDIDATES_JSONL] [--out CSV]

The draw is fixed so anyone can repeat it:
- pool: every row with status "missing" in the dry-run candidates.jsonl of legacy_import
- sort rule: the pool is sorted by candidate_id (stable legacy ids) before the draw
- draw: random.Random(SEED).sample(pool, N) with SEED = 20261021 and N = 20

Where the input comes from, in this order:
1. --input, when given.
2. The local dry-run file DEFAULT_CANDIDATES. It is under results/runs/, which git ignores,
   so a fresh checkout does not have it.
3. Its copy in the bucket (uploaded 2026-10-21, same file as the local copy), downloaded
   when the local file is missing:
   gs://academiabot/runs/import-legacy-20261021T000404Z-d03c16/candidates.jsonl
Every input must have 6,413 missing rows, or the script stops before the draw.

A second source gives the same rows: after `legacy_import --write`, BigQuery table
wikidata-academia.academiabot.candidates. Legacy ids are hashes of fixed inputs, so the
rows and candidate_ids are the same. Export
`SELECT * FROM academiabot.candidates WHERE run_id = 'us-tier1' AND legacy` as JSON lines
and pass that file with --input.

University names come from gs://academiabot/universities_us_tier1.json. The script sends
no Wikidata query. The us-tier1 answers had no source URL field, only a website, so the
source_url column holds the website the LLM gave.
"""
import argparse
import csv
import json
import random
import sys
import urllib.parse
from pathlib import Path
from typing import Callable, Dict, List, Optional

from wikidata_discover import config

SEED = 20261021
N = 20
EXPECTED_MISSING = 6413
DEFAULT_CANDIDATES = "wikidata_discover/results/runs/import-legacy-20261021T000404Z-d03c16/candidates.jsonl"
BUCKET_CANDIDATES = "gs://academiabot/runs/import-legacy-20261021T000404Z-d03c16/candidates.jsonl"
DEFAULT_OUT = "wikidata_discover/results/check4_missing_sample.csv"
URL_COLUMN = "source_url (the LLM website field; us-tier1 kept no other URL)"
COLUMNS = ["sample_no", "school_name", "unit_type_llm", "university_name", "university_qid",
           "search_school_only", "search_university_plus_school", URL_COLUMN, "candidate_id",
           "already on Wikidata (QID)", "really a school (yes/no)"]


class InputError(Exception):
    """The input could not be read, or it is not the dry-run file the draw needs."""


def search_link(query: str) -> str:
    """A Wikidata full-text search link (prefix search is what missed these items)."""
    return "https://www.wikidata.org/w/index.php?" + urllib.parse.urlencode({"search": query, "fulltext": "1"})


def draw(candidates: List[dict], seed: int = SEED, n: int = N) -> List[dict]:
    """The missing rows, sorted by candidate_id, then n drawn with the seed."""
    pool = sorted((c for c in candidates if c.get("status") == "missing"), key=lambda c: c["candidate_id"])
    return random.Random(seed).sample(pool, n)


def sheet_rows(sample: List[dict], names: Dict[str, str]) -> List[dict]:
    out = []
    for i, c in enumerate(sample, 1):
        uni = names.get(c["university_qid"], "")
        out.append({"sample_no": i, "school_name": c["name"], "unit_type_llm": c.get("unit_type") or "",
                    "university_name": uni, "university_qid": c["university_qid"],
                    "search_school_only": search_link(c["name"]),
                    "search_university_plus_school": search_link(f"{uni} {c['name']}"),
                    URL_COLUMN: c.get("source_url") or c.get("website") or "",
                    "candidate_id": c["candidate_id"],
                    "already on Wikidata (QID)": "", "really a school (yes/no)": ""})
    return out


def load_candidates(input_path: Optional[str], download: Callable[[str], str],
                    report: Callable[[str], None] = lambda m: None) -> List[dict]:
    """The candidates rows: from --input, else the local file, else the bucket copy.
    Stops with InputError when none can be read or the rows are not the dry run's."""
    if input_path:
        try:
            text = Path(input_path).read_text()
        except OSError as e:
            raise InputError(f"Cannot read --input {input_path}: {e}") from e
        where = input_path
    elif Path(DEFAULT_CANDIDATES).exists():
        text, where = Path(DEFAULT_CANDIDATES).read_text(), DEFAULT_CANDIDATES
    else:
        report(f"No local file at {DEFAULT_CANDIDATES}. Downloading {BUCKET_CANDIDATES}")
        try:
            text = download(BUCKET_CANDIDATES)
        except Exception as e:  # noqa: BLE001 - any failure means the bucket is not reachable
            raise InputError(
                "Cannot find the input of the draw.\n"
                f"  Local file: {DEFAULT_CANDIDATES} (missing)\n"
                f"  Bucket copy: {BUCKET_CANDIDATES} ({type(e).__name__}: {str(e)[:200]})\n"
                "Get a copy of the file, then run the script again with --input <path>.") from e
        where = BUCKET_CANDIDATES
    cands = [json.loads(line) for line in text.splitlines() if line.strip()]
    missing = sum(c.get("status") == "missing" for c in cands)
    if missing != EXPECTED_MISSING:
        raise InputError(f"{where} has {missing} missing rows, not {EXPECTED_MISSING}. "
                         "It is not the input of the check 4 draw, so the draw would differ.")
    return cands


def download_gs(url: str) -> str:
    from google.cloud import storage
    bucket, _, name = url[len("gs://"):].partition("/")
    return storage.Client().bucket(bucket).blob(name).download_as_text(timeout=120)


def load_names() -> Dict[str, str]:
    from google.cloud import storage
    blob = storage.Client().bucket("academiabot").blob("universities_us_tier1.json")
    return dict(json.loads(blob.download_as_text()))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--input", help=f"candidates.jsonl to draw from (default: {DEFAULT_CANDIDATES}, "
                                   f"else {BUCKET_CANDIDATES})")
    p.add_argument("--out", default=DEFAULT_OUT)
    a = p.parse_args(argv)
    try:
        cands = load_candidates(a.input, download_gs, report=config.console.print)
    except InputError as e:
        config.console.print(str(e), markup=False)
        return 1
    rows = sheet_rows(draw(cands), load_names())
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    config.console.print(f"Drew {len(rows)} of {EXPECTED_MISSING} missing rows with seed {SEED}. Wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
