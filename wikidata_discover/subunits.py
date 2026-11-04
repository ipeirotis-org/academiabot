"""subunits <QID>: the direct sub-units of one unit (a university or a school), from an LLM
that searches the web (Anya, week 4).

    python -m wikidata_discover.scripts.wikidata_division_discover subunits Q770467 --provider anthropic

For each unit it writes, under wikidata_discover/results/subunits/:
- <QID>_<provider>.csv: every sub-unit, with name, unit_type, in_scope, website, one
  source_url, a no_url flag, country, country_source (the page or address that shows it), and the fetch
  outcome of the source page and of the country page, and name_on_page (is the sub-unit's
  name in its source page: yes, no, skipped for a page that is not read, empty without a page);
- <QID>_<provider>_departments.csv: the department rows only, the input of Shuo's scorer.
A country source that is a web page is fetched and saved like a source URL; an address is not.
A page is fetched once per run (CitedPages), even when units of several QIDs cite it.
Each sub-unit also adds a candidates row with status "unaligned": it is not yet compared
with Wikidata (alignment is week 6). Offices are kept, with in_scope "no".
"""
import csv
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from wikidata_discover import config, fetcher, llm_helpers, run_log
from wikidata_discover.sparql_helpers import execute_sparql_bindings

logger = logging.getLogger(__name__)

OUT_DIR = config.RESULTS_DIR / "subunits"
COLUMNS = ["name", "unit_type", "in_scope", "website", "source_url", "no_url", "evidence_outcome", "name_on_page", "country",
           "country_source", "country_evidence_outcome", "parent_qid", "parent_label", "provider"]

UNIT_QUERY = """
SELECT ?label ?website ?countryLabel ?parent ?parentLabel ?prop ?isUniversity ?isSystem WHERE {
  BIND(EXISTS { wd:%(q)s wdt:P31/wdt:P279* wd:Q3918 } AS ?isUniversity)
  BIND(EXISTS { wd:%(q)s wdt:P31/wdt:P279* wd:Q20857129 } AS ?isSystem)
  OPTIONAL { wd:%(q)s rdfs:label ?label . FILTER(LANG(?label) = "en") }
  OPTIONAL { wd:%(q)s wdt:P856 ?website }
  OPTIONAL { wd:%(q)s wdt:P17 ?country }
  OPTIONAL { { wd:%(q)s wdt:P749 ?parent . BIND("P749" AS ?prop) } UNION
             { wd:%(q)s wdt:P361 ?parent . BIND("P361" AS ?prop) } }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}"""


def unit_from_bindings(qid: str, bindings: List[Dict[str, str]]) -> Dict[str, Any]:
    """The unit's label, website, country, parent (P749 before P361; parent_qids has every
    parent with that property, two for a joint unit), whether it is a
    university (P31/P279* Q3918), and whether it is a university system (P31/P279*
    Q20857129, which includes state university systems) from the query rows. Pure."""
    def first(key, rows=bindings):
        return next((r[key] for r in rows if r.get(key)), None)
    parents = sorted((r for r in bindings if r.get("parent")), key=lambda r: (r.get("prop") != "P749", r["parent"]))
    parent = parents[0] if parents else {}
    return {"qid": qid, "label": first("label") or qid, "website": first("website"),
            "country": first("countryLabel"),
            "parent_qid": parent.get("parent", "").rsplit("/", 1)[-1] or None,
            "parent_qids": list(dict.fromkeys(r["parent"].rsplit("/", 1)[-1] for r in parents
                                              if r.get("prop") == parent.get("prop"))),
            "parent_label": parent.get("parentLabel"),
            "is_university": first("isUniversity") in ("true", "1", True),
            "is_system": first("isSystem") in ("true", "1", True)}


def flatten(bindings: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """SPARQL JSON rows ({"var": {"type": ..., "value": ...}}) as {"var": value}. Pure."""
    return [{k: v.get("value") for k, v in b.items() if isinstance(v, dict)} for b in bindings]


def fetch_unit(qid: str) -> Dict[str, Any]:
    return unit_from_bindings(qid, flatten(execute_sparql_bindings(UNIT_QUERY % {"q": qid})))


MAX_LEVELS_UP = 6


def find_university(unit: Dict[str, Any], fetch=fetch_unit, max_levels: int = MAX_LEVELS_UP) -> Optional[str]:
    """The root university of a unit: walk up the parents and keep the highest one that is
    a university and not a university system. Wikidata types campuses (NYU Abu Dhabi) as
    universities under their university, so the walk goes past them; it also types systems
    (University of California, University of Texas System, CUNY) as universities, so the
    walk stops at a system and never makes it the root of UC Berkeley or UT Austin. The
    walk also stops at the first parent that is not a university once one was found (an
    association), at a loop, or after max_levels. When only a system was found (a run on
    CUNY itself), the root is that system.

    A joint unit has two parents (NYU Shanghai: NYU and East China Normal University). The
    walk follows each; when they end at different universities the answer is None, as it
    is when no university is found. The run log then leaves university_qid empty instead
    of guessing."""
    roots = _walk_up(unit, fetch, max_levels, {unit["qid"]},
                     best=unit["qid"] if unit.get("is_university") and not unit.get("is_system") else None,
                     nearest=unit["qid"] if unit.get("is_university") else None)
    roots.discard(None)
    return roots.pop() if len(roots) == 1 else None


def _parents_of(u: Dict[str, Any]) -> List[str]:
    return u.get("parent_qids") or ([u["parent_qid"]] if u.get("parent_qid") else [])


def _walk_up(current, fetch, left, seen, best, nearest) -> set:
    """The roots that each parent branch above `current` ends at (see find_university)."""
    parents = [p for p in _parents_of(current) if p not in seen]
    if current.get("is_system") or left <= 0 or not parents:
        return {best or nearest}
    out = set()
    for pq in parents:
        parent = fetch(pq)
        if parent.get("is_university"):
            if parent.get("is_system"):
                out.add(best or nearest or pq)
            else:
                out |= _walk_up(parent, fetch, left - 1, seen | {pq}, pq, nearest or pq)
        elif best is not None or nearest is not None:
            out.add(best or nearest)
        else:
            out |= _walk_up(parent, fetch, left - 1, seen | {pq}, best, nearest)
    return out


def country_url(u: Dict[str, Any]) -> str:
    """The country source when it is a web page, else "" (a street address is not fetched)."""
    src = (u.get("country_source") or "").strip()
    return src if re.match(r"https?://", src, re.I) else ""


def cited_urls(u: Dict[str, Any]) -> List[str]:
    """The pages one unit cites: its source URL, then its country page if that differs."""
    return list(dict.fromkeys(x for x in [u.get("source_url") or "", country_url(u)] if x))


class CitedPages:
    """One map from cited URL to its evidence row for a whole run, shared by every QID of the
    command (and by later levels of a deeper run). A URL is fetched and saved once. A later
    unit that cites it links to the same evidence row, and its name is checked against the
    saved page text. The evidence row's unit_names keeps the names found at fetch time:
    rows are not rewritten, and the run log may already hold the row."""

    def __init__(self, log, fetch=fetcher.fetch):
        self.log, self.fetch = log, fetch
        self.pages: Dict[str, Dict[str, Any]] = {}   # url -> evidence_id, outcome, text, name_check
        self.fetches = 0

    def cite(self, units: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Fetch and save every page these units cite that this run has not fetched yet.
        Returns {url: {"evidence_id", "outcome", "text", "name_check"}} for the pages they cite."""
        citing: Dict[str, List[str]] = {}
        for u in units:
            for url in cited_urls(u):
                citing.setdefault(url, []).append(u["name"])
        for url, names in citing.items():
            if url in self.pages:
                continue
            result = self.fetch(url)
            self.fetches += 1
            read = fetcher.read_result(result)
            eid = fetcher.record(self.log, result, names, read) if self.log is not None else None
            self.pages[url] = {"evidence_id": eid, "outcome": result.outcome, "text": read[0], "name_check": read[1]}
        return {url: self.pages[url] for url in citing}


def name_on_page(name: str, page: Optional[Dict[str, Any]]) -> str:
    """yes or no when the page text was read, skipped when it was not (a PDF), "" without a page. Pure."""
    if not page or page.get("name_check") is None:
        return ""
    if page["name_check"] != "done":
        return "skipped"
    return "yes" if fetcher.name_in_text(name, page.get("text") or "") else "no"


def csv_rows(units: List[Dict[str, Any]], unit: Dict[str, Any], provider: str,
             evidence: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """One CSV row per sub-unit. Pure."""
    evidence = evidence or {}
    return [{"name": u["name"], "unit_type": u["unit_type"], "in_scope": u["in_scope"],
             "website": u.get("website") or "", "source_url": u.get("source_url") or "",
             "no_url": "yes" if u.get("no_url") else "",
             "evidence_outcome": evidence.get(u.get("source_url") or "", {}).get("outcome", ""),
             "name_on_page": name_on_page(u["name"], evidence.get(u.get("source_url") or "")),
             "country": u.get("country") or "",
             "country_source": u.get("country_source") or "",
             "country_evidence_outcome": evidence.get(country_url(u), {}).get("outcome", ""),
             "parent_qid": unit["qid"],
             "parent_label": unit["label"], "provider": provider} for u in units]


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def subunits_for(qid: str, log: Optional[run_log.RunLog], out_dir: Optional[Path] = None,
                 fetch=fetch_unit, fetch_pages: bool = True, fetch_page=fetcher.fetch,
                 pages: Optional[CitedPages] = None, university_qid: Optional[str] = None) -> Dict[str, Any]:
    """Ask for one unit's sub-units, fetch and save each cited page once, write the two
    CSV files, and add a candidates row per sub-unit with its evidence id. `pages` is the
    run's CitedPages, so a page another QID of the run already fetched is reused; without
    it, this call has its own.

    The run log attributes every row to the root university, and each candidates row has
    the immediate parent (qid) as parent_qid. A deeper run passes the university it started
    from as university_qid; without it, find_university walks up Wikidata. Returns a short
    summary, with university_qid for the next level."""
    out_dir = out_dir or OUT_DIR
    unit = fetch(qid)
    if university_qid is None:
        university_qid = find_university(unit, fetch)
        if university_qid is None:
            logger.warning("subunits: no university found above %s; university_qid stays empty", qid)
    if log is not None:
        log.set_university(university_qid)
    units = llm_helpers.LLMHelper.extract_subunits(unit)
    provider = llm_helpers.last_subunit_provider or "unknown"
    if fetch_pages and pages is None:
        pages = CitedPages(log, fetch_page)
    evidence = pages.cite(units) if fetch_pages else {}
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
                              evidence_ids=[e for e in [evidence.get(url, {}).get("evidence_id") for url in cited_urls(u)] if e])
    by_type: Dict[str, int] = {}
    for u in units:
        by_type[u["unit_type"]] = by_type.get(u["unit_type"], 0) + 1
    outcomes: Dict[str, int] = {}
    for e in evidence.values():
        outcomes[e["outcome"]] = outcomes.get(e["outcome"], 0) + 1
    return {"qid": qid, "label": unit["label"], "university_qid": university_qid, "provider": provider,
            "outcome": "ok" if units else "no sub-units", "units": len(units), "by_type": by_type,
            "no_url": sum(1 for u in units if u.get("no_url")), "pages": outcomes, "csv": str(full)}
