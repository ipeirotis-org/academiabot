"""Snapshot (Anya, week 3): a university's whole subtree as Wikidata has it today.

Every unit below the university through P749, P361 (upward: child -> parent) and P527,
P355, P199 (downward: parent -> child), crawled one unit at a time over full statements
(every rank except deprecated), with each statement's rank and qualifiers. Then each
unit's own P749 and P361 statements, so a second parent outside the tree is kept too
(NYU Shanghai, Q13652966, also has P749 to East China Normal University). For every
unit: labels, other names (aliases), P31, website (P856), ROR id (P6782).

The queries are the ones in TASKS.md section 11. All of them go through
sparql_helpers (0.3 s between requests, retries, 429 waits).

`snapshot <QID>...` saves each snapshot to the bucket under
gs://academiabot/runs/<run_id>/snapshots/<QID>.json, to
results/runs/<run_id>/snapshots/<QID>.json, and as the latest copy to
results/snapshots/<QID>.json, and writes one runs row per invocation. With a fixed
--run-id, a restart skips every university whose snapshot is already in the bucket.
"""
import json
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from wikidata_discover import config
from wikidata_discover.sparql_helpers import execute_sparql_bindings

ENTITY = "http://www.wikidata.org/entity/"
STATEMENT = ENTITY + "statement/"
QUALIFIER = "http://www.wikidata.org/prop/qualifier/"
UP = ("P749", "P361")             # the statement is on the child, its value is the parent
DOWN = ("P527", "P355", "P199")   # the statement is on the parent, its value is the child
DETAIL_PROPS = ("P31", "P856", "P6782")
BATCH = 40                        # values per VALUES list, so a GET request stays short
MAX_UNITS = 5000                  # a safety stop; the snapshot then says it is truncated


# ── queries (TASKS.md section 11) ────────────────────────────────────────

def children_query(qid: str) -> str:
    """One unit's children over full statements, rank shown. Keep the optimizer hint
    and the order inside each block: without them the query service times out."""
    up = [f'{{ ?st ps:{p} wd:{qid} . ?child p:{p} ?st . BIND("{p}" AS ?prop) }}' for p in UP]
    down = [f'{{ wd:{qid} p:{p} ?st . ?st ps:{p} ?child . BIND("{p}" AS ?prop) }}' for p in DOWN]
    return ("SELECT ?st ?prop ?child ?rank WHERE {\n"
            '  hint:Query hint:optimizer "None" .\n  '
            + "\n  UNION ".join(up + down) + "\n"
            "  ?st wikibase:rank ?rank . FILTER(?rank != wikibase:DeprecatedRank)\n}")


def parents_query(qids: Iterable[str]) -> str:
    """Every P749 and P361 statement of the given units, rank shown."""
    values = " ".join(f"wd:{q}" for q in qids)
    blocks = [f'{{ ?child p:{p} ?st . ?st ps:{p} ?parent . BIND("{p}" AS ?prop) }}' for p in UP]
    return ("SELECT ?child ?prop ?parent ?rank ?st WHERE {\n"
            '  hint:Query hint:optimizer "None" .\n'
            f"  VALUES ?child {{ {values} }}\n  " + "\n  UNION ".join(blocks) + "\n"
            "  ?st wikibase:rank ?rank . FILTER(?rank != wikibase:DeprecatedRank)\n}")


def qualifiers_query(statement_ids: Iterable[str]) -> str:
    values = " ".join(f"<{STATEMENT}{s}>" for s in statement_ids)
    return ("SELECT ?st ?qual ?value WHERE {\n"
            f"  VALUES ?st {{ {values} }}\n"
            "  ?st ?qual ?value .\n"
            f'  FILTER(STRSTARTS(STR(?qual), "{QUALIFIER}P"))\n}}')


def details_query(qids: Iterable[str]) -> str:
    """Labels and aliases in every language, and P31, P856, P6782 over full statements."""
    values = " ".join(f"wd:{q}" for q in qids)
    props = [f'{{ ?item p:{p} ?s . ?s ps:{p} ?value ; wikibase:rank ?rank . BIND("{p}" AS ?kind) '
             "FILTER(?rank != wikibase:DeprecatedRank) }" for p in DETAIL_PROPS]
    return ("SELECT ?item ?kind ?value ?rank WHERE {\n"
            f"  VALUES ?item {{ {values} }}\n"
            '  { ?item rdfs:label ?value . BIND("label" AS ?kind) }\n'
            '  UNION { ?item skos:altLabel ?value . BIND("alias" AS ?kind) }\n  UNION '
            + "\n  UNION ".join(props) + "\n}")


def english_labels_query(qids: Iterable[str]) -> str:
    values = " ".join(f"wd:{q}" for q in qids)
    return (f"SELECT ?item ?label WHERE {{ VALUES ?item {{ {values} }} "
            '?item rdfs:label ?label . FILTER(LANG(?label) = "en") }')


# ── parsing (pure) ───────────────────────────────────────────────────────

def _v(b: dict, key: str) -> Optional[str]:
    return b.get(key, {}).get("value")


def _qid(uri: Optional[str]) -> Optional[str]:
    return uri[len(ENTITY):] if uri and uri.startswith(ENTITY) and "/" not in uri[len(ENTITY):] else None


def _rank(uri: Optional[str]) -> str:
    name = (uri or "").rsplit("#", 1)[-1]
    return {"PreferredRank": "preferred", "NormalRank": "normal"}.get(name, name.lower() or "normal")


def _statement_id(uri: str) -> str:
    return uri[len(STATEMENT):] if uri.startswith(STATEMENT) else uri


def parse_children(qid: str, bindings: List[dict]) -> List[dict]:
    """Links from one unit to its children. A value that is not an item (a blank or a
    literal) is skipped."""
    links = []
    for b in bindings:
        child, prop = _qid(_v(b, "child")), _v(b, "prop")
        if not child or not prop:
            continue
        links.append({"statement_id": _statement_id(_v(b, "st")), "prop": prop,
                      "child": child, "parent": qid, "rank": _rank(_v(b, "rank")),
                      "statement_on": child if prop in UP else qid})
    return links


def parse_parents(bindings: List[dict]) -> List[dict]:
    links = []
    for b in bindings:
        child, parent, prop = _qid(_v(b, "child")), _qid(_v(b, "parent")), _v(b, "prop")
        if child and parent and prop:
            links.append({"statement_id": _statement_id(_v(b, "st")), "prop": prop, "child": child,
                          "parent": parent, "rank": _rank(_v(b, "rank")), "statement_on": child})
    return links


def parse_qualifiers(bindings: List[dict]) -> Dict[str, Dict[str, List[str]]]:
    """{statement_id: {qualifier property: [values]}}. Item values become QIDs."""
    out: Dict[str, Dict[str, List[str]]] = {}
    for b in bindings:
        st, qual, value = _v(b, "st"), _v(b, "qual"), _v(b, "value")
        if not st or not qual or value is None:
            continue
        prop = qual[len(QUALIFIER):]
        vals = out.setdefault(_statement_id(st), {}).setdefault(prop, [])
        value = _qid(value) or value
        if value not in vals:
            vals.append(value)
    return out


def parse_details(bindings: List[dict]) -> Dict[str, dict]:
    """{qid: {labels: {lang: text}, aliases: {lang: [text]}, P31: [{qid, rank}],
    P856: [{value, rank}], P6782: [{value, rank}]}}."""
    out: Dict[str, dict] = {}
    for b in bindings:
        item, kind = _qid(_v(b, "item")), _v(b, "kind")
        if not item or not kind:
            continue
        d = out.setdefault(item, empty_details())
        value = b.get("value", {})
        text, lang = value.get("value"), value.get("xml:lang", "")
        if kind == "label":
            d["labels"][lang] = text
        elif kind == "alias":
            if text not in d["aliases"].setdefault(lang, []):
                d["aliases"][lang].append(text)
        elif kind == "P31":
            entry = {"qid": _qid(text) or text, "rank": _rank(_v(b, "rank"))}
            if entry not in d["P31"]:
                d["P31"].append(entry)
        elif kind in ("P856", "P6782"):
            entry = {"value": text, "rank": _rank(_v(b, "rank"))}
            if entry not in d[kind]:
                d[kind].append(entry)
    return out


def empty_details() -> dict:
    return {"labels": {}, "aliases": {}, "P31": [], "P856": [], "P6782": []}


def best_label(labels: Dict[str, str], qid: str) -> str:
    """English label, else the multilingual one, else any, else the QID."""
    return labels.get("en") or labels.get("mul") or next(iter(sorted(labels.items())), (None, qid))[1]


def _batches(items: List[str], size: int = BATCH):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ── the crawl ────────────────────────────────────────────────────────────

def take_snapshot(root: str, sparql: Callable[[str], List[dict]] = execute_sparql_bindings,
                  max_units: int = MAX_UNITS) -> dict:
    """Crawl the subtree of `root` and return the snapshot. `sparql` is the query
    function (a fake in tests)."""
    started = time.time()
    queries = 0

    def ask(q: str) -> List[dict]:
        nonlocal queries
        queries += 1
        return sparql(q)

    depth = {root: 0}
    order = [root]
    links: Dict[str, dict] = {}
    truncated = False
    i = 0
    while i < len(order):                       # breadth first, one unit at a time
        unit = order[i]
        i += 1
        for link in parse_children(unit, ask(children_query(unit))):
            links.setdefault(link["statement_id"], link)
            child = link["child"]
            if child not in depth:
                if len(order) >= max_units:
                    truncated = True
                    continue
                depth[child] = depth[unit] + 1
                order.append(child)

    # Each unit's own upward statements, so a parent outside the tree is kept.
    for batch in _batches(order):
        for link in parse_parents(ask(parents_query(batch))):
            links.setdefault(link["statement_id"], link)

    qualifiers: Dict[str, Dict[str, List[str]]] = {}
    for batch in _batches(sorted(links)):
        qualifiers.update(parse_qualifiers(ask(qualifiers_query(batch))))

    in_tree = set(order)
    outside = sorted({l["parent"] for l in links.values()} - in_tree)
    details: Dict[str, dict] = {}
    for batch in _batches(order + outside):
        details.update(parse_details(ask(details_query(batch))))

    p31_qids = sorted({e["qid"] for d in details.values() for e in d["P31"]})
    p31_labels: Dict[str, str] = {}
    for batch in _batches(p31_qids):
        for b in ask(english_labels_query(batch)):
            p31_labels[_qid(_v(b, "item"))] = _v(b, "label")

    units = {}
    for qid in order + outside:
        d = details.get(qid, empty_details())
        for e in d["P31"]:
            e["label"] = p31_labels.get(e["qid"])
        units[qid] = {"qid": qid, "label": best_label(d["labels"], qid), "in_tree": qid in in_tree,
                      "depth": depth.get(qid), **d,
                      "parents": sorted(({"parent": l["parent"], "prop": l["prop"], "rank": l["rank"],
                                          "statement_id": l["statement_id"],
                                          "statement_on": l["statement_on"],
                                          "qualifiers": qualifiers.get(l["statement_id"], {})}
                                         for l in links.values() if l["child"] == qid),
                                        key=lambda p: (p["prop"], p["parent"], p["statement_id"]))}
    link_list = sorted(({**l, "qualifiers": qualifiers.get(l["statement_id"], {}),
                         "parent_in_tree": l["parent"] in in_tree} for l in links.values()),
                       key=lambda l: (l["parent"], l["child"], l["prop"], l["statement_id"]))
    return {
        "university_qid": root,
        "university_label": units[root]["label"],
        "taken_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "properties": {"up": list(UP), "down": list(DOWN)},
        "counts": {"units_in_tree": len(order) - 1, "parents_outside_tree": len(outside),
                   "links": len(link_list), "queries": queries,
                   "seconds": round(time.time() - started, 1)},
        "truncated": truncated,
        "units": units,
        "links": link_list,
    }


# ── the command ──────────────────────────────────────────────────────────

def load_university_list(bucket, name: str = "universities_us_tier1.json") -> List[str]:
    """QIDs from the university list in the bucket ([[QID, label], ...])."""
    from wikidata_discover.batch import is_qid
    rows = json.loads(bucket.blob(name).download_as_text())
    return [r[0] for r in rows if r and is_qid(r[0])]


def done_in_bucket(bucket, run_id: str) -> set:
    """Universities whose snapshot is already in the bucket for this run."""
    prefix = f"runs/{run_id}/snapshots/"
    return {Path(b.name).stem for b in bucket.client.list_blobs(bucket, prefix=prefix)
            if b.name.endswith(".json")}


def unique_qids(qids: List[str]) -> Tuple[List[str], int]:
    """The QIDs in order, each once (the first occurrence kept), and how many were dropped."""
    out = list(dict.fromkeys(qids))
    return out, len(qids) - len(out)


def run_snapshots(qids: List[str], run_id: Optional[str] = None, argv=None, *, bucket=None,
                  sparql: Callable[[str], List[dict]] = execute_sparql_bindings,
                  results_dir: Optional[Path] = None, report=print) -> dict:
    """Snapshot each QID, skip those already in the bucket for this run, and record the
    invocation in the run log. One failed university does not stop the others; it is
    retried on the next start. Returns the summary."""
    from wikidata_discover import run_log
    from wikidata_discover.batch import operator_identity, source_identity, validate_run_id
    results_dir = results_dir or config.RESULTS_DIR
    run_id = validate_run_id(run_id) if run_id else run_log.new_run_id("snapshot")
    run_dir = results_dir / "runs" / run_id
    (run_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    identity = source_identity(run_dir)
    log = run_log.start(run_id, command="snapshot", args={"argv": list(argv or [])}, qids=qids,
                        who=operator_identity(), git_commit=identity, bucket=bucket,
                        results_dir=results_dir,
                        run_config={"properties_up": list(UP), "properties_down": list(DOWN),
                                    "user_agent": config.USER_AGENT, "bucket": config.RUNLOG_BUCKET})
    log.attach_source_patch(run_dir, identity)

    requested = len(qids)
    qids, dropped = unique_qids(qids)
    if dropped:
        report(f"{dropped} duplicate QIDs dropped; {len(qids)} universities left, first occurrence kept")
    summary = {"requested": requested, "duplicates_dropped": dropped, "skipped_done": 0, "ok": 0,
               "failed": 0, "failed_qids": [], "units": 0}
    # Build both clients now. Each reads the service account key file once and keeps the
    # credentials in memory, so a long run survives the session-end hook that deletes
    # the file. A client that cannot be built here makes the run log fall back to local
    # JSON, as for any other bucket or BigQuery failure.
    for build in (log._get_bucket, log._get_bq):
        try:
            build()
        except Exception as e:  # noqa: BLE001
            report(f"run log: could not build a cloud client at start ({type(e).__name__})")
    try:
        done = done_in_bucket(log._get_bucket(), run_id)
    except Exception as e:  # noqa: BLE001 - no bucket listing: nothing is skipped
        report(f"could not list finished snapshots ({type(e).__name__}); nothing is skipped")
        done = set()
    progress = run_dir / "progress.jsonl"
    outcome = "failed"
    try:
        for n, qid in enumerate(qids, 1):
            if qid in done:
                summary["skipped_done"] += 1
                continue
            t0 = time.time()
            try:
                snap = take_snapshot(qid, sparql=sparql)
                snap["run_id"], snap["invocation_id"] = run_id, log.invocation_id
                text = json.dumps(snap, indent=1, ensure_ascii=False, sort_keys=True)
                where = log.save_text(f"snapshots/{qid}.json", text, "application/json")
                (run_dir / "snapshots" / f"{qid}.json").write_text(text)
                (results_dir / "snapshots").mkdir(parents=True, exist_ok=True)
                (results_dir / "snapshots" / f"{qid}.json").write_text(text)
                rec = {"qid": qid, "status": "ok", "units": snap["counts"]["units_in_tree"],
                       "truncated": snap["truncated"], "saved_to": where}
                summary["ok"] += 1
                summary["units"] += snap["counts"]["units_in_tree"]
            except Exception as e:  # noqa: BLE001 - one university must not stop the run
                rec = {"qid": qid, "status": "failed", "error": f"{type(e).__name__}: {str(e)[:300]}"}
                summary["failed"] += 1
                summary["failed_qids"].append(qid)
            rec.update({"n": n, "of": len(qids), "seconds": round(time.time() - t0, 1),
                        "at": run_log.now_iso(), "invocation_id": log.invocation_id})
            with progress.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            report(f"[{n}/{len(qids)}] {qid} {rec['status']} "
                   f"{rec.get('units', rec.get('error', ''))} ({rec['seconds']} s)")
            if n % 25 == 0:
                log.save_text("progress.jsonl", progress.read_text())
        outcome = "ok" if not summary["failed"] else "partial"
    except KeyboardInterrupt:
        outcome = "interrupted"
        raise
    finally:
        if progress.exists():
            log.save_text("progress.jsonl", progress.read_text())
        summary["outcome"] = outcome
        summary["run_log"] = log.finish(outcome, {k: v for k, v in summary.items() if k != "run_log"})
        report(run_log.describe(summary["run_log"]))
    return summary
