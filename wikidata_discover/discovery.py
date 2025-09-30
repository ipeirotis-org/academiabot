from typing import List, Dict, Any, Tuple
from wikidata_discover.sparql_helpers import run_sparql
from wikidata_discover.sparql_helpers import execute_sparql_bindings
from wikidata_discover.wikidata_api import quick_wd_search
from wikidata_discover.hierarchy       import all_descendants
from wikidata_discover.llm_helpers import LLMHelper
from wikidata_discover.config import console

from rich.table import Table
from pathlib import Path
import pandas as pd

CHILDREN_SPARQL_TEMPLATE = """
SELECT ?child ?childLabel WHERE {
  VALUES ?univ { wd:%s }
  ?child (wdt:P361|wdt:P355|wdt:P749) ?univ .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
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
        return run_sparql(CHILDREN_SPARQL_TEMPLATE % self.university_qid)

    def get_all_descendants_qids(self) -> set[str]:
        # fetch every descendant (for filtering deeper nodes)
        edges, _ = all_descendants(self.university_qid)
        return {child for _, child, _, _ in edges}

    def find_potential_orphans_for(
        self, candidate_name: str, existing_qids: set
    ) -> List[Tuple[str, str]]:
        """
        Search Wikidata for entities whose English label matches the
        candidate division name and aren’t already in existing_qids.
        """
        hits = quick_wd_search(candidate_name)
        return [(qid, label) for qid, label in hits if qid not in existing_qids]

    def discover_missing(self) -> List[Dict[str, Any]]:
        console.print(
            f"[bold blue]University:[/bold blue] {self.university_label} ({self.university_qid})"
        )

        # only direct children are “linked”; all descendants used for orphan logic
        direct_children = self.get_existing_children()
        direct_qids = {qid for qid, _ in direct_children}
        descendant_qids = self.get_all_descendants_qids()

        divisions = LLMHelper.extract_divisions(
            self.university_label, self.university_website
        )

        missing: List[Dict[str, Any]] = []
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Division")
        table.add_column("Status")

        for division in divisions:
            name = division.get("name") or division.get("unit")
            if not name:
                continue

            # Only show direct children + per-division search hits to the LLM
            qsearch_hits = quick_wd_search(name)
            choices = direct_children + [
                (qid, lbl) for qid, lbl in qsearch_hits if qid not in direct_qids
            ]
            match = LLMHelper.choose_match(name, self.university_label, choices)

            if match is None:
                status = "missing"
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

            elif match[0].startswith("ORPHAN:"):
                qid = match[0].split(":", 1)[1]
                status = f"exists_orphan → {qid}"
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
                qid, label = match
                # if LLM links to a deep descendant but not a direct child → orphan
                if qid not in direct_qids and qid in descendant_qids:
                    status = f"exists_orphan → {qid} ({label})"
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
                    status = f"exists_linked → {qid} ({label})"

            table.add_row(name, status)

        console.print(table)

        if missing:
            out_file = Path(f"missing_divisions_{self.university_qid}.csv")
            pd.DataFrame(missing).to_csv(out_file, index=False)
            console.print(
                f"[green]{len(missing)} missing divisions written to {out_file}.[/green]"
            )
        else:
            console.print(
                "[green]No missing divisions detected – Wikidata seems up‑to‑date![/green]"
            )

        return missing
