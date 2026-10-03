"""Split the harvested U.S. university list into the institutions worth a cloud run
and the items a person should look at first.

Usage: python -m wikidata_discover.scripts.filter_universities [LIST_JSON] [--upload]

The harvest query (P31/P279* university, P17 United States) also returns schools and
colleges inside universities, university systems' sub-units, defunct institutions,
and a few things that are not institutions at all. Running discovery on a law
school asks the LLM for "the schools of the law school", which wastes credit and
produces noise. So:

  tier 1  has an IPEDS ID (P1771) and no dissolution date (P576): a post-secondary
          institution the U.S. Department of Education recognizes. These run first.
  review  everything else that is not clearly a sub-unit or defunct: a ROR ID, or a
          university-like class and a website. A person decides what to add.
  drop    dissolved, a sub-unit (P749 or P361 points at another listed item), no
          website, or not a university-like class at all.

Writes results/universities_us_tier1.json (rows [qid, label], the shape the Cloud
Function reads) and results/universities_us_review.csv (every item with its tier and
reason). --upload copies the tier 1 file to the bucket as universities_us_tier1.json.
"""
import csv
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from wikidata_discover.config import RESULTS_DIR

# Classes that mean "a post-secondary institution" for the review tier. Everything
# else (campus, faculty, department, publisher, place, building) is not one.
UNIVERSITY_CLASSES = {
    "Q3918", "Q902104", "Q875538", "Q615150", "Q62078547", "Q15936437", "Q1371037", "Q557206",
    "Q1620945", "Q7840326", "Q189004", "Q1377182", "Q1336920", "Q23002054", "Q23002039", "Q23002052",
    "Q1743327", "Q576603", "Q917182", "Q494230", "Q1663017", "Q3660535", "Q1188663", "Q2385804",
    "Q4671277", "Q383092", "Q1971849", "Q233324", "Q14911880",
}

ATTRIBUTES_SPARQL = """SELECT ?u ?uLabel ?cls ?ipeds ?ror ?website ?dissolved ?parent WHERE {
  VALUES ?u { %s }
  OPTIONAL { ?u wdt:P31 ?cls }
  OPTIONAL { ?u wdt:P1771 ?ipeds }
  OPTIONAL { ?u wdt:P6782 ?ror }
  OPTIONAL { ?u wdt:P856 ?website }
  OPTIONAL { ?u wdt:P576 ?dissolved }
  OPTIONAL { ?u (wdt:P749|wdt:P361) ?parent }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}"""


def classify(item: Dict, listed: Iterable[str]) -> Tuple[str, str]:
    """(tier, reason) for one item. `item` has label, classes (set of QIDs), ipeds,
    ror, website, dissolved (truthy when set), parents (set of QIDs). `listed` is
    every QID in the harvested list, so a parent in it marks a sub-unit."""
    if item.get("dissolved"):
        return "drop", "dissolved"
    if item.get("ipeds"):
        return "tier1", "ipeds"
    if set(item.get("parents", ())) & set(listed):
        return "drop", "unit of a listed institution"
    classes = set(item.get("classes", ()))
    if item.get("ror"):
        return "review", "ror but no ipeds"
    if classes & UNIVERSITY_CLASSES and item.get("website"):
        return "review", "university class, no ipeds or ror"
    if classes & UNIVERSITY_CLASSES:
        return "drop", "no website"
    return "drop", "not a university class"


def fetch_attributes(qids: List[str], chunk: int = 250) -> Dict[str, Dict]:
    """One row per QID with the attributes classify() needs, from Wikidata."""
    from wikidata_discover.sparql_helpers import execute_sparql_bindings
    info: Dict[str, Dict] = {}
    for i in range(0, len(qids), chunk):
        values = " ".join(f"wd:{q}" for q in qids[i:i + chunk])
        for b in execute_sparql_bindings(ATTRIBUTES_SPARQL % values):
            q = b["u"]["value"].rsplit("/", 1)[-1]
            d = info.setdefault(q, {"label": b.get("uLabel", {}).get("value", q), "classes": set(),
                                    "parents": set(), "ipeds": None, "ror": None, "website": None, "dissolved": None})
            if "cls" in b:
                d["classes"].add(b["cls"]["value"].rsplit("/", 1)[-1])
            if "parent" in b:
                d["parents"].add(b["parent"]["value"].rsplit("/", 1)[-1])
            for key in ("ipeds", "ror", "website", "dissolved"):
                if key in b:
                    d[key] = b[key]["value"]
    for q in qids:
        info.setdefault(q, {"label": q, "classes": set(), "parents": set(), "ipeds": None, "ror": None,
                            "website": None, "dissolved": None})
    return info


def list_qids(rows) -> List[str]:
    """QIDs from any of the list shapes we have used, de-duplicated, in order."""
    from wikidata_discover.cloud.collect_function import row_qid
    return list(dict.fromkeys(q for q in (row_qid(r) for r in rows) if q))


def split(info: Dict[str, Dict]) -> Tuple[List[List[str]], List[Dict]]:
    """(tier 1 rows, review rows): tier 1 as [qid, label] sorted by label, and one
    dict per item with qid, label, tier, reason, classes, parents, website."""
    listed = set(info)
    tier1, review = [], []
    for q, d in info.items():
        tier, reason = classify(d, listed)
        review.append({"qid": q, "label": d["label"], "tier": tier, "reason": reason,
                       "classes": " ".join(sorted(d["classes"])), "parents": " ".join(sorted(d["parents"])),
                       "ipeds": d["ipeds"] or "", "website": d["website"] or ""})
        if tier == "tier1":
            tier1.append([q, d["label"]])
    tier1.sort(key=lambda r: r[1].lower())
    review.sort(key=lambda r: ({"tier1": 0, "review": 1, "drop": 2}[r["tier"]], r["label"].lower()))
    return tier1, review


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    upload = "--upload" in argv
    args = [a for a in argv if a != "--upload"]
    src = Path(args[0]) if args else RESULTS_DIR / "universities_us.json"
    rows = json.loads(src.read_text())
    qids = list_qids(rows)
    print(f"{len(qids)} universities in {src}; fetching attributes from Wikidata", flush=True)
    tier1, review = split(fetch_attributes(qids))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / "universities_us_tier1.json"
    out.write_text(json.dumps(tier1, indent=1))
    review_path = RESULTS_DIR / "universities_us_review.csv"
    with review_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(review[0].keys()))
        w.writeheader(); w.writerows(review)
    counts = {}
    for r in review:
        counts[(r["tier"], r["reason"])] = counts.get((r["tier"], r["reason"]), 0) + 1
    for (tier, reason), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"{n:5d}  {tier:7s} {reason}")
    print(f"tier 1: {len(tier1)} -> {out}\nall items with tier and reason -> {review_path}")
    if upload:
        from google.cloud import storage
        from wikidata_discover.batch import BUCKET, PROJECT
        blob = storage.Client(project=PROJECT).bucket(BUCKET).blob(out.name)
        blob.upload_from_filename(str(out))
        print(f"uploaded gs://{BUCKET}/{out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
