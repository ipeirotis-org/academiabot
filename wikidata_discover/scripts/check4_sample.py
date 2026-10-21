"""Week 3 check 4: draw 20 random "missing" schools from the legacy dry run (Anya).

Usage: python -m wikidata_discover.scripts.check4_sample [CANDIDATES_JSONL] [--out CSV]

The draw is fixed so anyone can repeat it:
- pool: every row with status "missing" in the dry-run candidates.jsonl of legacy_import
- sort rule: the pool is sorted by candidate_id (stable legacy ids) before the draw
- draw: random.Random(SEED).sample(pool, N) with SEED = 20261021 and N = 20

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
from typing import Dict, List

from wikidata_discover import config

SEED = 20261021
N = 20
DEFAULT_CANDIDATES = "wikidata_discover/results/runs/import-legacy-20261021T000404Z-d03c16/candidates.jsonl"
DEFAULT_OUT = "wikidata_discover/results/check4_missing_sample.csv"
URL_COLUMN = "source_url (the LLM website field; us-tier1 kept no other URL)"
COLUMNS = ["sample_no", "school_name", "unit_type_llm", "university_name", "university_qid",
           "search_school_only", "search_university_plus_school", URL_COLUMN, "candidate_id",
           "already on Wikidata (QID)", "really a school (yes/no)"]


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


def load_names() -> Dict[str, str]:
    from google.cloud import storage
    blob = storage.Client().bucket("academiabot").blob("universities_us_tier1.json")
    return dict(json.loads(blob.download_as_text()))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("candidates", nargs="?", default=DEFAULT_CANDIDATES)
    p.add_argument("--out", default=DEFAULT_OUT)
    a = p.parse_args(argv)
    with open(a.candidates) as f:
        cands = [json.loads(line) for line in f if line.strip()]
    rows = sheet_rows(draw(cands), load_names())
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    config.console.print(f"Drew {len(rows)} of {sum(c.get('status') == 'missing' for c in cands)} missing rows "
                         f"with seed {SEED}. Wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
