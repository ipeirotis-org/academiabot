import json
import logging
import uuid
from typing import List, Dict, Any, Tuple, Optional, Iterable
from urllib.parse import urlparse
from wikidata_discover.sparql_helpers import run_sparql
from wikidata_discover.sparql_helpers import execute_sparql_bindings
from wikidata_discover.wikidata_api import (
    get_entity_label_and_website,
    get_entity_parent_qids,
    quick_wd_search,
)
from wikidata_discover.hierarchy import all_descendants
from wikidata_discover.llm_helpers import LLMHelper
from wikidata_discover import config as _config
from wikidata_discover.config import console
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
  { ?child (wdt:P361|wdt:P749) ?parent . }
  UNION
  { ?parent (wdt:P355|wdt:P527|wdt:P199) ?child . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

CHILDREN_ALT_LABELS_SPARQL_TEMPLATE = """
SELECT ?child (GROUP_CONCAT(DISTINCT ?alt; separator="|") AS ?altLabels) WHERE {
  VALUES ?parent { wd:%s }
  { ?child (wdt:P361|wdt:P749) ?parent . }
  UNION
  { ?parent (wdt:P355|wdt:P527|wdt:P199) ?child . }
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

# Units that declare this entity as a subordinate from the parent side via a
# downward hierarchy edge (P355 has subsidiary, P527 has part, P199 business
# division). hierarchy.py crawls these as parent->child links, so a unit can sit
# under an organization that only states the relationship from its own side,
# with no child-side P749/P361. Used for best-effort joint-parent enrichment and
# kept symmetric with the parent->child branch of the children queries.
PARENT_DOWNWARD_SPARQL_TEMPLATE = """
SELECT ?parent WHERE {
  ?parent (wdt:P355|wdt:P527|wdt:P199) wd:%s .
}
"""


class Discovery:
    def __init__(self, university_qid: str):
        self.university_qid = university_qid
        self.university_label, self.university_website = self.fetch_entity_info(university_qid)
        self._children_cache: Dict[str, List[Tuple[str, str]]] = {}
        self._alt_labels_cache: Dict[str, Dict[str, List[str]]] = {}
        self._entity_parents_cache: Dict[str, Optional[set]] = {}
        self._entity_website_cache: Dict[str, Optional[str]] = {}
        self._downward_parents_cache: Dict[str, set] = {}

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
        # fetch only direct children: child-side P361/P749 or parent-side
        # downward links P355/P527/P199 (the hierarchy edges hierarchy.py crawls)
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
                # A failed lookup is NOT the same as "no children". Treating it as
                # empty would classify already-linked units as missing and emit
                # CREATE QuickStatements, producing duplicate Wikidata entities.
                # Fail loudly instead so the run does not corrupt data from an
                # unverified empty state (e.g. a SPARQL timeout or rate-limit).
                raise RuntimeError(
                    f"Could not load existing children for {qid}; aborting to avoid "
                    f"classifying linked units as missing. Cause: {exc}"
                ) from exc
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

    def _existing_parent_qids(self, qid: str) -> Optional[set]:
        """Cached lookup of an entity's existing parent QIDs (P749/P361).

        Returns the set of parents, or None if the lookup could not be performed
        (so callers treat it as unverifiable and fail safe). Uses the Wikidata
        Action API, which keeps working when the SPARQL endpoint is unavailable.
        """
        if qid not in self._entity_parents_cache:
            try:
                self._entity_parents_cache[qid] = get_entity_parent_qids(qid)
            except Exception as exc:
                logger.warning(
                    "Could not fetch existing parents for %s; treating as "
                    "unverifiable: %s",
                    qid, exc,
                )
                self._entity_parents_cache[qid] = None
        return self._entity_parents_cache[qid]

    def _downward_parent_qids(self, qid: str) -> set:
        """Best-effort lookup of units that declare this entity as a subordinate.

        Reads parent-side downward edges (P355 has subsidiary, P527 has part,
        P199 business division). Complements _existing_parent_qids() (which reads
        only child-side P749/P361) for joint-parent enrichment, so siblings under
        a parent that declares the relationship only from its own side are still
        reachable. Used for enrichment only, never for classification.
        Best-effort: a lookup failure returns an empty set and never aborts the
        run.
        """
        if qid not in self._downward_parents_cache:
            try:
                bindings = execute_sparql_bindings(PARENT_DOWNWARD_SPARQL_TEMPLATE % qid)
                self._downward_parents_cache[qid] = {
                    b["parent"]["value"].split("/")[-1] for b in bindings
                }
            except Exception as exc:
                logger.debug("Downward-parent lookup for %s failed: %s", qid, exc)
                self._downward_parents_cache[qid] = set()
        return self._downward_parents_cache[qid]

    def _entity_website(self, qid: str) -> Optional[str]:
        """Cached, best-effort lookup of an entity's official website (P856)."""
        if qid not in self._entity_website_cache:
            try:
                _, website = get_entity_label_and_website(qid)
            except Exception as exc:
                logger.debug("Could not fetch website for %s: %s", qid, exc)
                website = None
            self._entity_website_cache[qid] = website
        return self._entity_website_cache[qid]

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

        out_file = Path(f"missing_divisions_{self.university_qid}.csv")
        qs_file = Path(f"quickstatements_{self.university_qid}.qs")
        if missing:
            pd.DataFrame(missing).to_csv(out_file, index=False)
            console.print(
                f"[green]{len(missing)} missing/orphan divisions written to {out_file}.[/green]"
            )
            from wikidata_discover.to_qs_wikidata import export_quickstatements
            export_quickstatements(
                missing,
                self.university_qid,
                self.university_label,
                out_path=qs_file,
            )
        else:
            console.print(
                "[green]No missing divisions detected - Wikidata seems up to date![/green]"
            )
            # Remove stale per-QID outputs from an earlier run so a later
            # 'qs-batch --no-bq' does not re-emit statements for a university
            # that no longer has anything missing.
            for stale in (out_file, qs_file):
                try:
                    stale.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    logger.warning("Could not remove stale output %s: %s", stale, exc)

        reports_dir = RESULTS_DIR / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "run_id": run_id,
            "university_qid": self.university_qid,
            "university_label": self.university_label,
            "model": model_for_provider(tree.get("extraction_provider")),
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
            # Persist the unit rows first and only write the discovery_runs
            # marker once they are durable. get_processed_qids() and
            # get_coverage_summary() key on discovery_runs, so a run row written
            # without its units would make a later resume skip this university or
            # report it with no unit rows.
            units_saved = bq_helpers.try_save_discovered_units(units)
            run_saved = units_saved and bq_helpers.try_save_discovery_run(report)
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

        # extract_divisions_best_available returns (units, provider). A valid but
        # empty list means a genuine leaf (no sub-units); it raises only when
        # every provider was unavailable or failed. We let that raise propagate
        # rather than treating an extraction failure as an empty leaf, which
        # would silently write an incomplete tree.
        divisions, extraction_provider = LLMHelper.extract_divisions_best_available(
            parent_label,
            parent_website or "",
            level=level,
            parent_context=" > ".join(path[:-1]),
        )
        logger.info(
            "%s: level %d, %d direct children, %d LLM candidates (provider=%s)",
            parent_qid, level, len(direct_children), len(divisions), extraction_provider,
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
            "extraction_provider": extraction_provider,
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
            existing_parent_qids: List[str] = []
            if matched is None:
                status = "missing"
                display_status = "missing"

            else:
                if matched[0].startswith("ORPHAN:"):
                    candidate_qid = matched[0].split(":", 1)[1]
                    candidate_label = matched[1]
                else:
                    candidate_qid, candidate_label = matched

                # Only attach a P749 to an existing entity when there is evidence
                # it belongs here: it is already under this parent, or it is a
                # cross-listed unit that already exists under one of its claimed
                # joint parents. Otherwise create a new unit rather than risk
                # linking another unit's or institution's entity.
                if candidate_qid in direct_qids:
                    existing_parents: Optional[set] = direct_qids
                    institution_confirmed = True
                else:
                    existing_parents = self._existing_parent_qids(candidate_qid)
                    # Non-name evidence: does the candidate's website sit on the
                    # university's domain, or on the current parent's own domain?
                    # A school/department may have its own domain at deeper
                    # levels, so matching either confirms the entity belongs here
                    # and lets us safely adopt a disconnected orphan.
                    candidate_website = self._entity_website(candidate_qid)
                    institution_confirmed = same_registrable_domain(
                        self.university_website, candidate_website
                    ) or same_registrable_domain(parent_website, candidate_website)

                status = classify_search_match(
                    candidate_qid,
                    parent_qid,
                    direct_qids,
                    existing_parents,
                    joint_parent_qids=additional_parent_qids["qids"],
                    institution_confirmed=institution_confirmed,
                )
                if status in ("exists_linked", "exists_orphan"):
                    child_qid = candidate_qid
                    child_label = candidate_label
                    display_status = f"{status} -> {child_qid} ({child_label})"
                    # The entity's actual current parents (P749/P361), so the
                    # export only emits P749 for parents it does not already have
                    # and never re-links an existing joint parent. Cached lookup.
                    real_parents = self._existing_parent_qids(candidate_qid)
                    existing_parent_qids = sorted(real_parents) if real_parents else []
                else:
                    logger.info(
                        "Search hit %s (%s) for '%s' is parented elsewhere or "
                        "unverifiable; treating as missing to avoid an incorrect "
                        "P749 under %s.",
                        candidate_qid, candidate_label, name, parent_qid,
                    )
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
                "existing_parent_qids": existing_parent_qids,
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
                    # Website is optional metadata for the recursive call; a
                    # transient SPARQL/API failure here must not abort the whole
                    # --depth run, so swallow any error and recurse without it.
                    try:
                        _, child_website = self.fetch_entity_info(child_qid)
                    except Exception as exc:
                        logger.debug("Optional website fetch for %s failed: %s", child_qid, exc)
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
            # Enrichment for resolving joint/cross-listed parents. Besides the
            # university's top-level units, include sibling units that share a
            # parent with the current unit (e.g. at level 3 the current parent is
            # a department, so its siblings are the other departments under the
            # same school). Without these, a joint program whose other parent is
            # a sibling department cannot resolve that QID. Best-effort: a lookup
            # failure here must not abort the run.
            enrichment_parents = {self.university_qid}
            enrichment_parents.update(self._existing_parent_qids(parent_qid) or set())
            # Also include parents that declare this unit only from the parent
            # side via a downward edge (P355/P527/P199), so sibling units under
            # such a parent are available for joint-parent matching even when the
            # child-side P749/P361 link is absent. Best-effort and
            # enrichment-only; never affects classification.
            enrichment_parents.update(self._downward_parent_qids(parent_qid))
            for enrichment_qid in enrichment_parents:
                try:
                    choices.extend(self.get_existing_children(enrichment_qid))
                except Exception as exc:
                    logger.debug(
                        "joint-parent enrichment: children lookup for %s failed: %s",
                        enrichment_qid, exc,
                    )
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


def model_for_provider(provider: Optional[str]) -> str:
    """Map the provider that handled extraction to its configured model id.

    Reads the config module dynamically so a CLI --llm override (which mutates
    config.LLM_MODEL after import) is reflected, and so fallback to Anthropic or
    Gemini records that provider's model rather than the OpenAI default.
    """
    return {
        "openai": _config.LLM_MODEL,
        "anthropic": _config.ANTHROPIC_MODEL,
        "gemini": _config.GEMINI_MODEL,
    }.get(provider, _config.LLM_MODEL)


# Multi-label public suffixes seen for academic and other institutions outside
# the U.S. For hosts under these, the registrable domain needs the label before
# the suffix (e.g. "ox.ac.uk", not "ac.uk"); otherwise unrelated institutions
# would collapse to the same domain. Not exhaustive: a complete public-suffix
# list would need an extra dependency, and this project primarily targets U.S.
# .edu institutions where the last-two-labels heuristic is already correct.
_COMPOUND_PUBLIC_SUFFIXES = frozenset({
    "ac.uk", "ac.nz", "ac.jp", "ac.kr", "ac.in", "ac.za", "ac.il",
    "ac.at", "ac.be", "ac.th", "ac.ir", "ac.id",
    "edu.au", "edu.cn", "edu.sg", "edu.hk", "edu.in", "edu.my", "edu.tr",
    "edu.mx", "edu.br", "edu.co", "edu.pk", "edu.tw", "edu.sa", "edu.eg",
    "edu.ph", "edu.pl", "edu.gr", "edu.ar",
    "co.uk", "com.au", "co.jp", "co.nz", "co.in", "co.za",
})


def registrable_domain(url: Optional[str]) -> Optional[str]:
    """Return the registrable domain of a URL (e.g. 'nyu.edu' for any nyu.edu host).

    Uses the last two labels, falling back to the last three when those two form
    a known compound public suffix (e.g. 'ac.uk', 'edu.au') so that unrelated
    institutions under the same suffix are not treated as one domain. Returns
    None if no host can be parsed, or if the host is itself only a bare public
    suffix with no registrable part.
    """
    if not url:
        return None
    parsed = urlparse(url if "://" in url else "//" + url)
    host = (parsed.hostname or "").lower()
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return labels[0] if labels else None
    last_two = ".".join(labels[-2:])
    if last_two in _COMPOUND_PUBLIC_SUFFIXES:
        # Need the label before the compound suffix to identify the institution.
        # A bare public suffix (e.g. just "ac.uk") has no registrable part.
        return ".".join(labels[-3:]) if len(labels) >= 3 else None
    return last_two


def same_registrable_domain(a: Optional[str], b: Optional[str]) -> bool:
    """True if both URLs share a registrable domain (e.g. www.nyu.edu vs med.nyu.edu)."""
    da, db = registrable_domain(a), registrable_domain(b)
    return bool(da and db and da == db)


def classify_search_match(
    candidate_qid: str,
    current_parent_qid: str,
    direct_qids: set,
    existing_parents: Optional[set],
    joint_parent_qids: Iterable[str] = (),
    institution_confirmed: bool = False,
) -> str:
    """Decide the status for a Wikidata match that is not a direct child.

    existing_parents is the candidate's current set of parent QIDs (P749/P361),
    or None if that lookup could not be performed. joint_parent_qids are the
    other parents the LLM claims this unit is cross-listed under (already
    resolved to QIDs). institution_confirmed is True when non-name evidence
    (e.g. the candidate's website is on the university's domain) shows the
    entity belongs to this institution. Returns "exists_linked",
    "exists_orphan", or "missing".

    We only attach a P749 to an existing entity when there is positive evidence
    it belongs here:
      - already a direct child of this parent -> exists_linked
      - already linked to this parent via P749/P361 -> exists_linked
      - a cross-listed unit that already exists under one of its claimed joint
        parents -> exists_orphan, so we add the current parent to that entity
        instead of creating a duplicate
      - an unparented entity confirmed to belong to this institution -> a real
        disconnected orphan, exists_orphan, so we adopt it rather than create a
        duplicate
      - everything else (unparented but unconfirmed, parented under an unrelated
        unit, or an unverifiable/failed lookup) -> missing, so we create a new
        unit rather than risk attaching another institution's or unit's entity
    """
    if candidate_qid in direct_qids:
        return "exists_linked"
    if existing_parents is None:
        return "missing"
    if current_parent_qid in existing_parents:
        return "exists_linked"
    if joint_parent_qids and (set(existing_parents) & set(joint_parent_qids)):
        return "exists_orphan"
    if not existing_parents and institution_confirmed:
        return "exists_orphan"
    return "missing"


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
            # Split only on pipe/semicolon, not comma: parent labels commonly
            # contain commas (e.g. "College of Arts, Media and Design"), so
            # comma-splitting would break a name before resolve_parent_qids()
            # can match it and drop the joint P749 parent.
            raw_values.extend(re.split(r"[|;]\s*", str(value)))

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
        status = child.get("status")
        additional = child.get("additional_parent_qids") or []
        # Export missing units (to create), orphans (to link), and already-linked
        # units that are cross-listed (to add their remaining joint parents).
        if status in {"missing", "exists_orphan"} or (status == "exists_linked" and additional):
            if status == "exists_orphan":
                export_status = "orphan"
            elif status == "exists_linked":
                export_status = "linked_joint"
            else:
                export_status = "missing"
            rows.append(
                {
                    "name": child.get("name"),
                    "unit_type": child.get("unit_type"),
                    "url": child.get("website") or "",
                    "location": child.get("location") or "",
                    "status": export_status,
                    "qid": child.get("qid") or "",
                    "parent_qid": child.get("parent_qid"),
                    "parent_label": child.get("parent_label"),
                    "is_joint": child.get("is_joint", False),
                    "parent_names": "|".join(child.get("parent_names") or []),
                    "additional_parent_qids": "|".join(child.get("additional_parent_qids") or []),
                    "existing_parent_qids": "|".join(child.get("existing_parent_qids") or []),
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
                "existing_parent_qids": "|".join(child.get("existing_parent_qids") or []),
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
