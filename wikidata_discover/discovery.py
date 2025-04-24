from typing import List, Dict, Any, Tuple
from sparql_helpers import run_sparql
from wikidata_api import quick_wd_search
from llm_helpers import LLMHelper
from config import console
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

UNIV_LABEL_SPARQL = """
SELECT ?label WHERE { wd:%s rdfs:label ?label . FILTER(LANG(?label)="en") }
"""

class Discovery:
    def __init__(self, university_qid: str):
        self.university_qid = university_qid
        self.university_label = self.fetch_university_label()

    def fetch_university_label(self) -> str:
        lbl_res = run_sparql(UNIV_LABEL_SPARQL % self.university_qid)
        if not lbl_res:
            console.print(f"[red]Could not find label for {self.university_qid}[/red]")
            raise ValueError(f"Label not found for {self.university_qid}")
        return lbl_res[0][1]

    def get_existing_children(self) -> List[Tuple[str, str]]:
        return run_sparql(CHILDREN_SPARQL_TEMPLATE % self.university_qid)

    def find_potential_orphans(self, existing_qids: set) -> List[Tuple[str, str]]:
        potential_orphans = quick_wd_search(self.university_label)
        return [(qid, label) for qid, label in potential_orphans if qid not in existing_qids]

    def discover_missing(self) -> List[Dict[str, Any]]:
        console.print(f"[bold blue]University:[/bold blue] {self.university_label} ({self.university_qid})")

        existing_children = self.get_existing_children()
        existing_qids = {qid for qid, _ in existing_children}

        potential_orphans = self.find_potential_orphans(existing_qids)

        gpt_choices = existing_children + potential_orphans
        divisions = LLMHelper.extract_divisions(self.university_label)

        missing: List[Dict[str, Any]] = []
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Division")
        table.add_column("Status")

        for division in divisions:
            name = division.get("name") or division.get("unit")
            if not name:
                continue

            match = LLMHelper.choose_match(name, self.university_label, gpt_choices)

            if match is None:
                status = "missing"
                missing.append({
                    "name": name,
                    "unit_type": division.get("unit_type", "faculty"),
                    "url": division.get("website", ""),
                    "location": ", ".join(filter(None, (division.get("city"), division.get("state")))),
                    "university_qid": self.university_qid,
                    "university_label": self.university_label,
                    "status": status
                })

            elif match[0].startswith("ORPHAN:"):
                qid = match[0].split(":", 1)[1]
                status = f"exists_orphan → {qid}"
                missing.append({
                    "name": name,
                    "status": "orphan",
                    "qid": qid,
                    "university_qid": self.university_qid,
                    "university_label": self.university_label
                })

            else:
                qid, label = match
                status = f"exists_linked → {qid} ({label})"

            table.add_row(name, status)

        console.print(table)

        if missing:
            out_file = Path(f"missing_divisions_{self.university_qid}.csv")
            pd.DataFrame(missing).to_csv(out_file, index=False)
            console.print(f"[green]{len(missing)} missing divisions written to {out_file}.[/green]")
        else:
            console.print("[green]No missing divisions detected – Wikidata seems up‑to‑date![/green]")

        return missing