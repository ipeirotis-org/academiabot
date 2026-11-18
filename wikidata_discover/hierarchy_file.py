"""discover --depth 2 (Anya, week 5): one hierarchy file per university.

    python -m wikidata_discover.scripts.wikidata_division_discover discover Q49210 --depth 2
    ... --expand Q770467 Q946028 Q966540 Q13652966     (level 2 for these units only: the pilot)

Steps for one university:

1. Take a fresh snapshot of what Wikidata has below it (week 3), and save it with the run.
2. Level 1: the LLM's sub-units of the university (week 4 step), plus every in-scope unit
   that the snapshot has directly under the university, whether or not the LLM named it.
3. Level 2: the same for each school and campus of level 1. A study-away site
   (data/study_away_sites.json) gets no lookup: "children not looked up (study-away site)".
4. Alignment against the snapshot only (week 5). An LLM unit is matched to a snapshot unit
   under the same parent by website or by name (after the university's and the parent's
   names are removed from the front). A unit that the snapshot does not have is "uncertain",
   with the reason "not in the snapshot; full search in week 6". The full search is week 6.
5. Evidence: each cited page is fetched and saved once per run (CitedPages). A unit only in
   the snapshot gets its P856 website as its source page; without one it is flagged.
6. Node ids from the register (node_register.py), then the file:
   results/hierarchy/<QID>.json and runs/<run_id>/hierarchy/<QID>.json in the bucket, and
   one `nodes` row per node in the run log.

Every function that does not talk to the network is pure, so the tests use fakes.
"""
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from wikidata_discover import config, llm_helpers, node_register, run_log, subunits
from wikidata_discover.discovery import is_fuzzy_match, normalize_name

logger = logging.getLogger(__name__)

OUT_DIR = config.RESULTS_DIR / "hierarchy"
STUDY_AWAY_FILE = Path(__file__).parent / "data" / "study_away_sites.json"

NOT_IN_SNAPSHOT = "not in the snapshot; full search in week 6"
STUDY_AWAY = "children not looked up (study-away site)"
EXPANDABLE = ("school", "campus")
ANCESTOR_PROPS = ("P749", "P361", "P527", "P355", "P199")


# ── snapshot helpers (pure) ──────────────────────────────────────────────

def in_tree_parents(snap: dict, qid: str) -> Dict[str, List[str]]:
    """{parent QID in the tree: [properties]} for one snapshot unit. Pure."""
    out: Dict[str, List[str]] = {}
    for link in snap["links"]:
        if link["child"] == qid and link.get("parent_in_tree", True) and link["parent"] != qid:
            out.setdefault(link["parent"], []).append(link["prop"])
    return out


def _ancestors(snap: dict, qid: str, seen=None) -> set:
    seen = set() if seen is None else seen
    for p in in_tree_parents(snap, qid):
        if p not in seen:
            seen.add(p)
            _ancestors(snap, p, seen)
    return seen


def tree_parents(snap: dict, qid: str) -> List[str]:
    """The most specific parents of a snapshot unit inside the tree. A parent that is an
    ancestor of another parent is dropped (Anthropology has P361 to NYU and to the College
    of Arts and Science: its tree parent is the college). When several remain, the ones
    linked with P749 win; if still several, all are kept (a joint unit). Pure."""
    parents = in_tree_parents(snap, qid)
    if not parents:
        return []
    keep = [p for p in parents if not any(p in _ancestors(snap, other) for other in parents if other != p)]
    p749 = [p for p in keep if "P749" in parents[p]]
    return sorted(p749 or keep)


def p749_parents_outside(snap: dict, qid: str) -> List[str]:
    """Parents outside the tree that the unit names with P749 (NYU Shanghai: East China
    Normal University). Pure."""
    unit = snap["units"].get(qid) or {}
    return sorted({p["parent"] for p in unit.get("parents") or []
                   if p["prop"] == "P749" and p.get("rank") != "deprecated"
                   and not (snap["units"].get(p["parent"]) or {}).get("in_tree")})


def has_p749(snap: dict, child: str, parent: str) -> bool:
    """Wikidata has P749 child -> parent (any rank but deprecated). Pure."""
    unit = snap["units"].get(child) or {}
    return any(p["prop"] == "P749" and p["parent"] == parent and p.get("rank") != "deprecated"
               for p in unit.get("parents") or [])


def snapshot_children(snap: dict, parent_qid: str) -> List[str]:
    """Snapshot units whose tree parents include parent_qid. Pure."""
    return sorted(q for q, u in snap["units"].items()
                  if u.get("in_tree") and q != snap["university_qid"] and parent_qid in tree_parents(snap, q))


def unit_aliases(u: dict) -> List[str]:
    """English aliases of a snapshot unit. Pure."""
    al = u.get("aliases") or {}
    out = []
    for lang, names in (al.items() if isinstance(al, dict) else []):
        if lang == "en" or lang.startswith("en-"):
            out.extend(names if isinstance(names, list) else [names])
    return list(dict.fromkeys(out))


# ── types and scope (pure) ───────────────────────────────────────────────

_OUT_P31 = ("library", "museum", "gallery", "student center", "building", "position", "university press",
            "website", "hospital", "journal", "law review", "periodical", "scholarship", "award")
_OUT_NAME = r"\b(journal|law review|review|scholarship|prize|award|annual survey)\b"


def classify_snapshot_unit(u: dict) -> Tuple[str, str, str]:
    """(unit_type, in_scope, reason) for a unit only Wikidata has, from its label and P31
    labels. Wikidata types are often wrong ("Department of Accounting" is typed business
    school), so the label is read first. Pure."""
    name = (u.get("label") or "").lower()
    p31 = " | ".join((t.get("label") or "").lower() for t in u.get("P31") or [])
    if any(k in p31 for k in _OUT_P31) or name.startswith("president of"):
        return "office", "no", f"P31 is {p31 or 'empty'}: not an academic unit (modeling rules, section 6)"
    if re.search(_OUT_NAME, name) and not re.search(r"\b(school|college|department)\b", name):
        return "other", "no", "label names a journal, a review, a scholarship, or a prize: not a unit"
    if "degree program" in p31 or "academic degree" in p31:
        return "program", "yes", "P31 says degree program"
    if re.search(r"\bdepartment\b", name) or "academic department" in p31:
        return "department", "yes", "label or P31 says department"
    if re.search(r"\b(center|centre|laboratory|lab|project)\b", name):
        return "center", "check", "label names a center, laboratory, or project"
    if re.search(r"\b(school|college|faculty)\b", name) or re.search(r"\b(faculty|school|college)\b", p31):
        return "school", "yes", "label or P31 says school, college, or faculty"
    if "research" in p31 or "institute" in name:
        return "center", "check", "P31 or label says research center or institute"
    if "university" in p31 or "higher education" in p31:
        return "campus", "yes", "typed as a university under the university: a campus"
    return "other", "check", f"P31 is {p31 or 'empty'}: a person decides"


def load_study_away(path: Path = STUDY_AWAY_FILE) -> Dict[str, dict]:
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}


def is_study_away(university_qid: str, name: str, qid: Optional[str], sites: Dict[str, dict]) -> bool:
    """A study-away site of this university, by QID or by name. Pure."""
    entry = sites.get(university_qid) or {}
    if qid and qid in (entry.get("qids") or []):
        return True
    n = node_register.norm_name(name)
    return bool(n) and n in {node_register.norm_name(s) for s in entry.get("sites") or []}


# ── matching LLM units to snapshot units (pure) ──────────────────────────

def strip_prefixes(name: str, prefixes: List[str]) -> str:
    """The name without a leading prefix (the university's or the parent's name or
    abbreviation), longest prefix first. Pure."""
    s = (name or "").strip()
    for p in sorted({p for p in prefixes if p}, key=len, reverse=True):
        m = re.match(re.escape(p) + r"(?:'s)?[\s,:\-–]+(.+)$", s, re.I)
        if m:
            return m.group(1).strip()
    return s


def names_fit(a: str, b: str, prefixes: List[str]) -> bool:
    """Two names for the same unit, after the prefixes are removed. Pure."""
    a2, b2 = strip_prefixes(a, prefixes), strip_prefixes(b, prefixes)
    for x, y in ((a, b), (a2, b2), (a2, b), (a, b2)):
        nx, ny = normalize_name(x), normalize_name(y)
        if nx and nx == ny:
            return True
    return is_fuzzy_match(a2, b2)


def match_units(llm_units: List[dict], snap_units: List[dict], prefixes: List[str]) -> Dict[int, dict]:
    """For each LLM unit (by index), the snapshot unit it is, or why none was chosen.
    A website match decides alone; otherwise the name (or an alias) must fit. Matching is
    one to one: a snapshot unit that two LLM units fit is given to neither. Pure.

    Returns {index: {"qid": QID or None, "method", "reason", "candidates": [...]}}."""
    fits: Dict[int, List[Tuple[str, str]]] = {}
    for i, u in enumerate(llm_units):
        uw = {node_register.norm_url(u.get("website"))} - {""}
        found = []
        for s in snap_units:
            sw = {node_register.norm_url(w) for w in s.get("P856_urls") or []} - {""}
            if uw & sw:
                found.append((s["qid"], "website"))
            elif any(names_fit(u["name"], n, prefixes) for n in [s["label"]] + s.get("aliases_en", [])):
                found.append((s["qid"], "name"))
        by_site = [f for f in found if f[1] == "website"]
        fits[i] = by_site if len(by_site) == 1 else found
    claimed: Dict[str, List[int]] = {}
    for i, f in fits.items():
        if len(f) == 1:
            claimed.setdefault(f[0][0], []).append(i)
    out = {}
    for i, f in fits.items():
        cands = [{"qid": q, "found_by": f"snapshot {how}"} for q, how in f]
        if len(f) == 1 and len(claimed[f[0][0]]) == 1:
            out[i] = {"qid": f[0][0], "method": f"snapshot {f[0][1]}", "candidates": cands,
                      "reason": f"the only snapshot unit under this parent with the same {f[0][1]}"}
        elif len(f) == 1:
            out[i] = {"qid": None, "method": "none", "candidates": cands,
                      "reason": f"{f[0][0]} fits more than one unit of this list; a person decides"}
        elif f:
            out[i] = {"qid": None, "method": "none", "candidates": cands,
                      "reason": "several snapshot units fit: " + ", ".join(q for q, _ in f)}
        else:
            out[i] = {"qid": None, "method": "none", "candidates": [], "reason": NOT_IN_SNAPSHOT}
    return out


# ── nodes (pure) ─────────────────────────────────────────────────────────

VERSION_FIELDS = ("name", "aliases", "description", "unit_type", "parent_ids", "parents_shown", "parent_links",
                  "qid", "country", "alignment", "alignment_candidates", "wikidata_duplicates", "website",
                  "source_url", "evidence")


def node_version(node: dict) -> str:
    """Hash of everything a reviewer is shown (AGENTS.md, "Hierarchy file"). Pure.
    Evidence counts by fact, URL, fetch outcome, and the hash of the page's text (of its
    bytes when it has no text, such as a PDF), not by evidence row id: every fetch adds a
    new row, and the same page must keep the version (and a reviewer's verdict) valid. The
    text, not the bytes: nyu.edu pages carry a new encrypted token in every response."""
    shown = {k: node.get(k) for k in VERSION_FIELDS}
    shown["evidence"] = [{"fact": e.get("fact"), "url": e.get("url"), "outcome": e.get("outcome"),
                          "page": e.get("text_hash") or e.get("content_hash")}
                         for e in node.get("evidence") or []]
    return hashlib.sha256(json.dumps(shown, sort_keys=True, default=str).encode()).hexdigest()[:16]


def description(unit_type: str, parent_name: str) -> str:
    kind = {"school": "school", "department": "academic department", "program": "academic program",
            "center": "research center", "campus": "campus", "office": "office"}.get(unit_type, "unit")
    return f"{kind} of {parent_name}" if parent_name else kind


def parent_links(node: dict, parents: List[dict], snap: dict) -> List[dict]:
    """One entry per parent: does Wikidata have P749 node -> parent? Pure.
    present / missing (P361, P527, P355, or P199 alone counts as missing) / not applicable
    (either side has no QID). Rank and qualifiers are week 7."""
    out = []
    for p in parents:
        if not node.get("qid") or not p.get("qid"):
            status = "not applicable"
        else:
            status = "present" if has_p749(snap, node["qid"], p["qid"]) else "missing"
        out.append({"parent_key": p["key"], "parent_qid": p.get("qid"), "p749": status,
                    "rank": "normal" if len(parents) > 1 else "preferred or normal", "qualifiers": {}})
    return out


def alignment_of(node: dict) -> Tuple[str, str]:
    """(alignment, reason) from the QID and the parent links. Pure."""
    if node.get("is_root"):
        return "root", "the university"
    if not node.get("qid"):
        return "uncertain", node.get("match_reason") or NOT_IN_SNAPSHOT
    links = node.get("parent_links") or []
    if any(l["p749"] == "missing" for l in links):
        return "orphan", "has a QID; P749 to " + ", ".join(
            l["parent_qid"] for l in links if l["p749"] == "missing") + " is missing"
    if any(l["p749"] == "not applicable" for l in links):
        return "pending", "has a QID; waits for a parent that has no QID yet"
    return "linked", "has a QID and P749 to every parent"


# ── building the tree ────────────────────────────────────────────────────

class Deps:
    """Everything that talks to the network. Tests pass fakes."""

    def __init__(self, take_snapshot=None, fetch_unit=None, extract=None, pages=None,
                 countries=None, is_university=None, study_away=None):
        from wikidata_discover import snapshot as snapmod
        self.take_snapshot = take_snapshot or snapmod.take_snapshot
        self.fetch_unit = fetch_unit or subunits.fetch_unit
        self.extract = extract or _extract_with_provider
        self.pages = pages
        self.countries = countries or wikidata_countries
        self.is_university = is_university or (lambda q: bool(subunits.fetch_unit(q).get("is_university")))
        self.study_away = load_study_away() if study_away is None else study_away


def _extract_with_provider(unit: dict) -> Tuple[List[dict], str, List[str]]:
    units = llm_helpers.LLMHelper.extract_subunits(unit)
    return units, llm_helpers.last_subunit_provider or "unknown", list(llm_helpers.last_subunit_call_ids)


COUNTRY_QUERY = """SELECT ?item ?country ?countryLabel WHERE {
  VALUES ?item { %s }
  ?item wdt:P17 ?country .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}"""


def wikidata_countries(qids: List[str]) -> Dict[str, dict]:
    """P17 of each QID: {qid: {"qid", "label"}}, 40 per query."""
    from wikidata_discover.sparql_helpers import execute_sparql_bindings
    out: Dict[str, dict] = {}
    qids = sorted(set(qids))
    for i in range(0, len(qids), 40):
        rows = subunits.flatten(execute_sparql_bindings(COUNTRY_QUERY % " ".join(f"wd:{q}" for q in qids[i:i + 40])))
        for r in rows:
            q = r["item"].rsplit("/", 1)[-1]
            out.setdefault(q, {"qid": r["country"].rsplit("/", 1)[-1], "label": r.get("countryLabel")})
    return out


def _llm_node(u: dict, key: str, level: int, parent_key: str, provider: str, call_ids: List[str]) -> dict:
    return {"key": key, "level": level, "origin": "llm", "name": u["name"], "aliases": [],
            "unit_type": u["unit_type"], "type_source": "llm", "in_scope": u["in_scope"],
            "parent_keys": [parent_key], "qid": None, "website": u.get("website"),
            "source_url": u.get("source_url"),
            "country": {"label": u.get("country"), "qid": None, "source": u.get("country_source")},
            "provider": provider, "llm_call_ids": call_ids, "flags": [], "children_lookup": "",
            "raw_unit": u}


def _snap_node(s: dict, key: str, level: int, parent_keys: List[str]) -> dict:
    t, scope, why = classify_snapshot_unit(s)
    sites = [w for w in s.get("P856_urls") or [] if w]
    return {"key": key, "level": level, "origin": "snapshot", "name": s["label"], "aliases": s.get("aliases_en", []),
            "unit_type": t, "type_source": f"wikidata ({why})", "in_scope": scope,
            "parent_keys": parent_keys, "qid": s["qid"], "website": sites[0] if sites else None,
            "source_url": sites[0] if sites else None, "country": {"label": None, "qid": None, "source": None},
            "provider": None, "llm_call_ids": [], "flags": [], "children_lookup": "",
            "match_method": "snapshot only", "match_reason": "in the Wikidata snapshot; no LLM named it",
            "alignment_candidates": [{"qid": s["qid"], "found_by": "snapshot", "chosen": True}]}


def _snap_view(snap: dict, qid: str) -> dict:
    u = snap["units"][qid]
    return {"qid": qid, "label": u.get("label") or qid, "aliases_en": unit_aliases(u),
            "P856_urls": [e.get("value") or e.get("url") or e.get("label") if isinstance(e, dict) else e
                          for e in u.get("P856") or []],
            "P31": u.get("P31") or []}


def elsewhere_matches(name: str, snap: dict, exclude_parent: Optional[str], prefixes: List[str]) -> List[dict]:
    """Snapshot units in the tree, under any other parent, whose name fits. Recorded as
    candidates for week 6; never chosen in week 5 (two schools can each have a
    "Department of Economics"). Pure."""
    out = []
    # The unit's own parent and that parent's ancestors are never "the same unit elsewhere":
    # with the parent's name removed, "Department of Mathematics" would fit Courant itself.
    above = ({exclude_parent} | _ancestors(snap, exclude_parent)) if exclude_parent else set()
    for q, u in snap["units"].items():
        if not u.get("in_tree") or q == snap["university_qid"] or q in above:
            continue
        tp = tree_parents(snap, q)
        if exclude_parent and exclude_parent in tp:
            continue
        pre = prefixes + [(snap["units"].get(p) or {}).get("label") or "" for p in tp]
        if any(names_fit(name, n, pre) for n in [u.get("label") or ""] + unit_aliases(u)):
            out.append({"qid": q, "found_by": "snapshot name, under another parent (" + (", ".join(tp) or "none") + ")",
                        "chosen": False})
    return out


def build_level(parent: dict, llm_units: List[dict], provider: str, call_ids: List[str], snap: dict,
                prefixes: List[str], level: int, seen_qids: set) -> List[dict]:
    """The nodes of one level under one parent: LLM units (matched or not) and the
    snapshot units under that parent that no LLM unit matched. Pure."""
    snap_kids = [_snap_view(snap, q) for q in snapshot_children(snap, parent["qid"])] if parent.get("qid") else []
    matches = match_units(llm_units, snap_kids, prefixes)
    nodes, matched = [], set()
    for i, u in enumerate(llm_units):
        n = _llm_node(u, f"{parent['key']}/{i}", level, parent["key"], provider, call_ids)
        m = matches[i]
        n["alignment_candidates"] = [{**c, "chosen": c["qid"] == m["qid"]} for c in m["candidates"]]
        n["match_method"], n["match_reason"] = m["method"], m["reason"]
        if m["qid"]:
            s = snap["units"][m["qid"]]
            n["qid"], n["origin"] = m["qid"], "llm and snapshot"
            n["aliases"] = [a for a in [s.get("label")] + unit_aliases(s) if a and a != u["name"]]
            matched.add(m["qid"])
        else:
            other = elsewhere_matches(u["name"], snap, parent.get("qid"), prefixes)
            if other:
                n["alignment_candidates"] += other
                if m["reason"] == NOT_IN_SNAPSHOT:
                    n["match_reason"] = (f"{NOT_IN_SNAPSHOT}. The snapshot has a unit with this name under another "
                                         "parent: " + ", ".join(c["qid"] for c in other))
        nodes.append(n)
    for s in snap_kids:
        if s["qid"] in matched or s["qid"] in seen_qids:
            continue
        nodes.append(_snap_node(s, f"{parent['key']}/{s['qid']}", level, [parent["key"]]))
    return nodes


def build_tree(root_qid: str, depth: int, deps: Deps, expand: Optional[List[str]] = None,
               log=None) -> dict:
    """Snapshot, levels 1 and 2, alignment, evidence. Returns the tree without node ids:
    {"snapshot", "root", "nodes" (parent first), "left_out", "lookups"}."""
    snap = deps.take_snapshot(root_qid)
    root_unit = deps.fetch_unit(root_qid)
    root_snap = snap["units"].get(root_qid) or {}
    root = {"key": "root", "is_root": True, "level": 0, "origin": "snapshot", "name": root_unit["label"],
            "aliases": unit_aliases(root_snap), "unit_type": "university", "type_source": "wikidata",
            "in_scope": "yes", "parent_keys": [], "qid": root_qid, "website": root_unit.get("website"),
            "source_url": root_unit.get("website"), "country": {"label": root_unit.get("country"), "qid": None,
                                                                "source": f"wikidata:{root_qid}"},
            "provider": None, "llm_call_ids": [], "flags": [], "children_lookup": "looked up",
            "alignment_candidates": []}
    univ_prefixes = [root["name"]] + root["aliases"]
    lookups, left_out = [], []

    def lookup(unit_dict: dict, who: str) -> Tuple[List[dict], str, List[str], Optional[str]]:
        try:
            units, provider, ids = deps.extract(unit_dict)
            lookups.append({"unit": who, "outcome": "ok" if units else "no sub-units", "units": len(units),
                            "provider": provider})
            return units, provider, ids, None
        except Exception as e:  # noqa: BLE001 - one failed lookup must not stop the file
            err = f"{type(e).__name__}: {str(e)[:200]}"
            lookups.append({"unit": who, "outcome": "failed", "error": err})
            if isinstance(e, llm_helpers.LLMDeadline):
                raise
            return [], "none", [], err

    units1, prov1, ids1, err1 = lookup(root_unit, root_qid)
    if err1:
        root["children_lookup"] = f"lookup failed: {err1}"
    level1 = build_level(root, units1, prov1, ids1, snap, univ_prefixes, 1, set())
    nodes = [root]
    seen_qids = {n["qid"] for n in level1 if n.get("qid")}
    expand_set = {e.lower() for e in expand or []}
    for n in level1:
        if n["in_scope"] == "no":
            left_out.append({"name": n["name"], "qid": n.get("qid"), "level": 1, "origin": n["origin"],
                             "reason": f"out of scope: {n['unit_type']}" + (f" ({n['type_source']})" if n["origin"] == "snapshot" else "")})
            continue
        nodes.append(n)
    level1 = [n for n in nodes if n.get("level") == 1]

    for n in level1:
        if is_study_away(root_qid, n["name"], n.get("qid"), deps.study_away) or any(
                is_study_away(root_qid, a, None, deps.study_away) for a in n.get("aliases") or []):
            n["children_lookup"] = STUDY_AWAY
            n["unit_type"] = "campus"
            continue
        if depth < 2:
            n["children_lookup"] = "not looked up (depth 1)"
            continue
        if n["unit_type"] not in EXPANDABLE or n["in_scope"] != "yes":
            n["children_lookup"] = f"not looked up (a {n['unit_type']}; depth 2 looks below schools and campuses only)"
            continue
        kids, prov, ids = [], "", []
        if expand_set and not ({(n.get("qid") or "").lower(), n["name"].lower()} & expand_set):
            n["children_lookup"] = "not looked up (not named in --expand)"
        else:
            unit_dict = deps.fetch_unit(n["qid"]) if n.get("qid") else {
                "qid": None, "label": n["name"], "parent_label": root["name"], "website": n.get("website"),
                "country": n["country"].get("label")}
            kids, prov, ids, err = lookup(unit_dict, n.get("qid") or n["name"])
            n["children_lookup"] = f"lookup failed: {err}" if err else ("looked up" if kids else "looked up: no sub-units")
            n["children_provider"] = prov
        prefixes = univ_prefixes + [n["name"]] + n.get("aliases", [])
        level2 = build_level(n, kids, prov, ids, snap, prefixes, 2, seen_qids)
        for c in level2:
            if c.get("qid"):
                seen_qids.add(c["qid"])
            if c["in_scope"] == "no":
                left_out.append({"name": c["name"], "qid": c.get("qid"), "level": 2, "origin": c["origin"],
                                 "parent": n["name"], "reason": f"out of scope: {c['unit_type']}"})
                continue
            c["children_lookup"] = "not looked up (depth 2)"
            nodes.append(c)

    # Snapshot units the walk did not place (deeper than level 2, or under a unit left out).
    placed = {n.get("qid") for n in nodes} | {l.get("qid") for l in left_out}
    for q, u in snap["units"].items():
        if u.get("in_tree") and q not in placed:
            left_out.append({"name": u.get("label"), "qid": q, "level": u.get("depth"), "origin": "snapshot",
                             "reason": f"not placed at level 1 or 2 (snapshot depth {u.get('depth')}, "
                                       f"tree parents {', '.join(tree_parents(snap, q)) or 'none'})"})

    _second_parents(nodes, snap, deps)
    _evidence(nodes, deps, root_qid)
    return {"snapshot": snap, "root": root, "nodes": nodes, "left_out": left_out, "lookups": lookups}


def _second_parents(nodes: List[dict], snap: dict, deps: Deps) -> None:
    """A unit with P749 to a university outside the tree (NYU Shanghai -> East China Normal
    University) keeps that parent too, as "<QID>/root": that university's own root node."""
    for n in nodes:
        if n.get("is_root") or not n.get("qid"):
            continue
        for q in p749_parents_outside(snap, n["qid"]):
            if deps.is_university(q):
                n["parent_keys"].append(f"{q}/root")
                label = (snap["units"].get(q) or {}).get("label") or q
                n.setdefault("external_parents", {})[f"{q}/root"] = {"qid": q, "name": label}


def _evidence(nodes: List[dict], deps: Deps, root_qid: str) -> None:
    """Fetch and save every cited page once, and the country of every unit only Wikidata has."""
    if deps.pages is not None:
        cites = [{"name": n["name"], "source_url": n.get("source_url"),
                  "country_source": (n.get("country") or {}).get("source")} for n in nodes]
        got = deps.pages.cite(cites)
    else:
        got = {}
    need_country = [n["qid"] for n in nodes if n.get("qid") and not (n.get("country") or {}).get("label")]
    countries = deps.countries(need_country) if need_country else {}
    for n in nodes:
        src = n.get("source_url") or ""
        page = got.get(src)
        n["evidence"] = []
        if page:
            n["evidence"].append({"fact": "unit", "url": src, "evidence_id": page.get("evidence_id"),
                                  "outcome": page.get("outcome"), "content_hash": page.get("content_hash"),
                                  "text_hash": page.get("text_hash"),
                                  "name_on_page": subunits.name_on_page(n["name"], page)})
        c = n.get("country") or {}
        cpage = got.get(subunits.country_url({"country_source": c.get("source")}))
        if cpage:
            n["evidence"].append({"fact": "country", "url": c.get("source"), "evidence_id": cpage.get("evidence_id"),
                                  "outcome": cpage.get("outcome"), "content_hash": cpage.get("content_hash"),
                                  "text_hash": cpage.get("text_hash")})
        if not c.get("label") and n.get("qid") in countries:
            n["country"] = {**countries[n["qid"]], "source": f"wikidata:{n['qid']} (P17)"}
        if not src:
            n["flags"].append("no source URL" if n["origin"] != "snapshot" else
                              "no source page: no P856 on Wikidata, and the page search is not built yet")
        elif page and page.get("outcome") != "ok":
            n["flags"].append(f"source page not saved: {page.get('outcome')}")
        if not (n.get("country") or {}).get("label"):
            n["flags"].append("no country")


# ── ids, file, rows ──────────────────────────────────────────────────────

def register_units(nodes: List[dict]) -> List[dict]:
    return [{"key": n["key"], "qid": n.get("qid"), "name": n["name"], "aliases": n.get("aliases") or [],
             "parent_keys": n["parent_keys"], "website": n.get("website"), "source_url": n.get("source_url"),
             "is_root": bool(n.get("is_root"))} for n in nodes]


def finish_nodes(tree: dict, ids: Dict[str, str], flags: Dict[str, List[str]], run_id: str,
                 invocation_id: str) -> List[dict]:
    """Node ids, parent ids, parent links, alignment, version: the file's node list. Pure."""
    by_key = {n["key"]: n for n in tree["nodes"]}
    snap = tree["snapshot"]
    out = []
    for n in tree["nodes"]:
        parents = []
        for k in n["parent_keys"]:
            if k in by_key:
                p = by_key[k]
                parents.append({"key": ids[k], "qid": p.get("qid"), "name": p["name"]})
            else:
                ext = (n.get("external_parents") or {}).get(k, {})
                parents.append({"key": k, "qid": ext.get("qid"), "name": ext.get("name")})
        node = {
            "node_id": ids[n["key"]], "level": n["level"], "name": n["name"], "aliases": n.get("aliases") or [],
            "unit_type": n["unit_type"], "type_source": n.get("type_source"), "in_scope": n["in_scope"],
            "origin": n["origin"],
            "parent_ids": [p["key"] for p in parents],
            "parents_shown": [{"node_id": p["key"], "name": p["name"], "qid": p["qid"]} for p in parents],
            "qid": n.get("qid"), "country": n.get("country"), "website": n.get("website"),
            "source_url": n.get("source_url"),
            "description": "" if n.get("is_root") else description(n["unit_type"], parents[0]["name"] if parents else ""),
            "alignment_candidates": n.get("alignment_candidates") or [],
            "wikidata_duplicates": [], "evidence": n.get("evidence") or [],
            "evidence_ids": [e["evidence_id"] for e in n.get("evidence") or [] if e.get("evidence_id")],
            "children_lookup": n.get("children_lookup", ""),
            "flags": (n.get("flags") or []) + flags.get(n["key"], []),
            "provider": n.get("provider"), "llm_call_ids": n.get("llm_call_ids") or [],
            "candidate_ids": n.get("candidate_ids") or [], "run_id": run_id, "invocation_id": invocation_id,
            "is_root": bool(n.get("is_root")), "match_reason": n.get("match_reason"),
        }
        node["parent_links"] = [{**l, "parent_id": l.pop("parent_key")} for l in parent_links(node, [
            {"key": p["key"], "qid": p["qid"]} for p in parents], snap)]
        node["alignment"], node["alignment_reason"] = alignment_of(node)
        node.pop("is_root")
        node.pop("match_reason")
        node["node_version"] = node_version(node)
        out.append(node)
    return out


def add_candidates(log, tree: dict) -> None:
    """One candidates row per LLM-proposed unit, with its alignment status."""
    status_of = {"linked": "linked", "orphan": "orphan", "pending": "pending", "uncertain": "uncertain"}
    by_key = {n["key"]: n for n in tree["nodes"]}
    for n in tree["nodes"]:
        if n["origin"] == "snapshot" or n.get("is_root"):
            continue
        parent = by_key.get(n["parent_keys"][0]) if n["parent_keys"] else None
        tmp = {"qid": n.get("qid"), "parent_links": parent_links(
            {"qid": n.get("qid")}, [{"key": parent["key"], "qid": parent.get("qid")}] if parent else [],
            tree["snapshot"]), "match_reason": n.get("match_reason")}
        status = status_of[alignment_of(tmp)[0]]
        n["candidate_ids"] = [log.add_candidate(
            parent_qid=(parent or {}).get("qid"),
            parent_candidate_id=((parent or {}).get("candidate_ids") or [None])[0] if parent and not parent.get("qid") else None,
            name=n["name"], unit_type=n["unit_type"], status=status, matched_qid=n.get("qid"),
            match_method="fuzzy" if n.get("qid") else "none",
            match_choices=n.get("alignment_candidates"), source_url=n.get("source_url"),
            website=n.get("website"), provider=n.get("provider"), llm_call_ids=n.get("llm_call_ids"),
            evidence_ids=[e["evidence_id"] for e in n.get("evidence") or [] if e.get("evidence_id")])]


def summary(nodes: List[dict], tree: dict) -> dict:
    """Counts for the console, the runs row, and the midpoint report. Pure."""
    def count(key):
        out: Dict[str, int] = {}
        for n in nodes:
            out[str(n.get(key))] = out.get(str(n.get(key)), 0) + 1
        return out
    with_page = sum(1 for n in nodes if any(e.get("fact") == "unit" and e.get("outcome") == "ok" for e in n["evidence"]))
    return {"nodes": len(nodes), "by_level": count("level"), "by_type": count("unit_type"),
            "by_alignment": count("alignment"), "by_origin": count("origin"),
            "with_saved_page": with_page, "with_flags": sum(1 for n in nodes if n["flags"]),
            "left_out": len(tree["left_out"]), "lookups": tree["lookups"]}


def hierarchy_document(root_qid: str, nodes: List[dict], tree: dict, run_id: str, invocation_id: str,
                       register_info: dict) -> dict:
    return {"university_qid": root_qid, "university_label": tree["root"]["name"], "run_id": run_id,
            "invocation_id": invocation_id, "format": "hierarchy-file-week5",
            "snapshot": {"taken_at": tree["snapshot"].get("taken_at"), "counts": tree["snapshot"].get("counts"),
                         "path": tree.get("snapshot_path")},
            "register": register_info, "summary": summary(nodes, tree), "nodes": nodes,
            "left_out": tree["left_out"]}


NODE_ROW_FIELDS = ("node_id", "level", "name", "aliases", "unit_type", "type_source", "in_scope", "origin",
                   "parent_ids", "parent_links", "qid", "country", "node_version", "alignment",
                   "alignment_reason", "alignment_candidates", "wikidata_duplicates", "website", "description",
                   "source_url", "evidence_ids", "evidence", "children_lookup", "flags", "candidate_ids",
                   "llm_call_ids", "provider")


def node_row(university_qid: str, node: dict) -> dict:
    return {"university_qid": university_qid, **{k: node.get(k) for k in NODE_ROW_FIELDS}}


def run_university(root_qid: str, depth: int, log, store: node_register.RegisterStore, deps: Deps,
                   expand: Optional[List[str]] = None, out_dir: Optional[Path] = None) -> dict:
    """Build, assign ids, and write one university's hierarchy file. Returns the summary."""
    out_dir = out_dir or OUT_DIR
    log.set_university(root_qid)
    # Load the register first: if the bucket is unreachable, stop before spending on LLM calls.
    store.load(root_qid)
    cross, cross_gen = store.load_json(node_register.CROSS_REGISTER_NAME, {"pairs": []})
    tree = build_tree(root_qid, depth, deps, expand, log)
    tree["snapshot_path"] = log.save_text(f"hierarchy/{root_qid}/snapshot.json",
                                          json.dumps(tree["snapshot"], indent=1, sort_keys=True, ensure_ascii=False),
                                          "application/json")
    add_candidates(log, tree)
    ids, flags, reg, loaded, gen, new_gen = node_register.assign_and_save(
        store, root_qid, register_units(tree["nodes"]), log.run_id)
    loaded_path = log.save_text(f"hierarchy/{root_qid}/ids.loaded.json",
                                json.dumps(loaded, indent=1, sort_keys=True), "application/json")
    cross_path = log.save_text("hierarchy/cross_register.loaded.json", json.dumps(cross, indent=1, sort_keys=True),
                               "application/json")
    nodes = finish_nodes(tree, ids, flags, log.run_id, log.invocation_id)
    reg_info = {"path": f"gs://{config.RUNLOG_BUCKET}/" + node_register.REGISTER_NAME.format(qid=root_qid),
                "loaded_generation": gen, "saved_generation": new_gen, "loaded_copy": loaded_path,
                "cross_register_generation": cross_gen, "cross_register_copy": cross_path}
    doc = hierarchy_document(root_qid, nodes, tree, log.run_id, log.invocation_id, reg_info)
    text = json.dumps(doc, indent=2, ensure_ascii=False, default=str)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{root_qid}.json").write_text(text)
    run_copy = config.RESULTS_DIR / "runs" / log.run_id / "hierarchy" / f"{root_qid}.json"
    run_copy.parent.mkdir(parents=True, exist_ok=True)
    run_copy.write_text(text)
    bucket_path = log.save_text(f"hierarchy/{root_qid}.json", text, "application/json")
    for n in nodes:
        log.add_node(node_row(root_qid, n))
    return {"qid": root_qid, "label": tree["root"]["name"], "file": str(out_dir / f"{root_qid}.json"),
            "bucket_path": bucket_path, **doc["summary"]}
