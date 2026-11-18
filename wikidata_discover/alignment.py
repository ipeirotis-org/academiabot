"""Alignment with every search (Anya, week 6): never call a unit "new" when Wikidata has it.

For one unit, the searches run in this fixed order (also the order of the cumulative rows
in the A1 report):

1. prefix:          label prefix search (EntitySearch, the old method)
2. fulltext:        full-text search on the unit's name
3. university_name: full-text search on the university's name plus the unit's name
4. other_names:     prefix and full-text search on other names: the unit's aliases, and
                    the core of its name ("Department of Finance" -> "Finance Department")
5. website:         items whose P856 is the unit's website (exact URL, http/https, www or not)
6. tree:            items already in the university's tree (the snapshot) whose name fits,
                    under any parent

Every search is a read-only SPARQL request through sparql_helpers (a pause between
requests, retries with backoff, and a wait on HTTP 429). The action API (wbsearchentities)
is not used, because it answers 429 to shared cloud IPs. Every search response is saved
with the run, so a replay sees the same candidates.

Then an LLM decides (llm_helpers): it picks one item from the list or none ("choose", the
default), or answers yes or no for each item ("yes-no", behind --decide yes-no, for
research question A1). A unit is "new" only when every search ran and the decision is
none. A search that failed makes the unit "uncertain", never "new".
"""
import hashlib
import json
import logging
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from wikidata_discover import llm_helpers, node_register
from wikidata_discover.sparql_helpers import execute_sparql_bindings

logger = logging.getLogger(__name__)

SEARCH_KINDS = ("prefix", "fulltext", "university_name", "other_names", "website", "tree")
DECIDE_MODES = ("choose", "yes-no")
PER_SEARCH = 7            # results kept from one search request
MAX_CANDIDATES = 12       # items shown to the LLM for one unit
SKIP_P31 = {"Q4167836": "Wikimedia category", "Q4167410": "disambiguation page", "Q5": "human",
            "Q13442814": "scholarly article", "Q11266439": "Wikimedia template", "Q17633526": "Wikinews article"}


# ── queries (pure) ───────────────────────────────────────────────────────

def _lit(s: str) -> str:
    return json.dumps(s or "")          # a SPARQL string literal with escapes


def prefix_query(text: str, limit: int = PER_SEARCH) -> str:
    return ("SELECT ?item WHERE { SERVICE wikibase:mwapi { bd:serviceParam wikibase:endpoint \"www.wikidata.org\"; "
            f"wikibase:api \"EntitySearch\"; mwapi:search {_lit(text)}; mwapi:language \"en\"; mwapi:limit \"{limit}\". "
            "?item wikibase:apiOutputItem mwapi:item. } }")


def fulltext_query(text: str, limit: int = PER_SEARCH) -> str:
    return ("SELECT ?item WHERE { SERVICE wikibase:mwapi { bd:serviceParam wikibase:endpoint \"www.wikidata.org\"; "
            f"wikibase:api \"Search\"; mwapi:srsearch {_lit(text)}; mwapi:srlimit \"{limit}\". "
            "?item wikibase:apiOutputItem mwapi:title. } }")


def website_variants(url: Optional[str]) -> List[str]:
    """The exact P856 values a site can have: http or https, with or without www and a
    trailing slash. Empty for anything that is not an http(s) URL. Pure."""
    n = node_register.norm_url(url)
    if not n:
        return []
    host, _, path = n.partition("/")
    path = ("/" + path) if path else ""
    out = []
    for scheme in ("https", "http"):
        for h in (host, "www." + host):
            for tail in ("", "/"):
                out.append(f"{scheme}://{h}{path}{tail}")
    return list(dict.fromkeys(out))


def website_query(url: str) -> Optional[str]:
    vs = website_variants(url)
    if not vs:
        return None
    values = " ".join(f"<{v}>" for v in vs if re.fullmatch(r"[^<>\"{}|\\^`\s]+", v))
    return f"SELECT ?item WHERE {{ VALUES ?site {{ {values} }} ?item wdt:P856 ?site . }}"


def details_query(qids: Iterable[str]) -> str:
    values = " ".join(f"wd:{q}" for q in qids)
    return f"""SELECT ?item ?label ?desc ?alias ?p31 ?p31Label ?parent ?parentLabel ?site ?countryLabel WHERE {{
  VALUES ?item {{ {values} }}
  OPTIONAL {{ ?item rdfs:label ?label . FILTER(LANG(?label) = "en") }}
  OPTIONAL {{ ?item schema:description ?desc . FILTER(LANG(?desc) = "en") }}
  OPTIONAL {{ ?item skos:altLabel ?alias . FILTER(LANG(?alias) = "en") }}
  OPTIONAL {{ ?item wdt:P31 ?p31 . }}
  OPTIONAL {{ {{ ?item wdt:P749 ?parent }} UNION {{ ?item wdt:P361 ?parent }} }}
  OPTIONAL {{ ?item wdt:P856 ?site . }}
  OPTIONAL {{ ?item wdt:P17 ?country . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}"""


def _qid(uri: Optional[str]) -> Optional[str]:
    q = (uri or "").rsplit("/", 1)[-1]
    return q if re.fullmatch(r"Q\d+", q) else None


def parse_items(bindings: List[dict]) -> List[str]:
    """QIDs from search results, in order, without repeats. Pure."""
    out = []
    for b in bindings:
        q = _qid((b.get("item") or {}).get("value"))
        if q and q not in out:
            out.append(q)
    return out


def parse_details(bindings: List[dict]) -> Dict[str, dict]:
    """{qid: label, description, aliases, p31 (labels), p31_qids, parents (labels),
    parent_qids, websites, countries}. Pure."""
    out: Dict[str, dict] = {}
    for b in bindings:
        q = _qid((b.get("item") or {}).get("value"))
        if not q:
            continue
        d = out.setdefault(q, {"qid": q, "label": None, "description": None, "aliases": [], "p31": [],
                               "p31_qids": [], "parents": [], "parent_qids": [], "websites": [], "countries": []})
        v = lambda k: (b.get(k) or {}).get("value")
        d["label"] = d["label"] or v("label")
        d["description"] = d["description"] or v("desc")
        for key, val in (("aliases", v("alias")), ("p31", v("p31Label")), ("p31_qids", _qid(v("p31"))),
                         ("parents", v("parentLabel")), ("parent_qids", _qid(v("parent"))),
                         ("websites", v("site")), ("countries", v("countryLabel"))):
            if val and val not in d[key]:
                d[key].append(val)
    return out


# ── names to search (pure) ───────────────────────────────────────────────

_GENERIC = r"(department|school|college|faculty|division|program|programme|institute|center|centre)"


def other_names(name: str, aliases: List[str], parent_name: str = "") -> List[str]:
    """Other names to search for: the aliases, then the name turned around ("Department of
    Finance" -> "Finance Department"), then the core alone when it is long enough to be
    specific. At most 4, none equal to the name. Pure."""
    out = list(aliases or [])
    m = re.match(rf"^{_GENERIC}\s+(?:of|for|in)\s+(?:the\s+)?(.+)$", name.strip(), re.I)
    if m:
        kind, core = m.group(1), m.group(2)
        out.append(f"{core} {kind.title()}")
        if len(core.split()) >= 2:
            out.append(core)
    m = re.match(rf"^(.+?)\s+{_GENERIC}$", name.strip(), re.I)
    if m:
        out.append(f"{m.group(2).title()} of {m.group(1)}")
    seen, result = {node_register.norm_name(name)}, []
    for x in out:
        n = node_register.norm_name(x)
        if n and n not in seen:
            seen.add(n)
            result.append(x)
    return result[:4]


# ── running the searches ─────────────────────────────────────────────────

class Searcher:
    """Runs the searches for units, through one query function, and keeps every response
    (saved with the run when a run log is given, and reused within the run)."""

    def __init__(self, sparql: Callable[[str], List[dict]] = execute_sparql_bindings, log=None):
        self.sparql, self.log = sparql, log
        self.responses: Dict[str, List[dict]] = {}
        self.saved_paths: Dict[str, str] = {}
        self.queries = 0

    def ask(self, query: str) -> List[dict]:
        if query in self.responses:
            return self.responses[query]
        self.queries += 1
        rows = self.sparql(query)
        self.responses[query] = rows
        if self.log is not None:
            h = hashlib.sha256(query.encode()).hexdigest()[:20]
            self.saved_paths[query] = self.log.save_text(
                f"searches/{h}.json", json.dumps({"query": query, "bindings": rows}, ensure_ascii=False),
                "application/json")
        return rows

    def run(self, unit: dict, tree_fn: Optional[Callable[[dict], List[str]]] = None,
            kinds: Iterable[str] = SEARCH_KINDS) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
        """{kind: [QIDs]} and {kind: error text} for one unit. A failed search is recorded,
        never treated as "found nothing"."""
        found: Dict[str, List[str]] = {}
        errors: Dict[str, str] = {}
        name, univ = unit["name"], unit.get("university") or ""
        plans = {
            "prefix": lambda: self.ask(prefix_query(name)),
            "fulltext": lambda: self.ask(fulltext_query(name)),
            "university_name": lambda: self.ask(fulltext_query(f"{univ} {name}")) if univ else [],
            "other_names": lambda: [b for alt in other_names(name, unit.get("aliases") or [], unit.get("parent_name") or "")
                                    for q in (prefix_query(alt), fulltext_query(alt)) for b in self.ask(q)],
            "website": lambda: self.ask(website_query(unit.get("website"))) if website_query(unit.get("website")) else [],
        }
        for kind in kinds:
            try:
                if kind == "tree":
                    found[kind] = list(tree_fn(unit)) if tree_fn else []
                else:
                    found[kind] = parse_items(plans[kind]())
            except Exception as e:  # noqa: BLE001 - the unit cannot become "new" now
                errors[kind] = f"{type(e).__name__}: {str(e)[:160]}"
                found[kind] = []
        return found, errors

    def details(self, qids: List[str]) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        qids = sorted(set(qids))
        for i in range(0, len(qids), 40):
            out.update(parse_details(self.ask(details_query(qids[i:i + 40]))))
        return out


def candidate_pool(found: Dict[str, List[str]], details: Dict[str, dict], kinds: Iterable[str],
                   exclude: Iterable[str] = ()) -> List[dict]:
    """The items the chosen search kinds found, in kind order, each with every kind that
    found it, without categories, people, and other items that are never units, and without
    `exclude` (the university itself, the unit's parent). At most MAX_CANDIDATES. Pure."""
    kinds = [k for k in SEARCH_KINDS if k in set(kinds)]
    skip = set(exclude)
    pool: Dict[str, dict] = {}
    for k in kinds:
        for q in found.get(k) or []:
            d = details.get(q)
            if q in skip or d is None or any(p in SKIP_P31 for p in d.get("p31_qids") or []):
                continue
            if (d.get("label") or "").startswith("Category:"):
                continue
            pool.setdefault(q, {**d, "found_by": []})
            if k not in pool[q]["found_by"]:
                pool[q]["found_by"].append(k)
    return list(pool.values())[:MAX_CANDIDATES]


# ── deciding ─────────────────────────────────────────────────────────────

def decide(unit: dict, candidates: List[dict], mode: str = "choose",
           choose_fn: Callable = None, yes_no_fn: Callable = None) -> dict:
    """{"qid", "duplicates", "reason", "per_candidate", "llm_call_ids", "provider"} for one
    unit and its candidates. With no candidates nothing is asked. yes-no: one answer per
    candidate; exactly one yes is the match; two or more yes are a possible Wikidata
    duplicate, and no item is chosen (a person decides)."""
    choose_fn = choose_fn or llm_helpers.LLMHelper.decide_alignment_choose
    yes_no_fn = yes_no_fn or llm_helpers.LLMHelper.decide_alignment_yes_no
    if not candidates:
        return {"qid": None, "duplicates": [], "reason": "no Wikidata item found by the searches",
                "per_candidate": {}, "llm_call_ids": [], "provider": None}
    shown = [{k: c.get(k) for k in ("qid", "label", "description", "aliases", "p31", "parents", "websites", "countries")}
             for c in candidates]
    if mode == "choose":
        r = choose_fn(unit, shown)
        dups = ([r["choice"]] + r["also_same_unit"]) if r["choice"] and r["also_same_unit"] else []
        return {"qid": r["choice"], "duplicates": dups, "reason": r["reason"], "per_candidate": {},
                "llm_call_ids": r["llm_call_ids"], "provider": r["provider"]}
    if mode != "yes-no":
        raise ValueError(f"decide: mode must be one of {DECIDE_MODES}, got {mode!r}")
    per, ids, provider = {}, [], None
    for c in shown:
        r = yes_no_fn(unit, c)
        per[c["qid"]] = {"same_unit": r["same_unit"], "reason": r["reason"]}
        ids += r["llm_call_ids"]
        provider = provider or r["provider"]
    yes = [q for q, v in per.items() if v["same_unit"]]
    if len(yes) == 1:
        return {"qid": yes[0], "duplicates": [], "reason": per[yes[0]]["reason"], "per_candidate": per,
                "llm_call_ids": ids, "provider": provider}
    if yes:
        return {"qid": None, "duplicates": yes, "reason": "the answer was yes for several items: "
                + ", ".join(yes) + "; a person decides", "per_candidate": per, "llm_call_ids": ids,
                "provider": provider}
    return {"qid": None, "duplicates": [], "reason": "the answer was no for every item", "per_candidate": per,
            "llm_call_ids": ids, "provider": provider}


def align_unit(unit: dict, searcher: Searcher, mode: str = "choose", kinds: Iterable[str] = SEARCH_KINDS,
               tree_fn: Optional[Callable[[dict], List[str]]] = None, exclude: Iterable[str] = (),
               found=None, errors=None, details=None, choose_fn=None, yes_no_fn=None) -> dict:
    """Search (unless `found` is given), then decide. Returns the decision plus
    "status" (aligned, new, or uncertain), "candidates" (every item looked at, with how it
    was found and whether it was chosen), and "search_errors"."""
    kinds = list(kinds)
    if found is None:
        found, errors = searcher.run(unit, tree_fn, kinds)
    errors = {k: v for k, v in (errors or {}).items() if k in kinds}
    if details is None:
        details = searcher.details([q for k in kinds for q in found.get(k) or []])
    pool = candidate_pool(found, details, kinds, exclude)
    d = decide(unit, pool, mode, choose_fn, yes_no_fn)
    cands = [{"qid": c["qid"], "label": c.get("label"), "found_by": c["found_by"], "chosen": c["qid"] == d["qid"],
              **({"same_unit": d["per_candidate"][c["qid"]]["same_unit"]} if c["qid"] in d["per_candidate"] else {})}
             for c in pool]
    if d["qid"]:
        status = "aligned"
        reason = f"chosen from {len(pool)} candidates ({mode}): {d['reason']}"
    elif errors:
        status = "uncertain"
        reason = "a search failed (" + "; ".join(f"{k}: {v}" for k, v in errors.items()) + "); not called new"
    elif d["duplicates"]:
        status = "uncertain"
        reason = d["reason"]
    else:
        status = "new"
        reason = f"no item after every search ({', '.join(kinds)}); {d['reason']}"
    return {**d, "status": status, "reason": reason, "candidates": cands, "search_errors": errors,
            "found": {k: found.get(k) or [] for k in kinds}}
