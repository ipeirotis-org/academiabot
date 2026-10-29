"""subunits <QID>: the direct sub-units of one unit (a university or a school), from an LLM
that searches the web (Anya, week 4).

    python -m wikidata_discover.scripts.wikidata_division_discover subunits Q770467 --provider anthropic

For each unit it writes, under wikidata_discover/results/subunits/:
- <QID>_<provider>.csv: every sub-unit, with name, unit_type, in_scope, website, one
  source_url, a no_url flag, country, and country_source (the page or address that shows it);
- <QID>_<provider>_departments.csv: the department rows only, the input of Shuo's scorer.
Each sub-unit also adds a candidates row with status "unaligned": it is not yet compared
with Wikidata (alignment is week 6). Offices are kept, with in_scope "no".
"""
import csv
from pathlib import Path
from typing import Any, Dict, List, Optional

from wikidata_discover import config, fetcher, llm_helpers, run_log
from wikidata_discover.sparql_helpers import execute_sparql_bindings

OUT_DIR = config.RESULTS_DIR / "subunits"
COLUMNS = ["name", "unit_type", "in_scope", "website", "source_url", "no_url", "evidence_outcome", "country",
           "country_source", "parent_qid", "parent_label", "provider"]

UNIT_QUERY = """
SELECT ?label ?website ?countryLabel ?parent ?parentLabel ?prop WHERE {
  OPTIONAL { wd:%(q)s rdfs:label ?label . FILTER(LANG(?label) = "en") }
  OPTIONAL { wd:%(q)s wdt:P856 ?website }
  OPTIONAL { wd:%(q)s wdt:P17 ?country }
  OPTIONAL { { wd:%(q)s wdt:P749 ?parent . BIND("P749" AS ?prop) } UNION
             { wd:%(q)s wdt:P361 ?parent . BIND("P361" AS ?prop) } }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}"""


def unit_from_bindings(qid: str, bindings: List[Dict[str, str]]) -> Dict[str, Any]:
    """The unit's label, website, country, and parent (P749 before P361) from the query
    rows. Pure."""
    def first(key, rows=bindings):
        return next((r[key] for r in rows if r.get(key)), None)
    parents = sorted((r for r in bindings if r.get("parent")), key=lambda r: (r.get("prop") != "P749", r["parent"]))
    parent = parents[0] if parents else {}
    return {"qid": qid, "label": first("label") or qid, "website": first("website"),
            "country": first("countryLabel"),
            "parent_qid": parent.get("parent", "").rsplit("/", 1)[-1] or None,
            "parent_label": parent.get("parentLabel")}


def flatten(bindings: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """SPARQL JSON rows ({"var": {"type": ..., "value": ...}}) as {"var": value}. Pure."""
    return [{k: v.get("value") for k, v in b.items() if isinstance(v, dict)} for b in bindings]


def fetch_unit(qid: str) -> Dict[str, Any]:
    return unit_from_bindings(qid, flatten(execute_sparql_bindings(UNIT_QUERY % {"q": qid})))


def fetch_cited(units: List[Dict[str, Any]], log, fetch=fetcher.fetch) -> Dict[str, Dict[str, Any]]:
    """Fetch every cited source URL once and save it as an evidence row. Returns
    {url: {"evidence_id", "outcome"}}. Units that cite the same page share one fetch."""
    citing: Dict[str, List[str]] = {}
    for u in units:
        if u.get("source_url"):
            citing.setdefault(u["source_url"], []).append(u["name"])
    out = {}
    for url, names in citing.items():
        result = fetch(url)
        eid = fetcher.record(log, result, names) if log is not None else None
        out[url] = {"evidence_id": eid, "outcome": result.outcome}
    return out


def csv_rows(units: List[Dict[str, Any]], unit: Dict[str, Any], provider: str,
             evidence: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """One CSV row per sub-unit. Pure."""
    evidence = evidence or {}
    return [{"name": u["name"], "unit_type": u["unit_type"], "in_scope": u["in_scope"],
             "website": u.get("website") or "", "source_url": u.get("source_url") or "",
             "no_url": "yes" if u.get("no_url") else "",
             "evidence_outcome": evidence.get(u.get("source_url") or "", {}).get("outcome", ""),
             "country": u.get("country") or "",
             "country_source": u.get("country_source") or "", "parent_qid": unit["qid"],
             "parent_label": unit["label"], "provider": provider} for u in units]


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def subunits_for(qid: str, log: Optional[run_log.RunLog], out_dir: Optional[Path] = None,
                 fetch=fetch_unit, fetch_pages: bool = True, fetch_page=fetcher.fetch) -> Dict[str, Any]:
    """Ask for one unit's sub-units, fetch and save each cited page once, write the two
    CSV files, and add a candidates row per sub-unit with its evidence id. Returns a
    short summary."""
    out_dir = out_dir or OUT_DIR
    unit = fetch(qid)
    if log is not None:
        log.set_university(unit.get("parent_qid") or qid)
    units = llm_helpers.LLMHelper.extract_subunits(unit)
    provider = llm_helpers.last_subunit_provider or "unknown"
    evidence = fetch_cited(units, log, fetch_page) if fetch_pages else {}
    rows = csv_rows(units, unit, provider, evidence)
    full = out_dir / f"{qid}_{provider}.csv"
    write_csv(full, rows)
    write_csv(out_dir / f"{qid}_{provider}_departments.csv", [r for r in rows if r["unit_type"] == "department"])
    if log is not None:
        for u in units:
            log.add_candidate(run_id=log.run_id, invocation_id=log.invocation_id, parent_qid=qid,
                              name=u["name"], unit_type=u["unit_type"], status="unaligned", match_method="none",
                              source_url=u.get("source_url"), website=u.get("website"), provider=provider,
                              llm_call_ids=list(llm_helpers.last_subunit_call_ids),
                              evidence_ids=[e for e in [evidence.get(u.get("source_url") or "", {}).get("evidence_id")] if e])
    by_type: Dict[str, int] = {}
    for u in units:
        by_type[u["unit_type"]] = by_type.get(u["unit_type"], 0) + 1
    outcomes: Dict[str, int] = {}
    for e in evidence.values():
        outcomes[e["outcome"]] = outcomes.get(e["outcome"], 0) + 1
    return {"qid": qid, "label": unit["label"], "provider": provider, "units": len(units), "by_type": by_type,
            "no_url": sum(1 for u in units if u.get("no_url")), "pages": outcomes, "csv": str(full)}
