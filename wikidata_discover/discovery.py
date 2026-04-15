import json
import logging
from typing import List, Dict, Any, Tuple
from wikidata_discover.sparql_helpers import run_sparql
from wikidata_discover.sparql_helpers import execute_sparql_bindings
from wikidata_discover.wikidata_api import quick_wd_search
from wikidata_discover.hierarchy       import all_descendants
from wikidata_discover.llm_helpers import LLMHelper
from wikidata_discover.config import console

from rapidfuzz import fuzz
import re

from rich.table import Table
from pathlib import Path
import pandas as pd

RESULTS_DIR = Path(__file__).parent / "results"
logger = logging.getLogger(__name__)

CHILDREN_SPARQL_TEMPLATE = """
SELECT ?child ?childLabel WHERE {
  VALUES ?univ { wd:%s }
  ?child (wdt:P361|wdt:P355|wdt:P749) ?univ .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

CHILDREN_ALT_LABELS_SPARQL_TEMPLATE = """
SELECT ?child (GROUP_CONCAT(DISTINCT ?alt; separator="|") AS ?altLabels) WHERE {
  VALUES ?univ { wd:%s }
  ?child (wdt:P361|wdt:P355|wdt:P749) ?univ .
  OPTIONAL { ?child skos:altLabel ?alt . FILTER(LANG(?alt)="en") }
}
GROUP BY ?child
"""

UNIV_INFO_SPARQL = """
SELECT ?label ?website WHERE {
  wd:%s rdfs:label   ?label     . FILTER(LANG(?label)="en")
  OPTIONAL { wd:%s wdt:P856  ?website }
}
"""


class Discovery:
    def __init__(self, university_qid: str):
        self.university_qid = university_qid
        self.university_label, self.university_website = self.fetch_university_info()

    def fetch_university_info(self) -> tuple[str, str | None]:
        """
        Returns (label, website) for the given QID.
        Website will be None if there's no P856 claim.
        """
        bindings = execute_sparql_bindings(
            UNIV_INFO_SPARQL % (self.university_qid, self.university_qid)
        )
        if not bindings:
            console.print(f"[red]Could not find info for {self.university_qid}[/red]")
            raise ValueError(f"Info not found for {self.university_qid}")

        b = bindings[0]
        label = b["label"]["value"]
        website = b.get("website", {}).get("value")  # None if missing
        return label, website

    def get_existing_children(self) -> List[Tuple[str, str]]:
        # fetch only direct children (already-linked via P361/P355/P749)
        return run_sparql(CHILDREN_SPARQL_TEMPLATE % self.university_qid, as_tuples=True, main_key="child", label_key="childLabel")

    def get_children_alt_labels(self) -> Dict[str, List[str]]:
        """Return a dict mapping child QID -> list of English altLabels."""
        bindings = execute_sparql_bindings(
            CHILDREN_ALT_LABELS_SPARQL_TEMPLATE % self.university_qid
        )
        result: Dict[str, List[str]] = {}
        for b in bindings:
            qid = b["child"]["value"].split("/")[-1]
            raw = b.get("altLabels", {}).get("value", "")
            result[qid] = [a for a in raw.split("|") if a]
        return result


    def get_all_descendants_qids(self) -> set[str]:
        # fetch every descendant (for filtering deeper nodes)
        edges, _ = all_descendants(self.university_qid)
        return {child for _, child, _, _ in edges}

    def find_potential_orphans_for(
        self, candidate_name: str, existing_qids: set
    ) -> List[Tuple[str, str]]:
        """
        Search Wikidata for entities whose English label matches the
        candidate division name and aren't already in existing_qids.
        """
        hits = quick_wd_search(candidate_name)
        return [(qid, label) for qid, label in hits if qid not in existing_qids]


    def discover_missing(self) -> List[Dict[str, Any]]:
        console.print(
            f"[bold blue]University:[/bold blue] {self.university_label} ({self.university_qid})"
        )

        direct_children = self.get_existing_children()
        direct_qids = {qid for qid, _ in direct_children}
        descendant_qids = self.get_all_descendants_qids()
        alt_labels_map = self.get_children_alt_labels()

        divisions = LLMHelper.extract_divisions_best_available(
            self.university_label, self.university_website
        )
        logger.info(
            "%s: %d direct children, %d LLM candidates",
            self.university_qid, len(direct_children), len(divisions),
        )

        missing: List[Dict[str, Any]] = []
        counts = {"exists_linked": 0, "exists_orphan": 0, "missing": 0}
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Division")
        table.add_column("Status")

        for division in divisions:
            name = division.get("name") or division.get("unit")
            if not name:
                continue

            # step 1: fuzzy match against directly linked children (main label + altLabels)
            matched = None
            for qid, label in direct_children:
                alt_labels = alt_labels_map.get(qid, [])
                all_names = [label] + alt_labels
                if any(normalize_name(name) == normalize_name(n) or is_fuzzy_match(name, n) for n in all_names):
                    matched = (qid, label)
                    logger.debug("fuzzy match: '%s' -> %s (%s)", name, qid, label)
                    break

            # step 2: if no fuzzy match, fall back to LLM with Wikidata search results
            if not matched:
                qsearch_hits = quick_wd_search(name)
                choices = direct_children + [
                    (qid, lbl) for qid, lbl in qsearch_hits if qid not in direct_qids
                ]
                matched = LLMHelper.choose_match(name, self.university_label, choices)

            # step 3: classify the outcome
            if matched is None:
                status = "missing"
                counts["missing"] += 1
                missing.append(
                    {
                        "name": name,
                        "unit_type": division.get("unit_type", "faculty"),
                        "url": division.get("website", ""),
                        "location": ", ".join(
                            filter(None, (division.get("city"), division.get("state")))
                        ),
                        "university_qid": self.university_qid,
                        "university_label": self.university_label,
                        "status": status,
                    }
                )

            elif matched[0].startswith("ORPHAN:"):
                qid = matched[0].split(":", 1)[1]
                status = f"exists_orphan -> {qid}"
                counts["exists_orphan"] += 1
                missing.append(
                    {
                        "name": name,
                        "status": "orphan",
                        "qid": qid,
                        "university_qid": self.university_qid,
                        "university_label": self.university_label,
                    }
                )

            else:
                qid, label = matched
                if qid not in direct_qids and qid in descendant_qids:
                    status = f"exists_orphan -> {qid} ({label})"
                    counts["exists_orphan"] += 1
                    missing.append(
                        {
                            "name": name,
                            "status": "orphan",
                            "qid": qid,
                            "university_qid": self.university_qid,
                            "university_label": self.university_label,
                        }
                    )
                else:
                    status = f"exists_linked -> {qid} ({label})"
                    counts["exists_linked"] += 1

            table.add_row(name, status)

        console.print(table)

        if missing:
            out_file = Path(f"missing_divisions_{self.university_qid}.csv")
            pd.DataFrame(missing).to_csv(out_file, index=False)
            console.print(
                f"[green]{len(missing)} missing divisions written to {out_file}.[/green]"
            )
            #quickstatements export 
            from wikidata_discover.to_qs_wikidata import export_quickstatements
            export_quickstatements(
                missing,
                self.university_qid,
                self.university_label
            )
        else:
            console.print(
                "[green]No missing divisions detected - Wikidata seems up to date![/green]"
            )

        report = {
            "university_qid": self.university_qid,
            "university_label": self.university_label,
            "total_candidates": len(divisions),
            "exists_linked": counts["exists_linked"],
            "exists_orphan": counts["exists_orphan"],
            "missing": counts["missing"],
        }
        reports_dir = RESULTS_DIR / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / f"{self.university_qid}_report.json"
        report_path.write_text(json.dumps(report, indent=2))
        console.print(f"[dim]QA report written to {report_path}[/dim]")

        return missing
    
#helper functions for matching logic
def normalize_name(name: str) -> str:
    """Generic normalizer for academic division names."""
    name = name.lower().strip()
    name = re.sub(r"\b(university|college|school|faculty|institute|center|centre|department|division)\b", 
                  lambda m: f" {m.group(0)} ", name)
    name = re.sub(r"\b(the|of|for|and|at|by)\b", " ", name)
    name = re.sub(r"&", " and ", name)
    name = re.sub(r"\b[a-z]\.? ?[a-z]\.? ", "", name)  # removes "n.", "a. b."
    name = re.sub(r"[^a-z\s]", "", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip()



def is_fuzzy_match(a: str, b: str) -> bool:
    na, nb = normalize_name(a), normalize_name(b)

    if na == nb:
        return True

    # handles word reordering, e.g. "School of Law" vs "Law School"
    if fuzz.token_sort_ratio(na, nb) >= 88:
        return True

    # only use partial_ratio when strings are similar in length, otherwise
    # short strings like "Business" falsely match "Stern School of Business"
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(longer) > 0 and len(shorter) / len(longer) >= 0.5:
        if fuzz.partial_ratio(na, nb) >= 92:
            return True

    return False
