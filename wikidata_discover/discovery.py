import json
import logging
import uuid
from typing import List, Dict, Any, Tuple, Optional
from wikidata_discover.sparql_helpers import run_sparql
from wikidata_discover.sparql_helpers import execute_sparql_bindings
from wikidata_discover.wikidata_api import get_entity_label_and_website, quick_wd_search
from wikidata_discover.hierarchy import all_descendants
from wikidata_discover.llm_helpers import LLMHelper
from wikidata_discover.config import LLM_MODEL, console
from wikidata_discover import bq_helpers

from rapidfuzz import fuzz
import re

from rich.table import Table
from pathlib import Path
import pandas as pd

RESULTS_DIR = Path(__file__).parent / "results"
logger = logging.getLogger(__name__)

CHILDREN_SPARQL_TEMPLATE = """
SELECT ?child ?childLabel WHERE {
  VALUES ?parent { wd:%s }
  ?child (wdt:P361|wdt:P355|wdt:P749) ?parent .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

CHILDREN_ALT_LABELS_SPARQL_TEMPLATE = """
SELECT ?child (GROUP_CONCAT(DISTINCT ?alt; separator="|") AS ?altLabels) WHERE {
  VALUES ?parent { wd:%s }
  ?child (wdt:P361|wdt:P355|wdt:P749) ?parent .
  OPTIONAL { ?child skos:altLabel ?alt . FILTER(LANG(?alt)="en") }
}
GROUP BY ?child
"""

ENTITY_INFO_SPARQL = """
SELECT ?label ?website WHERE {
  wd:%s rdfs:label   ?label     . FILTER(LANG(?label)="en")
  OPTIONAL { wd:%s wdt:P856  ?website }
}
"""


class Discovery:
    def __init__(self, university_qid: str):
        self.university_qid = university_qid
        self.university_label, self.university_website = self.fetch_entity_info(university_qid)
        self._children_cache: Dict[str, List[Tuple[str, str]]] = {}
        self._alt_labels_cache: Dict[str, Dict[str, List[str]]] = {}
        self._descendant_qids_cache: Optional[set[str]] = None

    def fetch_entity_info(self, qid: str) -> tuple[str, str | None]:
        """
        Returns (label, website) for the given entity QID.
        Website will be None if there's no P856 claim.
        """
        try:
            bindings = execute_sparql_bindings(
                ENTITY_INFO_SPARQL % (qid, qid)
            )
        except Exception as exc:
            logger.warning("SPARQL entity lookup failed for %s; trying Wikidata API fallback: %s", qid, exc)
            return get_entity_label_and_website(qid)
        if not bindings:
            return get_entity_label_and_website(qid)

        b = bindings[0]
        label = b["label"]["value"]
        website = b.get("website", {}).get("value")  # None if missing
        return label, website

    def fetch_university_info(self) -> tuple[str, str | None]:
        return self.fetch_entity_info(self.university_qid)

    def get_existing_children(self, parent_qid: Optional[str] = None) -> List[Tuple[str, str]]:
        # fetch only direct children (already-linked via P361/P355/P749)
        qid = parent_qid or self.university_qid
        if qid not in self._children_cache:
            try:
                self._children_cache[qid] = run_sparql(
                    CHILDREN_SPARQL_TEMPLATE % qid,
                    as_tuples=True,
                    main_key="child",
                    label_key="childLabel",
                )
            except Exception as exc:
                logger.warning("SPARQL child lookup failed for %s; continuing without direct children: %s", qid, exc)
                self._children_cache[qid] = []
        return self._children_cache[qid]

    def get_children_alt_labels(self, parent_qid: Optional[str] = None) -> Dict[str, List[str]]:
        """Return a dict mapping child QID -> list of English altLabels."""
        qid = parent_qid or self.university_qid
        if qid in self._alt_labels_cache:
            return self._alt_labels_cache[qid]
        try:
            bindings = execute_sparql_bindings(
                CHILDREN_ALT_LABELS_SPARQL_TEMPLATE % qid
            )
        except Exception as exc:
            logger.warning("SPARQL alt-label lookup failed for %s; continuing without alt labels: %s", qid, exc)
            bindings = []
        result: Dict[str, List[str]] = {}
        for b in bindings:
            qid = b["child"]["value"].split("/")[-1]
            raw = b.get("altLabels", {}).get("value", "")
            result[qid] = [a for a in raw.split("|") if a]
        self._alt_labels_cache[parent_qid or self.university_qid] = result
        return result


    def get_all_descendants_qids(self) -> set[str]:
        # fetch every descendant (for filtering deeper nodes)
        edges, _ = all_descendants(self.university_qid)
        return {child for _, child, _, _ in edges}

    def university_descendant_qids(self) -> set[str]:
        """Cached set of every QID reachable under the university's org tree.

        Used to verify that a Wikidata search hit actually belongs to this
        university before we attach a parent link to it. Fails safe: if the
        descendant lookup is unavailable (e.g. SPARQL outage), returns an empty
        set so unverifiable hits are treated as missing rather than linked.
        """
        if self._descendant_qids_cache is None:
            try:
                self._descendant_qids_cache = self.get_all_descendants_qids()
            except Exception as exc:
                logger.warning(
                    "Could not fetch university descendants for orphan verification: %s",
                    exc,
                )
                self._descendant_qids_cache = set()
        return self._descendant_qids_cache

    def find_potential_orphans_for(
        self, candidate_name: str, existing_qids: set
    ) -> List[Tuple[str, str]]:
        """
        Search Wikidata for entities whose English label matches the
        candidate division name and aren't already in existing_qids.
        """
        hits = quick_wd_search(candidate_name)
        return [(qid, label) for qid, label in hits if qid not in existing_qids]


    def discover_missing(self, depth: int = 1, write_bq: bool = True) -> List[Dict[str, Any]]:
        depth = max(1, depth)
        run_id = str(uuid.uuid4())
        run_timestamp = bq_helpers.utc_now_iso()
        console.print(
            f"[bold blue]University:[/bold blue] {self.university_label} ({self.university_qid})"
        )

        tree = self.discover_tree(depth=depth)
        missing = collect_missing(tree)
        counts = count_statuses(tree)

        if missing:
            out_file = Path(f"missing_divisions_{self.university_qid}.csv")
            pd.DataFrame(missing).to_csv(out_file, index=False)
            console.print(
                f"[green]{len(missing)} missing/orphan divisions written to {out_file}.[/green]"
            )
            from wikidata_discover.to_qs_wikidata import export_quickstatements
            export_quickstatements(
                missing,
                self.university_qid,
                self.university_label,
            )
        else:
            console.print(
                "[green]No missing divisions detected - Wikidata seems up to date![/green]"
            )

        reports_dir = RESULTS_DIR / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "run_id": run_id,
            "university_qid": self.university_qid,
            "university_label": self.university_label,
            "model": LLM_MODEL,
            "timestamp": run_timestamp,
            "depth": depth,
            "total_candidates": counts["total_candidates"],
            "exists_linked": counts["exists_linked"],
            "exists_orphan": counts["exists_orphan"],
            "missing": counts["missing"],
        }
        report_path = reports_dir / f"{self.university_qid}_report.json"
        report_path.write_text(json.dumps(report, indent=2))
        console.print(f"[dim]QA report written to {report_path}[/dim]")

        trees_dir = RESULTS_DIR / "trees"
        trees_dir.mkdir(parents=True, exist_ok=True)
        tree_path = trees_dir / f"{self.university_qid}_depth{depth}_tree.json"
        tree_path.write_text(json.dumps(tree, indent=2))
        console.print(f"[dim]Discovery tree written to {tree_path}[/dim]")

        if write_bq:
            units = flatten_discovered_units(tree, run_id, run_timestamp)
            run_saved = bq_helpers.try_save_discovery_run(report)
            units_saved = bq_helpers.try_save_discovered_units(units)
            if run_saved and units_saved:
                console.print(
                    f"[green]Saved discovery run and {len(units)} units to BigQuery.[/green]"
                )
            else:
                console.print("[yellow]BigQuery unavailable; kept local output only.[/yellow]")

        return missing

    def discover_tree(self, depth: int = 1) -> Dict[str, Any]:
        """Discover a hierarchy tree under the configured university."""
        return self._discover_entity(
            parent_qid=self.university_qid,
            parent_label=self.university_label,
            parent_website=self.university_website,
            level=1,
            max_depth=max(1, depth),
            path=[self.university_label],
        )

    def _discover_entity(
        self,
        parent_qid: str,
        parent_label: str,
        parent_website: Optional[str],
        level: int,
        max_depth: int,
        path: List[str],
    ) -> Dict[str, Any]:
        direct_children = self.get_existing_children(parent_qid)
        direct_qids = {qid for qid, _ in direct_children}
        alt_labels_map = self.get_children_alt_labels(parent_qid)
        parent_resolution_choices = self.parent_resolution_choices(parent_qid, direct_children)

        try:
            divisions = LLMHelper.extract_divisions_best_available(
                parent_label,
                parent_website or "",
                level=level,
                parent_context=" > ".join(path[:-1]),
            )
        except ValueError as exc:
            # extract_divisions_best_available raises when no provider returns
            # units. At the top level that signals a real failure (e.g. no API
            # keys configured), so surface it. At deeper levels an entity with
            # no sub-units is a valid leaf, not a failure, so stop recursing here.
            if level == 1:
                raise
            logger.info(
                "%s: level %d extraction returned no sub-units; treating as leaf (%s)",
                parent_qid, level, exc,
            )
            divisions = []
        logger.info(
            "%s: level %d, %d direct children, %d LLM candidates",
            parent_qid, level, len(direct_children), len(divisions),
        )

        table = Table(show_header=True, header_style="bold magenta")
        table.add_column(f"Level {level}: {parent_label}")
        table.add_column("Status")
        table.add_column("Type")

        node: Dict[str, Any] = {
            "qid": parent_qid,
            "label": parent_label,
            "website": parent_website,
            "level": level - 1,
            "path": path,
            "children": [],
        }

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
                matched = LLMHelper.choose_match(name, parent_label, choices)

            # step 3: classify the outcome
            unit_type = normalize_unit_type(division.get("unit_type"), level)
            parent_names = extract_joint_parent_names(division, parent_label)
            additional_parent_qids = resolve_parent_qids(
                parent_names,
                current_parent_qid=parent_qid,
                choices=parent_resolution_choices,
            )
            unresolved_parent_names = [
                parent_name
                for parent_name in parent_names
                if parent_name not in additional_parent_qids["resolved_names"]
            ]
            child_qid = None
            child_label = name
            if matched is None:
                status = "missing"
                display_status = "missing"

            else:
                if matched[0].startswith("ORPHAN:"):
                    candidate_qid = matched[0].split(":", 1)[1]
                    candidate_label = matched[1]
                else:
                    candidate_qid, candidate_label = matched

                if candidate_qid in direct_qids:
                    child_qid = candidate_qid
                    child_label = candidate_label
                    status = "exists_linked"
                    display_status = f"exists_linked -> {child_qid} ({child_label})"
                elif candidate_qid in self.university_descendant_qids():
                    # Already inside this university's org tree but not linked to
                    # this specific parent: a safe orphan to (re)link.
                    child_qid = candidate_qid
                    child_label = candidate_label
                    status = "exists_orphan"
                    display_status = f"exists_orphan -> {child_qid} ({child_label})"
                else:
                    # Matched a global Wikidata search hit we cannot confirm
                    # belongs to this university. Generic labels (e.g. "School of
                    # Medicine") can resolve to another institution's entity, so
                    # do not attach a P749 to it. Treat as a new unit to create.
                    logger.info(
                        "Search hit %s (%s) for '%s' is not within %s's tree; "
                        "treating as missing to avoid cross-institution linking.",
                        candidate_qid, candidate_label, name, self.university_qid,
                    )
                    status = "missing"
                    display_status = "missing"

            child_node = {
                "name": name,
                "label": child_label,
                "qid": child_qid,
                "unit_type": unit_type,
                "website": division.get("website"),
                "reference": division.get("reference"),
                "is_joint": bool(division.get("is_joint") or parent_names),
                "parent_names": parent_names,
                "additional_parent_qids": additional_parent_qids["qids"],
                "unresolved_parent_names": unresolved_parent_names,
                "evidence": division.get("evidence"),
                "location": ", ".join(
                    filter(None, (division.get("city"), division.get("state")))
                ),
                "status": status,
                "parent_qid": parent_qid,
                "parent_label": parent_label,
                "university_qid": self.university_qid,
                "university_label": self.university_label,
                "level": level,
                "path": path + [name],
                "children": [],
            }

            if child_qid and level < max_depth:
                child_website = division.get("website")
                if not child_website:
                    try:
                        _, child_website = self.fetch_entity_info(child_qid)
                    except ValueError:
                        child_website = None
                child_subtree = self._discover_entity(
                    parent_qid=child_qid,
                    parent_label=child_label,
                    parent_website=child_website,
                    level=level + 1,
                    max_depth=max_depth,
                    path=path + [child_label],
                )
                child_node["children"] = child_subtree["children"]

            node["children"].append(child_node)
            table.add_row(name, display_status, unit_type)

        console.print(table)
        return node

    def parent_resolution_choices(
        self,
        parent_qid: str,
        direct_children: List[Tuple[str, str]],
    ) -> List[Tuple[str, str]]:
        choices = list(direct_children)
        if parent_qid != self.university_qid:
            choices.extend(self.get_existing_children(self.university_qid))
        return dedupe_qid_label_pairs(choices)
    
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


def normalize_unit_type(unit_type: Optional[str], level: int = 1) -> str:
    """Normalize LLM unit type labels to a small set used by reports and exports."""
    raw = (unit_type or "").strip().lower()
    aliases = {
        "dept": "department",
        "academic department": "department",
        "research center": "center",
        "centre": "center",
        "research centre": "center",
        "laboratory": "lab",
        "institute": "institute",
        "college": "school",
        "faculty": "school",
    }
    normalized = aliases.get(raw, raw)
    allowed = {
        "school",
        "division",
        "campus",
        "department",
        "program",
        "lab",
        "center",
        "institute",
        "unit",
    }
    if normalized in allowed:
        return normalized
    if level == 1:
        return "school"
    if level == 2:
        return "department"
    return "unit"


def extract_joint_parent_names(division: Dict[str, Any], current_parent_label: str) -> List[str]:
    """Collect additional parent names from LLM output for joint units."""
    raw_values = []
    for field in ("parent_names", "joint_with", "additional_parent_names"):
        value = division.get(field)
        if isinstance(value, list):
            raw_values.extend(value)
        elif value:
            raw_values.extend(re.split(r"[|,;]\s*", str(value)))

    names: List[str] = []
    seen = set()
    current_normalized = normalize_name(current_parent_label)
    for raw in raw_values:
        name = str(raw or "").strip()
        normalized = normalize_name(name)
        if not name or not normalized or normalized == current_normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        names.append(name)
    return names


def resolve_parent_qids(
    parent_names: List[str],
    current_parent_qid: str,
    choices: List[Tuple[str, str]],
) -> Dict[str, Any]:
    """Resolve parent names to known QIDs using direct/fuzzy label matching."""
    qids: List[str] = []
    resolved_names: List[str] = []
    for parent_name in parent_names:
        match = None
        for qid, label in choices:
            if qid == current_parent_qid:
                continue
            if normalize_name(parent_name) == normalize_name(label) or is_fuzzy_match(parent_name, label):
                match = qid
                break
        if match and match not in qids:
            qids.append(match)
            resolved_names.append(parent_name)
    return {"qids": qids, "resolved_names": resolved_names}


def dedupe_qid_label_pairs(items: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    seen = set()
    deduped = []
    for qid, label in items:
        if qid in seen:
            continue
        seen.add(qid)
        deduped.append((qid, label))
    return deduped


def collect_missing(tree: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten missing and orphan candidate nodes from a discovered tree."""
    rows: List[Dict[str, Any]] = []
    for child in tree.get("children", []):
        if child.get("status") in {"missing", "exists_orphan"}:
            rows.append(
                {
                    "name": child.get("name"),
                    "unit_type": child.get("unit_type"),
                    "url": child.get("website") or "",
                    "location": child.get("location") or "",
                    "status": "orphan" if child.get("status") == "exists_orphan" else "missing",
                    "qid": child.get("qid") or "",
                    "parent_qid": child.get("parent_qid"),
                    "parent_label": child.get("parent_label"),
                    "is_joint": child.get("is_joint", False),
                    "parent_names": "|".join(child.get("parent_names") or []),
                    "additional_parent_qids": "|".join(child.get("additional_parent_qids") or []),
                    "unresolved_parent_names": "|".join(child.get("unresolved_parent_names") or []),
                    "evidence": child.get("evidence") or "",
                    "university_qid": child.get("university_qid"),
                    "university_label": child.get("university_label"),
                    "level": child.get("level"),
                    "path": " > ".join(child.get("path", [])),
                }
            )
        rows.extend(collect_missing(child))
    return rows


def flatten_discovered_units(
    tree: Dict[str, Any],
    run_id: str,
    discovered_at: str,
) -> List[Dict[str, Any]]:
    """Flatten every candidate node into rows for BigQuery discovered_units."""
    rows: List[Dict[str, Any]] = []
    for child in tree.get("children", []):
        rows.append(
            {
                "run_id": run_id,
                "university_qid": child.get("university_qid"),
                "university_label": child.get("university_label"),
                "unit_name": child.get("name"),
                "unit_type": child.get("unit_type"),
                "status": child.get("status"),
                "matched_qid": child.get("qid"),
                "website": child.get("website"),
                "location": child.get("location") or "",
                "parent_qid": child.get("parent_qid"),
                "parent_label": child.get("parent_label"),
                "is_joint": child.get("is_joint", False),
                "parent_names": "|".join(child.get("parent_names") or []),
                "additional_parent_qids": "|".join(child.get("additional_parent_qids") or []),
                "unresolved_parent_names": "|".join(child.get("unresolved_parent_names") or []),
                "evidence": child.get("evidence"),
                "level": child.get("level"),
                "path": " > ".join(child.get("path", [])),
                "reference": child.get("reference"),
                "discovered_at": discovered_at,
            }
        )
        rows.extend(flatten_discovered_units(child, run_id, discovered_at))
    return rows


def count_statuses(tree: Dict[str, Any]) -> Dict[str, int]:
    counts = {
        "total_candidates": 0,
        "exists_linked": 0,
        "exists_orphan": 0,
        "missing": 0,
    }
    for child in tree.get("children", []):
        status = child.get("status")
        counts["total_candidates"] += 1
        if status in {"exists_linked", "exists_orphan", "missing"}:
            counts[status] += 1
        child_counts = count_statuses(child)
        for key, value in child_counts.items():
            counts[key] += value
    return counts
