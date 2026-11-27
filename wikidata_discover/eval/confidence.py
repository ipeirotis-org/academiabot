"""
One confidence per node of a hierarchy file (Shuo, week 7; see TASKS.md and SPECS.md).

Step 1 runs the checks on each node of a hierarchy file (Anya's week 5 file) and records them
against the node's current version (`node_version`):

- the four week 5 page checks (page_status, soft_404, on_domain, name_on_page) on the node's
  own saved source page (the evidence entry with fact "unit"),
- the week 6 verifier on the same saved page, once per parent link ("Unit is part of Parent"),
- agreement, only when the run asked 2 or more providers: how many of them named the unit.

The checks read the saved page from the run log (the evidence row and its text in the
bucket). They never fetch the page again, because the reviewer judges the saved page.

Step 2 combines the checks of one node into one confidence (`confidence()`). Only checks on
the node's current version count. The rule (2026-11-27, Shuo's plan):

- high: the page exists (page_status pass), is not an error page (soft_404 pass), is on the
  university's domain (on_domain pass), names the unit (name_on_page pass), the verifier
  says supported for every parent link, and agreement passes when there is an agreement check.
- low: the page is dead or missing (page_status fail), is an error page (soft_404 fail), or
  the verifier says unsupported for a parent link. A node with no checks at all is low too.
- medium: everything else (for example a page that refused the fetcher, or a verifier that
  says unclear).

Usage:
    python -m wikidata_discover.eval.confidence HIERARCHY.json [--provider openai] [--no-run-log]
    python -m wikidata_discover.eval.confidence HIERARCHY.json --rescore DIR   # confidence again, no calls

Output (wikidata_discover/results/confidence/<run_id>/):
    node_checks.csv   one row per check: node_id, node_version, parent_id, kind, result, score, detail
    confidence.csv    one row per node: the confidence, the reasons, and the result of each check
    pages/            the saved text of each page the checks read
"""

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from wikidata_discover import config
from wikidata_discover.eval import page_checks, verifier
from wikidata_discover.eval.agreement import write_csv

console = config.console

RESULTS_DIR = config.RESULTS_DIR / "confidence"
LEVELS = ("low", "medium", "high")
PAGE_KINDS = page_checks.KINDS   # page_status, soft_404, on_domain, name_on_page

# The same fields and rule as hierarchy_file.node_version (Anya, branch anya-week-5-6). A test
# checks that it gives the version stored in the file, so the two stay the same.
VERSION_FIELDS = ("name", "aliases", "description", "unit_type", "parent_ids", "parents_shown", "parent_links",
                  "qid", "country", "alignment", "alignment_candidates", "wikidata_duplicates", "website",
                  "source_url", "evidence")


# ─────────────────────────  THE HIERARCHY FILE  ─────────────────────────

def load_hierarchy(path: Path) -> dict:
    with Path(path).open(encoding="utf-8") as f:
        return json.load(f)


def node_version(node: dict) -> str:
    """Hash of everything a reviewer is shown. Evidence counts by fact, URL, fetch outcome,
    and the hash of the page text (of the bytes when there is no text). Pure."""
    shown = {k: node.get(k) for k in VERSION_FIELDS}
    shown["evidence"] = [{"fact": e.get("fact"), "url": e.get("url"), "outcome": e.get("outcome"),
                          "page": e.get("text_hash") or e.get("content_hash")}
                         for e in node.get("evidence") or []]
    return hashlib.sha256(json.dumps(shown, sort_keys=True, default=str).encode()).hexdigest()[:16]


def current_version(node: dict) -> str:
    """The node's version as its fields are now. The stored value must agree; a file
    edited by hand without a new version would otherwise keep an old verdict valid."""
    return node_version(node)


def reviewable_nodes(hierarchy: dict) -> List[dict]:
    """Every node except the root (the university itself is not reviewed)."""
    return [n for n in hierarchy["nodes"] if n.get("level", 1) != 0 and n.get("node_id") != "root"]


def unit_evidence(node: dict) -> Optional[dict]:
    """The evidence entry of the node's own source page (fact "unit"), or None."""
    for e in node.get("evidence") or []:
        if e.get("fact") == "unit":
            return e
    return None


def parents(node: dict) -> List[dict]:
    """One entry per parent link: parent_id, name, qid, rank, qualifiers, p749. Pure."""
    shown = {p.get("node_id"): p for p in node.get("parents_shown") or []}
    out = []
    links = node.get("parent_links") or [{"parent_id": pid} for pid in node.get("parent_ids") or []]
    for link in links:
        pid = link.get("parent_id") or link.get("parent_key")
        p = shown.get(pid) or {}
        out.append({"parent_id": pid, "name": p.get("name") or "", "qid": p.get("qid") or link.get("parent_qid"),
                    "rank": link.get("rank") or "", "qualifiers": link.get("qualifiers") or {},
                    "p749": link.get("p749") or ""})
    return out


# ─────────────────────────  CHECKS ON ONE NODE  ─────────────────────────

Verify = Callable[[str, str, str, str, object], dict]


def page_from_evidence(row: dict, text: str, content: Optional[bytes]) -> dict:
    """The page dict that the week 5 checks read, from a saved evidence row. Pure."""
    return {"url": row.get("url") or "", "outcome": row.get("outcome") or "", "status": row.get("http_status"),
            "final_url": row.get("final_url") or "", "detail": row.get("detail") or "",
            "content_type": row.get("content_type") or "",
            "title": page_checks.html_title(content, row.get("content_type") or ""),
            "name_check": row.get("name_check") or "done", "text": text or "",
            "evidence_id": row.get("evidence_id") or ""}


def node_checks(node: dict, page: Optional[dict], domains: Sequence[str], verify: Optional[Verify],
                run_providers: Sequence[str] = (), named_by: Sequence[str] = ()) -> List[dict]:
    """The check rows of one node (not yet written): kind, result, score, detail, parent_id,
    evidence_ids, llm_call_ids. `page` is the saved unit page (None when there is none),
    `verify(unit, parent, text, outcome, status)` is the week 6 verifier. Pure apart from
    `verify`."""
    version = current_version(node)
    ev = [page["evidence_id"]] if page and page.get("evidence_id") else []
    base = {"node_id": node["node_id"], "node_version": version, "evidence_ids": ev, "llm_call_ids": []}
    claim = {"name": node.get("name") or "", "url": (page or {}).get("url") or node.get("source_url") or ""}
    rows = []
    for kind, (result, score, detail) in page_checks.run_checks(claim, page, domains).items():
        rows.append({**base, "kind": kind, "result": result, "score": score, "parent_id": None,
                     "detail": json.dumps({"detail": detail, "url": claim["url"]}, sort_keys=True)})
    for p in parents(node):
        if verify is None:
            continue
        if page is None:
            v = {"verdict": "unsupported", "quote": "", "reason": "no source page", "decided_by": "page_status",
                 "pieces": 0, "piece": 0, "llm_call_ids": [], "quote_ok": ""}
        else:
            v = verify(node.get("name") or "", p["name"], page.get("text") or "", page.get("outcome") or "",
                       page.get("status"))
        result = {"supported": "pass", "unsupported": "fail"}.get(v["verdict"], "unclear")
        rows.append({**base, "kind": "verifier", "result": result, "score": None, "parent_id": p["parent_id"],
                     "llm_call_ids": list(v.get("llm_call_ids") or []),
                     "detail": json.dumps({"method": "llm", "verdict": v["verdict"], "quote": v.get("quote", ""),
                                           "quote_ok": v.get("quote_ok", ""), "reason": v.get("reason", ""),
                                           "decided_by": v.get("decided_by", ""), "pieces": v.get("pieces", 0),
                                           "piece": v.get("piece", 0), "parent": p["name"], "url": claim["url"]},
                                          sort_keys=True)})
    if len(set(run_providers)) >= 2:
        named = sorted(set(named_by) & set(run_providers))
        rows.append({**base, "kind": "agreement", "result": "pass" if len(named) >= 2 else "fail",
                     "score": round(len(named) / len(set(run_providers)), 4), "parent_id": None,
                     "detail": json.dumps({"named_by": named, "providers": sorted(set(run_providers))}, sort_keys=True)})
    return rows


# ─────────────────────────  ONE CONFIDENCE  ─────────────────────────

def current_checks(checks: Iterable[dict], node: dict) -> List[dict]:
    """The checks of this node on its current version only. Pure."""
    version = current_version(node)
    return [c for c in checks if c.get("node_id") == node["node_id"] and c.get("node_version") == version]


def confidence(checks: Sequence[dict], parent_ids: Sequence[str] = ()) -> Tuple[str, List[str]]:
    """(level, reasons) from the checks of one node version. See the module docstring. Pure."""
    if not checks:
        return "low", ["no checks on this version"]
    by_kind: Dict[str, List[dict]] = {}
    for c in checks:
        by_kind.setdefault(c["kind"], []).append(c)
    reasons_low, reasons_mid = [], []
    for kind in PAGE_KINDS:
        results = [c["result"] for c in by_kind.get(kind, [])]
        if not results:
            reasons_mid.append(f"{kind}: no check")
        elif kind in ("page_status", "soft_404") and "fail" in results:
            reasons_low.append(f"{kind}: fail")
        elif any(r != "pass" for r in results):
            reasons_mid.append(f"{kind}: {'fail' if 'fail' in results else 'unclear'}")
    verdicts = {c.get("parent_id"): c["result"] for c in by_kind.get("verifier", [])}
    for pid in parent_ids or list(verdicts):
        r = verdicts.get(pid)
        if r is None:
            reasons_mid.append(f"verifier ({pid}): no check")
        elif r == "fail":
            reasons_low.append(f"verifier ({pid}): unsupported")
        elif r != "pass":
            reasons_mid.append(f"verifier ({pid}): unclear")
    for c in by_kind.get("agreement", []):
        if c["result"] != "pass":
            reasons_mid.append(f"agreement: {c['result']}")
    if reasons_low:
        return "low", reasons_low + reasons_mid
    if reasons_mid:
        return "medium", reasons_mid
    return "high", ["every check passes"]


def doubt_key(level: str, reasons: Sequence[str]):
    """Sort key: low first, then medium, then high; more problems first. Pure."""
    return (LEVELS.index(level), -len(reasons))


def summarize(nodes: Sequence[dict], checks: Sequence[dict]) -> List[dict]:
    """confidence.csv rows: one per node, with its level, reasons, and each check result. Pure."""
    rows = []
    for n in nodes:
        mine = current_checks(checks, n)
        level, reasons = confidence(mine, [p["parent_id"] for p in parents(n)])
        row = {"node_id": n["node_id"], "node_version": current_version(n), "name": n.get("name", ""),
               "unit_type": n.get("unit_type", ""), "confidence": level, "reasons": "; ".join(reasons)}
        for kind in PAGE_KINDS + ("agreement",):
            row[kind] = ",".join(c["result"] for c in mine if c["kind"] == kind)
        row["verifier"] = ",".join(c["result"] for c in mine if c["kind"] == "verifier")
        rows.append(row)
    return rows


CHECK_COLUMNS = ["node_id", "node_version", "parent_id", "kind", "result", "score", "detail", "evidence_ids",
                 "llm_call_ids", "check_id", "run_id"]
CONFIDENCE_COLUMNS = ["node_id", "node_version", "name", "unit_type", "confidence", "reasons",
                      "page_status", "soft_404", "on_domain", "name_on_page", "verifier", "agreement"]


def read_checks_csv(path: Path) -> List[dict]:
    with Path(path).open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["parent_id"] = r.get("parent_id") or None
    return rows


def read_checks_bigquery(nodes: Sequence[dict], client=None) -> List[dict]:
    """The checks rows in BigQuery whose node_id and node_version match a node's current
    version (node ids repeat across universities, the version does not)."""
    from google.cloud import bigquery
    from wikidata_discover import run_log
    client = client or bigquery.Client(project=config.GCP_PROJECT)
    versions = sorted({current_version(n) for n in nodes})
    q = (f"SELECT node_id, node_version, parent_id, kind, result, score, detail, check_id, run_id "
         f"FROM `{run_log.table_ref('checks')}` WHERE node_version IN UNNEST(@v)")
    job = client.query(q, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("v", "STRING", versions)]))
    return [dict(r) for r in job.result()]


# ─────────────────────────  SAVED PAGES  ─────────────────────────

def load_saved_pages(evidence_ids: Sequence[str], out_dir: Path, client=None, storage_client=None) -> Dict[str, dict]:
    """evidence_id -> page dict, from the evidence rows in BigQuery and the saved text and
    bytes in the bucket. A text that cannot be read leaves the page with empty text."""
    from google.cloud import bigquery, storage
    from wikidata_discover import run_log
    client = client or bigquery.Client(project=config.GCP_PROJECT)
    storage_client = storage_client or storage.Client(project=config.GCP_PROJECT)
    job = client.query(f"SELECT * FROM `{run_log.table_ref('evidence')}` WHERE evidence_id IN UNNEST(@ids)",
                       job_config=bigquery.QueryJobConfig(query_parameters=[
                           bigquery.ArrayQueryParameter("ids", "STRING", sorted(set(evidence_ids)))]))
    (out_dir / "pages").mkdir(parents=True, exist_ok=True)

    def blob(path: Optional[str]) -> Optional[bytes]:
        if not path or not path.startswith("gs://"):
            return None
        bucket, name = path[5:].split("/", 1)
        try:
            return storage_client.bucket(bucket).blob(name).download_as_bytes(timeout=60)
        except Exception as e:  # noqa: BLE001 - a missing page leaves the text empty
            console.print(f"[yellow]could not read {path}: {type(e).__name__}[/yellow]")
            return None

    pages = {}
    for row in job.result():
        row = dict(row)
        raw = blob(row.get("text_path"))
        text = raw.decode("utf-8", errors="replace") if raw else ""
        content = blob(row.get("snapshot_path")) if (row.get("content_type") or "").startswith("text/html") else None
        (out_dir / "pages" / f"{row['evidence_id']}.txt").write_text(text, encoding="utf-8")
        pages[row["evidence_id"]] = page_from_evidence(row, text, content)
    return pages


def write_pages_index(out_dir: Path, pages: Dict[str, dict]) -> None:
    rows = [{k: v for k, v in p.items() if k != "text"} for p in pages.values()]
    write_csv(out_dir / "pages.csv", rows, ["evidence_id", "url", "outcome", "status", "final_url", "detail",
                                             "content_type", "title", "name_check"])


def read_pages_index(out_dir: Path) -> Dict[str, dict]:
    pages = {}
    with (out_dir / "pages.csv").open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            r["status"] = int(r["status"]) if r["status"] else None
            r["text"] = (out_dir / "pages" / f"{r['evidence_id']}.txt").read_text(encoding="utf-8")
            pages[r["evidence_id"]] = r
    return pages


# ─────────────────────────  RUN  ─────────────────────────

def run_providers_of(hierarchy: dict) -> List[str]:
    """The providers that listed units in this run, from the nodes. Pure."""
    return sorted({n.get("provider") for n in hierarchy["nodes"] if n.get("provider")})


def check_all(hierarchy: dict, pages: Dict[str, dict], domains: Sequence[str], verify: Optional[Verify]) -> List[dict]:
    providers = run_providers_of(hierarchy)
    rows = []
    nodes = reviewable_nodes(hierarchy)
    for i, n in enumerate(nodes, 1):
        e = unit_evidence(n)
        page = pages.get(e["evidence_id"]) if e else None
        named = [n["provider"]] if n.get("provider") else []
        rows.extend(node_checks(n, page, domains, verify, providers, named))
        if i % 50 == 0:
            console.print(f"  {i} of {len(nodes)} nodes")
    return rows


def run(args) -> Path:
    from wikidata_discover import run_log
    from wikidata_discover.batch import operator_identity, source_identity
    hierarchy = load_hierarchy(Path(args.hierarchy))
    nodes = reviewable_nodes(hierarchy)
    stale = [n["node_id"] for n in hierarchy["nodes"] if n.get("node_version") and n["node_version"] != current_version(n)]
    if stale:
        console.print(f"[yellow]{len(stale)} nodes have a stored node_version that does not match their fields "
                      f"(first: {stale[:5]}). The checks use the version of the fields.[/yellow]")
    if args.rescore:
        out_dir = Path(args.rescore)
        checks = read_checks_csv(out_dir / "node_checks.csv")
        write_csv(out_dir / "confidence.csv", summarize(nodes, checks), CONFIDENCE_COLUMNS)
        console.print(f"Report: {out_dir}")
        return out_dir
    config.load_missing_keys()
    model = {"openai": config.LLM_MODEL, "anthropic": config.ANTHROPIC_MODEL, "gemini": config.GEMINI_MODEL}[args.provider]
    run_id = run_log.new_run_id("node-checks")
    out_dir = Path(args.out) if args.out else RESULTS_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    log = None
    if not args.no_run_log:
        identity = source_identity(out_dir)
        run_log.start(run_id, command="node-checks", args={"argv": sys.argv[1:]},
                      qids=[hierarchy.get("university_qid")], who=operator_identity(), git_commit=identity,
                      run_config={"provider": args.provider, "model": model, "hierarchy_run_id": hierarchy.get("run_id"),
                                  "piece_chars": verifier.PIECE_CHARS, "piece_overlap": verifier.PIECE_OVERLAP})
        log = run_log.current()
        if log is not None:
            log.attach_source_patch(out_dir, identity)
    outcome = "failed"
    try:
        ids = [e["evidence_id"] for n in nodes for e in [unit_evidence(n)] if e and e.get("evidence_id")]
        pages = load_saved_pages(ids, out_dir)
        write_pages_index(out_dir, pages)
        qid = hierarchy.get("university_qid")
        root = next((n for n in hierarchy["nodes"] if n.get("node_id") == "root"), {})
        domains = page_checks.university_domains([qid], {qid: [root.get("website") or ""]}).get(qid, [])
        console.print(f"{len(nodes)} nodes, {len(pages)} saved pages, domains {domains}")

        def verify(unit, parent, text, outcome_, status):
            return verifier.verify_claim(unit, parent, text, outcome_, status, provider=args.provider)

        checks = check_all(hierarchy, pages, domains, verify)
        if log is not None:
            for c in checks:
                c["check_id"] = log.add_check(kind=c["kind"], result=c["result"], node_id=c["node_id"],
                                              node_version=c["node_version"], parent_id=c["parent_id"],
                                              score=c["score"], detail=c["detail"],
                                              llm_call_ids=c["llm_call_ids"], evidence_ids=c["evidence_ids"])
                c["run_id"] = run_id
        rows = [{k: (json.dumps(c[k]) if isinstance(c.get(k), list) else c.get(k, "")) for k in CHECK_COLUMNS}
                for c in checks]
        write_csv(out_dir / "node_checks.csv", rows, CHECK_COLUMNS)
        summary = summarize(nodes, checks)
        write_csv(out_dir / "confidence.csv", summary, CONFIDENCE_COLUMNS)
        counts = {lv: sum(1 for r in summary if r["confidence"] == lv) for lv in LEVELS}
        console.print(f"Confidence: {counts}")
        outcome = "ok"
    finally:
        if log is not None:
            console.print(f"[dim]{run_log.describe(log.finish(outcome, {'out_dir': str(out_dir)}))}[/dim]")
    console.print(f"Report: {out_dir}")
    return out_dir


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Checks and one confidence per node of a hierarchy file (week 7).")
    ap.add_argument("hierarchy", help="a hierarchy file (results/hierarchy/<QID>.json)")
    ap.add_argument("--provider", default="openai", choices=["openai", "anthropic", "gemini"],
                    help="the verifier's model (default openai: gpt-4o, Shuo's week 6 choice)")
    ap.add_argument("--out", help="output folder (default results/confidence/<run_id>)")
    ap.add_argument("--no-run-log", action="store_true", help="write the report only, no checks rows")
    ap.add_argument("--rescore", help="a report folder: compute confidence.csv again from its node_checks.csv")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
