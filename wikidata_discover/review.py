"""
The review sheet and the `reviews` table (Shuo, week 7; see TASKS.md and SPECS.md).

A review sheet has one row per node of a hierarchy file, with every value the export would
write: the name, aliases, type, QID or "new", the Wikidata items alignment considered, the
country and where it came from, the English description, the website, the source URL, and
for each parent link its target, rank, qualifiers, and the page that supports it.

Two passes:

1. `blind`: rows in a random order fixed by a seed, checks and confidence hidden. Every
   reviewer gets the same order, so the first N rows are a random sample.
2. `shown`: the checks and the confidence are on the row, doubtful rows first. A reviewer
   may record a second verdict here, but only on a unit with a blind verdict on the same version.

Columns to fill: `verdict` (accept, reject, fix), `url_checked_<k>` (yes or no, one per page
on the row), one `fix_<field>` column for each value a reviewer can correct, and `notes`.

- accept: every page on the row has `url_checked` yes, and no `fix_` column is filled.
- fix: one `fix_` column or more, and yes or no for every page. A corrected country needs
  `fix_country_source`, and a corrected parent link (target, QID, rank, qualifiers) needs
  `fix_parent_<k>_source`, because those facts are exported with their own reference.
- reject: `url_checked` is optional.

`review load` adds one `reviews` row per filled row. It never changes or deletes a row. A
row identical to one already saved (same reviewer, version, pass, and answers) is skipped,
so loading the same sheet twice adds nothing. A verdict counts only while the node's version
is the same: a later run that changes the unit (for example its source URL) gives a new
version, and the unit shows up again as unreviewed.

Usage:
    python -m wikidata_discover.review sheet HIERARCHY.json --pass blind [--limit N] [--out FILE.xlsx]
    python -m wikidata_discover.review sheet HIERARCHY.json --pass shown --checks DIR/node_checks.csv
    python -m wikidata_discover.review load FILE.xlsx --reviewer shuo
    python -m wikidata_discover.review status HIERARCHY.json [--reviewer shuo]
    python -m wikidata_discover.review export --university Q49210 --out reviews.csv
"""

import argparse
import csv
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from wikidata_discover import config
from wikidata_discover.eval import confidence as conf

console = config.console

SEED = 20261127
VERDICTS = ("accept", "reject", "fix")
PASSES = ("blind", "shown")
SOURCES = ("expert", "prolific")
RESULTS_DIR = config.RESULTS_DIR / "review"

UNIT_FIELDS = ["node_id", "level", "unit_type", "in_scope", "name", "aliases", "qid", "alignment",
               "alignment_candidates", "wikidata_duplicates", "description", "website", "source_url",
               "country", "country_source"]
PARENT_FIELDS = ["", "_qid", "_p749", "_rank", "_qualifiers", "_source"]    # parent_<k><suffix>
PAGE_FIELDS = ["_fact", "_url", "_saved"]                                   # page_<k><suffix>
FIXABLE_UNIT = ["name", "aliases", "unit_type", "qid", "description", "website", "source_url",
                "country", "country_source"]
FIXABLE_PARENT = ["", "_qid", "_rank", "_qualifiers", "_source"]
CHECK_FIELDS = ["confidence", "confidence_reasons", "check_page_status", "check_soft_404", "check_on_domain",
                "check_name_on_page", "check_verifier", "check_agreement"]
META_FIELDS = ["pass", "university_qid", "run_id", "node_version", "shown_hash"]


# ─────────────────────────  ROWS  ─────────────────────────

def _text(v) -> str:
    """A cell value as text. Lists are joined with "; ", dicts are JSON. Pure."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "; ".join(_text(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, sort_keys=True) if v else ""
    return str(v)


def _candidates_text(cands: Sequence[dict]) -> str:
    out = []
    for c in cands or []:
        found = c.get("found_by") or ""
        bits = ["chosen"] if c.get("chosen") else []
        bits.append("found by " + (", ".join(found) if isinstance(found, list) else str(found)))
        if c.get("reason"):
            bits.append(str(c["reason"]))
        label = f" {c['label']}" if c.get("label") else ""
        out.append(f"{c.get('qid')}{label} ({'; '.join(b for b in bits if b and b != 'found by ')})")
    return "; ".join(out)


def pages_of(node: dict) -> List[dict]:
    """The evidence pages a reviewer must open for this row: the unit's source page (which
    also supports each parent link), and the country's source page when it is a URL. Pure."""
    pages, seen = [], set()
    unit = conf.unit_evidence(node)
    unit_url = (unit or {}).get("url") or node.get("source_url") or ""
    if unit_url:
        pages.append({"fact": "unit, parent links", "url": unit_url,
                      "saved": (unit or {}).get("outcome") or "not saved"})
        seen.add(unit_url)
    for e in node.get("evidence") or []:
        if e.get("fact") == "country" and e.get("url") and e["url"] not in seen:
            pages.append({"fact": "country", "url": e["url"], "saved": e.get("outcome") or ""})
            seen.add(e["url"])
    country_src = ((node.get("country") or {}).get("source") or "")
    if country_src.startswith(("http://", "https://")) and country_src not in seen:
        pages.append({"fact": "country", "url": country_src, "saved": "not saved"})
    return pages


def shown_row(node: dict, hierarchy: dict, n_parents: int, n_pages: int) -> Dict[str, str]:
    """Every value the export would write for this node, as text, without checks. Pure."""
    country = node.get("country") or {}
    unit_page = (conf.unit_evidence(node) or {}).get("url") or node.get("source_url") or ""
    row = {"node_id": node["node_id"], "level": _text(node.get("level")), "unit_type": _text(node.get("unit_type")),
           "in_scope": _text(node.get("in_scope")), "name": _text(node.get("name")),
           "aliases": _text(node.get("aliases")), "qid": node.get("qid") or "new",
           "alignment": _text(node.get("alignment")),
           "alignment_candidates": _candidates_text(node.get("alignment_candidates")),
           "wikidata_duplicates": _text(node.get("wikidata_duplicates")),
           "description": _text(node.get("description")), "website": _text(node.get("website")),
           "source_url": _text(node.get("source_url")),
           "country": _text(country.get("label")) + (f" ({country['qid']})" if country.get("qid") else ""),
           "country_source": _text(country.get("source"))}
    ps = conf.parents(node)
    for k in range(1, n_parents + 1):
        p = ps[k - 1] if k <= len(ps) else None
        row[f"parent_{k}"] = p["name"] if p else ""
        row[f"parent_{k}_qid"] = (p["qid"] or "new") if p else ""
        row[f"parent_{k}_p749"] = p["p749"] if p else ""
        row[f"parent_{k}_rank"] = p["rank"] if p else ""
        row[f"parent_{k}_qualifiers"] = _text(p["qualifiers"]) if p else ""
        row[f"parent_{k}_source"] = unit_page if p else ""
    row["flags"] = _text(node.get("flags"))
    pages = pages_of(node)
    for k in range(1, n_pages + 1):
        pg = pages[k - 1] if k <= len(pages) else {}
        row[f"page_{k}_fact"] = pg.get("fact", "")
        row[f"page_{k}_url"] = pg.get("url", "")
        row[f"page_{k}_saved"] = pg.get("saved", "")
    row["university_qid"] = hierarchy.get("university_qid") or ""
    row["run_id"] = node.get("run_id") or hierarchy.get("run_id") or ""
    row["node_version"] = conf.current_version(node)
    return row


def check_cells(checks: Sequence[dict], node: dict) -> Dict[str, str]:
    """The checks and the confidence of the node's current version, as cells. Pure."""
    mine = conf.current_checks(checks, node)
    level, reasons = conf.confidence(mine, [p["parent_id"] for p in conf.parents(node)])
    cells = {"confidence": level, "confidence_reasons": "; ".join(reasons)}
    for kind in conf.PAGE_KINDS + ("agreement",):
        parts = []
        for c in mine:
            if c["kind"] == kind:
                d = _detail(c).get("detail") or ""
                parts.append(c["result"] + (f" ({d})" if d else ""))
        cells[f"check_{kind}"] = "; ".join(parts)
    parts = []
    for c in mine:
        if c["kind"] == "verifier":
            d = _detail(c)
            quote = d.get("quote") or ""
            parts.append(f"{d.get('parent') or c.get('parent_id')}: {d.get('verdict') or c['result']}"
                         + (f' "{quote}"' if quote else f" ({d.get('reason')})" if d.get("reason") else ""))
    cells["check_verifier"] = "; ".join(parts)
    return cells


def _detail(check: dict) -> dict:
    d = check.get("detail")
    if isinstance(d, dict):
        return d
    try:
        return json.loads(d or "{}")
    except (TypeError, ValueError):
        return {}


def shown_hash(row: Dict[str, str], columns: Sequence[str]) -> str:
    """Hash of every shown value of a row (columns that are not filled in). Pure."""
    shown = {c: _text(row.get(c)).strip() for c in columns}
    return hashlib.sha256(json.dumps(shown, sort_keys=True).encode()).hexdigest()[:16]


def layout(n_parents: int, n_pages: int, review_pass: str) -> Tuple[List[str], List[str], List[str]]:
    """(shown columns, fill columns, all columns in order) of a sheet. Pure."""
    shown = list(UNIT_FIELDS)
    for k in range(1, n_parents + 1):
        shown += [f"parent_{k}{s}" for s in PARENT_FIELDS]
    shown.append("flags")
    for k in range(1, n_pages + 1):
        shown += [f"page_{k}{s}" for s in PAGE_FIELDS]
    if review_pass == "shown":
        shown += CHECK_FIELDS
    fill = ["verdict"] + [f"url_checked_{k}" for k in range(1, n_pages + 1)]
    fill += [f"fix_{f}" for f in FIXABLE_UNIT]
    for k in range(1, n_parents + 1):
        fill += [f"fix_parent_{k}{s}" for s in FIXABLE_PARENT]
    fill.append("notes")
    hashed = shown + [m for m in META_FIELDS if m != "shown_hash"]
    return hashed, fill, ["row"] + shown + fill + META_FIELDS


def build_rows(hierarchy: dict, review_pass: str, checks: Sequence[dict] = (), seed: int = SEED,
               limit: Optional[int] = None, node_ids: Optional[Iterable[str]] = None) -> Tuple[List[dict], List[str], List[str]]:
    """The rows of a sheet, its columns, and its fill columns. Blind: random order from the
    seed (the order of all nodes, so the first N rows are the same for every reviewer and
    any N). Shown: doubtful first (low, medium, high; more problems first), ties in the
    blind order. `node_ids` keeps only those nodes, in the same order. Pure."""
    if review_pass not in PASSES:
        raise ValueError(f"pass must be one of {PASSES}")
    nodes = conf.reviewable_nodes(hierarchy)
    n_parents = max([len(conf.parents(n)) for n in nodes] + [1])
    n_pages = max([len(pages_of(n)) for n in nodes] + [1])
    hashed, fill, columns = layout(n_parents, n_pages, review_pass)
    order = sorted(nodes, key=lambda n: n["node_id"])
    random.Random(seed).shuffle(order)
    if node_ids is not None:
        keep = set(node_ids)
        order = [n for n in order if n["node_id"] in keep]
    rows = []
    for n in order:
        r = shown_row(n, hierarchy, n_parents, n_pages)
        if review_pass == "shown":
            r.update(check_cells(checks, n))
        r["pass"] = review_pass
        rows.append(r)
    if review_pass == "shown":
        pos = {id(r): i for i, r in enumerate(rows)}
        rows.sort(key=lambda r: (conf.doubt_key(r["confidence"], [x for x in r["confidence_reasons"].split("; ") if x
                                                                   and r["confidence"] != "high"]), pos[id(r)]))
    if limit is not None:
        rows = rows[:limit]
    for i, r in enumerate(rows, 1):
        r["row"] = str(i)
        for c in fill:
            r[c] = ""
        r["shown_hash"] = shown_hash(r, hashed)
    return rows, columns, fill


# ─────────────────────────  THE SHEET FILE  ─────────────────────────

README_LINES = [
    "How to review (week 7). One row is one unit. Read every value on the row.",
    "1. Open each page in the page_<k>_url columns. Write yes or no in url_checked_<k>: yes means the page opens and supports its fact.",
    "2. Write accept, reject, or fix in the verdict column.",
    "3. accept: every page is yes, and the fix_ columns stay empty.",
    "4. fix: write the right value in the fix_ column of each wrong value. Write yes or no for every page.",
    "5. A fixed country needs fix_country_source (a URL). A fixed parent needs fix_parent_<k>_source (a URL).",
    "6. reject: the unit is not a real unit of its parent, or it should not be on Wikidata. Say why in notes.",
    "7. Do not change the other columns. Change only verdict, url_checked_<k>, fix_ columns, and notes.",
    "8. Do not delete the columns at the right end (pass, run_id, node_version, shown_hash).",
    "9. Note the time when you start and when you stop.",
]


def write_sheet(path: Path, rows: List[dict], columns: List[str], fill: List[str], title: str = "") -> Path:
    """An xlsx sheet with a README tab, drop-down lists for verdict and url_checked, and the
    fill columns shaded. A .csv path writes plain CSV instead."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".csv":
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=columns)
            w.writeheader()
            w.writerows([{c: r.get(c, "") for c in columns} for r in rows])
        return path
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation
    wb = Workbook()
    ws = wb.active
    ws.title = "review"
    ws.append(columns)
    for r in rows:
        ws.append([r.get(c, "") for c in columns])
    bold = Font(bold=True)
    fill_bg = PatternFill("solid", fgColor="FFF2CC")
    meta_bg = PatternFill("solid", fgColor="E7E6E6")
    check_bg = PatternFill("solid", fgColor="DDEBF7")
    n = len(rows) + 1
    for j, c in enumerate(columns, 1):
        cell = ws.cell(row=1, column=j)
        cell.font = bold
        letter = get_column_letter(j)
        if c in fill:
            cell.fill = fill_bg
            for i in range(2, n + 1):
                ws.cell(row=i, column=j).fill = fill_bg
        elif c in META_FIELDS:
            cell.fill = meta_bg
        elif c in CHECK_FIELDS:
            cell.fill = check_bg
        width = {"name": 40, "aliases": 30, "source_url": 40, "notes": 40, "flags": 30, "description": 30,
                 "alignment_candidates": 30, "confidence_reasons": 40, "check_verifier": 50}.get(c)
        if width is None:
            width = 40 if c.endswith(("_url", "_source")) else 14
        ws.column_dimensions[letter].width = width
    for i in range(2, n + 1):
        for j in range(1, len(columns) + 1):
            ws.cell(row=i, column=j).alignment = Alignment(vertical="top", wrap_text=False)
    ws.freeze_panes = ws.cell(row=2, column=columns.index("name") + 2)
    if rows:
        dv = DataValidation(type="list", formula1='"accept,reject,fix"', allow_blank=True)
        ws.add_data_validation(dv)
        col = get_column_letter(columns.index("verdict") + 1)
        dv.add(f"{col}2:{col}{n}")
        yn = DataValidation(type="list", formula1='"yes,no"', allow_blank=True)
        ws.add_data_validation(yn)
        for c in columns:
            if c.startswith("url_checked_"):
                col = get_column_letter(columns.index(c) + 1)
                yn.add(f"{col}2:{col}{n}")
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{n}"
    info = wb.create_sheet("README")
    for line in ([title] if title else []) + README_LINES:
        info.append([line])
    info.column_dimensions["A"].width = 140
    wb.save(path)
    return path


def read_sheet(path: Path) -> List[Dict[str, str]]:
    """The rows of a filled sheet (xlsx or csv), every value as stripped text. A CSV saved
    by Excel on Windows (cp1252) is read too."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252")
        rows = list(csv.DictReader(text.splitlines()))
    else:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb["review"] if "review" in wb.sheetnames else wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(it)]
        rows = [dict(zip(header, r)) for r in it]
        wb.close()
    out = []
    for r in rows:
        clean = {str(k).strip(): _cell(v) for k, v in r.items() if k}
        if any(clean.values()):
            out.append(clean)
    return out


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


# ─────────────────────────  LOADING VERDICTS  ─────────────────────────

YES = {"yes": "yes", "y": "yes", "no": "no", "n": "no"}


def sheet_layout(row: Dict[str, str]) -> Tuple[List[str], int, int]:
    """(hashed columns, number of parents, number of pages) of a sheet, from one row's keys."""
    n_parents = len([c for c in row if c.startswith("parent_") and c[7:].isdigit()])
    n_pages = len([c for c in row if c.startswith("page_") and c.endswith("_url")])
    hashed, _, _ = layout(n_parents, n_pages, row.get("pass") or "blind")
    return hashed, n_parents, n_pages


def verdict_of(row: Dict[str, str], hashed: Sequence[str], n_parents: int, n_pages: int) -> Tuple[Optional[dict], List[str]]:
    """(the verdict of one filled row, problems). No verdict gives (None, []). Pure."""
    verdict = row.get("verdict", "").strip().lower()
    if not verdict:
        return None, []
    where = f"row {row.get('row') or '?'} ({row.get('node_id')})"
    problems = []
    if verdict not in VERDICTS:
        return None, [f"{where}: verdict must be accept, reject, or fix, not {row.get('verdict')!r}"]
    if row.get("pass") not in PASSES:
        problems.append(f"{where}: the pass column is missing or changed")
    if shown_hash(row, hashed) != row.get("shown_hash"):
        problems.append(f"{where}: a shown value was changed. Change only verdict, url_checked, fix_ columns, and notes")
    corrections = {}
    fixable = list(FIXABLE_UNIT) + [f"parent_{k}{s}" for k in range(1, n_parents + 1) for s in FIXABLE_PARENT]
    for f in fixable:
        v = row.get(f"fix_{f}", "").strip()
        if v:
            corrections[f] = v
    checked = []
    for k in range(1, n_pages + 1):
        url = row.get(f"page_{k}_url", "")
        if not url:
            continue
        raw = row.get(f"url_checked_{k}", "").strip().lower()
        if raw and raw not in YES:
            problems.append(f"{where}: url_checked_{k} must be yes or no, not {raw!r}")
        checked.append({"page": k, "fact": row.get(f"page_{k}_fact", ""), "url": url, "checked": YES.get(raw, "")})
    if verdict == "accept":
        if corrections:
            problems.append(f"{where}: an accept has no fix_ values (use fix instead)")
        missing = [c["page"] for c in checked if c["checked"] != "yes"]
        if missing:
            problems.append(f"{where}: an accept needs url_checked yes for every page (page {', '.join(map(str, missing))})")
    if verdict == "fix":
        if not corrections:
            problems.append(f"{where}: a fix needs at least one fix_ value")
        missing = [c["page"] for c in checked if not c["checked"]]
        if missing:
            problems.append(f"{where}: a fix needs url_checked yes or no for every page (page {', '.join(map(str, missing))})")
        if "country" in corrections and "country_source" not in corrections:
            problems.append(f"{where}: a fixed country needs fix_country_source (a URL)")
        for k in range(1, n_parents + 1):
            link = [s for s in ("", "_qid", "_rank", "_qualifiers") if f"parent_{k}{s}" in corrections]
            if link and f"parent_{k}_source" not in corrections:
                problems.append(f"{where}: a fixed parent link needs fix_parent_{k}_source (a URL)")
        for f in ("source_url", "country_source", "website") + tuple(f"parent_{k}_source" for k in range(1, n_parents + 1)):
            v = corrections.get(f)
            if v and not v.startswith(("http://", "https://")):
                problems.append(f"{where}: fix_{f} must be a URL (http or https)")
    if problems:
        return None, problems
    return {"node_id": row.get("node_id"), "run_id": row.get("run_id"), "university_qid": row.get("university_qid"),
            "node_version": row.get("node_version"), "shown_hash": row.get("shown_hash"), "pass": row.get("pass"),
            "verdict": verdict, "corrections": corrections, "url_checked": checked,
            "notes": row.get("notes", "")}, []


def same_answer_key(r: dict) -> tuple:
    """Two rows with this key equal are the same verdict loaded twice. Pure."""
    def j(v):
        if isinstance(v, str):
            try:
                v = json.loads(v) if v else v
            except ValueError:
                pass
        return json.dumps(v, sort_keys=True)
    return (r.get("reviewer"), r.get("node_id"), r.get("node_version"), r.get("pass"), r.get("verdict"),
            j(r.get("corrections") or {}), j(r.get("url_checked") or []), (r.get("notes") or "").strip(),
            r.get("shown_hash"))


def plan_load(sheet_rows: Sequence[Dict[str, str]], reviewer: str, existing: Sequence[dict],
              source: str = "expert") -> Tuple[List[dict], List[str], int]:
    """(new reviews rows, problems, rows skipped as already loaded). Pure.

    Pass order is enforced per reviewer and node version: a shown verdict needs a blind
    verdict first (already saved or in this sheet), and a blind verdict is refused once the
    reviewer has a shown verdict on that version, because the checks were seen."""
    reviewer = (reviewer or "").strip().lower()
    if not reviewer:
        return [], ["give the reviewer's name (--reviewer)"], 0
    if source not in SOURCES:
        return [], [f"source must be one of {SOURCES}"], 0
    if not sheet_rows:
        return [], ["the sheet has no rows"], 0
    hashed, n_parents, n_pages = sheet_layout(sheet_rows[0])
    seen = {same_answer_key(r) for r in existing}
    passes = {(r.get("node_id"), r.get("node_version"), r.get("pass")) for r in existing
              if (r.get("reviewer") or "").lower() == reviewer}
    new, problems, skipped = [], [], 0
    for row in sheet_rows:
        v, p = verdict_of(row, hashed, n_parents, n_pages)
        problems += p
        if v is None:
            continue
        v.update({"reviewer": reviewer, "source": source})
        key = (v["node_id"], v["node_version"])
        if v["pass"] == "shown" and (*key, "blind") not in passes:
            problems.append(f"row {row.get('row')} ({v['node_id']}): a second-pass verdict needs a first-pass "
                            f"verdict by {reviewer} on the same version first")
            continue
        if v["pass"] == "blind" and (*key, "shown") in passes:
            problems.append(f"row {row.get('row')} ({v['node_id']}): {reviewer} already saw the checks for this "
                            f"version, so this verdict is not blind")
            continue
        if same_answer_key(v) in seen:
            skipped += 1
            continue
        seen.add(same_answer_key(v))
        passes.add((*key, v["pass"]))
        new.append(v)
    return new, problems, skipped


# ─────────────────────────  STATUS  ─────────────────────────

def _time(r: dict) -> str:
    return str(r.get("reviewed_at") or "")


def current_reviews(node: dict, reviews: Sequence[dict], university_qid: Optional[str] = None) -> List[dict]:
    """The verdicts on this node's current version. A verdict on an older version does not
    count. Pure."""
    version = conf.current_version(node)
    return [r for r in reviews if r.get("node_id") == node["node_id"] and r.get("node_version") == version
            and (not university_qid or not r.get("university_qid") or r["university_qid"] == university_qid)]


def latest_by_reviewer(reviews: Sequence[dict], review_pass: str) -> Dict[str, dict]:
    """Each reviewer's latest verdict in one pass. Pure."""
    out: Dict[str, dict] = {}
    for r in sorted((r for r in reviews if r.get("pass") == review_pass), key=_time):
        out[r["reviewer"]] = r
    return out


def status_rows(hierarchy: dict, reviews: Sequence[dict], reviewer: Optional[str] = None) -> List[dict]:
    """One row per node: reviewed or not, by whom, and each reviewer's latest blind and
    shown verdict on the current version. Pure."""
    qid = hierarchy.get("university_qid")
    rows = []
    for n in conf.reviewable_nodes(hierarchy):
        mine = [r for r in reviews if r.get("node_id") == n["node_id"]
                and (not qid or not r.get("university_qid") or r["university_qid"] == qid)]
        cur = current_reviews(n, mine, qid)
        if reviewer:
            cur = [r for r in cur if r.get("reviewer") == reviewer.lower()]
        blind, shown = latest_by_reviewer(cur, "blind"), latest_by_reviewer(cur, "shown")
        rows.append({"node_id": n["node_id"], "name": n.get("name", ""), "node_version": conf.current_version(n),
                     "status": "reviewed" if blind else "unreviewed",
                     "blind": "; ".join(f"{k}: {v['verdict']}" for k, v in sorted(blind.items())),
                     "shown": "; ".join(f"{k}: {v['verdict']}" for k, v in sorted(shown.items())),
                     "older_versions": len(mine) - len(current_reviews(n, mine, qid))})
    return rows


# ─────────────────────────  STORAGE  ─────────────────────────

LOCAL_STORE = RESULTS_DIR / "reviews_local.jsonl"


def read_reviews(university_qid: Optional[str] = None, local: Optional[Path] = None) -> List[dict]:
    """Every saved review of a university: from BigQuery, plus the rows that went only to
    the local file (when BigQuery could not be reached)."""
    rows: List[dict] = []
    if local is None:
        from google.cloud import bigquery
        from wikidata_discover import run_log
        client = bigquery.Client(project=config.GCP_PROJECT)
        q = f"SELECT * FROM `{run_log.table_ref('reviews')}`" + (" WHERE university_qid = @q" if university_qid else "")
        params = [bigquery.ScalarQueryParameter("q", "STRING", university_qid)] if university_qid else []
        rows = [dict(r) for r in client.query(q, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()]
    path = local or LOCAL_STORE
    if Path(path).exists():
        ids = {r["review_id"] for r in rows}
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("review_id") not in ids and (not university_qid or r.get("university_qid") == university_qid):
                    rows.append(r)
    for r in rows:
        for k in ("corrections", "url_checked"):
            if isinstance(r.get(k), str) and r[k]:
                try:
                    r[k] = json.loads(r[k])
                except ValueError:
                    pass
    return rows


def append_local(rows: Sequence[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True, default=str) + "\n")


EXPORT_COLUMNS = ["review_id", "reviewed_at", "reviewer", "source", "pass", "university_qid", "node_id",
                  "node_version", "run_id", "verdict", "corrections", "url_checked", "notes", "shown_hash",
                  "sheet_file", "load_run_id"]


def export_rows(reviews: Sequence[dict]) -> List[dict]:
    return [{c: (json.dumps(r.get(c), sort_keys=True) if isinstance(r.get(c), (dict, list)) else r.get(c, ""))
             for c in EXPORT_COLUMNS} for r in sorted(reviews, key=lambda r: (_time(r), r.get("node_id") or ""))]


# ─────────────────────────  COMMANDS  ─────────────────────────

def cmd_sheet(args) -> Path:
    hierarchy = conf.load_hierarchy(Path(args.hierarchy))
    checks: List[dict] = []
    if args.review_pass == "shown":
        checks = conf.read_checks_csv(Path(args.checks)) if args.checks else conf.read_checks_bigquery(
            conf.reviewable_nodes(hierarchy))
    keep = None
    if args.unreviewed or args.reviewed_by:
        reviews = read_reviews(hierarchy.get("university_qid"), Path(args.local) if args.local else None)
        status = {s["node_id"]: s for s in status_rows(hierarchy, reviews, args.reviewer or args.reviewed_by)}
        want = "unreviewed" if args.unreviewed else "reviewed"
        keep = [k for k, s in status.items() if s["status"] == want]
    rows, columns, fill = build_rows(hierarchy, args.review_pass, checks, seed=args.seed, limit=args.limit, node_ids=keep)
    qid = hierarchy.get("university_qid") or "unknown"
    out = Path(args.out) if args.out else RESULTS_DIR / f"{qid}_{args.review_pass}.xlsx"
    title = (f"{hierarchy.get('university_label') or qid} ({qid}), run {hierarchy.get('run_id')}, "
             f"pass {args.review_pass}, {len(rows)} rows, seed {args.seed}")
    write_sheet(out, rows, columns, fill, title)
    if args.review_pass == "shown":
        levels = {lv: sum(1 for r in rows if r["confidence"] == lv) for lv in conf.LEVELS}
        console.print(f"Confidence on the sheet: {levels}")
    console.print(f"{len(rows)} rows: {out}")
    return out


def cmd_load(args) -> int:
    from wikidata_discover import run_log
    from wikidata_discover.batch import operator_identity, source_identity
    path = Path(args.file)
    rows = read_sheet(path)
    qids = sorted({r.get("university_qid") for r in rows if r.get("university_qid")})
    local = Path(args.local) if args.local else None
    existing = read_reviews(qids[0] if len(qids) == 1 else None, local)
    new, problems, skipped = plan_load(rows, args.reviewer, existing, args.source)
    for p in problems:
        console.print(f"[red]{p}[/red]")
    if problems and not args.skip_invalid:
        console.print(f"[red]Nothing saved: {len(problems)} problems. Correct the sheet and load it again "
                      f"(or use --skip-invalid to save the {len(new)} good rows).[/red]")
        return 1
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    stamp = run_log.now_iso()
    for r in new:
        r.update({"sheet_file": f"{path.name} sha256:{sha}", "reviewed_at": stamp})
    if new and local is None:
        run_id = run_log.new_run_id("review-load")
        identity = source_identity(RESULTS_DIR)
        log = run_log.start(run_id, command="review load", args={"argv": sys.argv[1:]}, qids=qids,
                            who=operator_identity(), git_commit=identity,
                            run_config={"reviewer": args.reviewer, "source": args.source, "sheet": path.name,
                                        "sheet_sha256": sha})
        for r in new:
            r["review_id"] = log.add_review(r)
            r["load_run_id"] = run_id
        status = log.finish("ok", {"reviews": len(new), "skipped": skipped})
        if status["bigquery"] != "ok":
            append_local(new, LOCAL_STORE)
            console.print(f"[yellow]BigQuery failed ({status['bigquery']}); the rows are in {LOCAL_STORE}[/yellow]")
    elif new:
        for r in new:
            r["review_id"] = run_log.new_id()
        append_local(new, local)
    counts = {v: sum(1 for r in new if r["verdict"] == v) for v in VERDICTS}
    console.print(f"Saved {len(new)} verdicts by {args.reviewer} ({counts}); {skipped} already saved before.")
    return 0


def cmd_status(args) -> List[dict]:
    hierarchy = conf.load_hierarchy(Path(args.hierarchy))
    reviews = read_reviews(hierarchy.get("university_qid"), Path(args.local) if args.local else None)
    rows = status_rows(hierarchy, reviews, args.reviewer)
    out = Path(args.out) if args.out else RESULTS_DIR / f"{hierarchy.get('university_qid')}_status.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    from wikidata_discover.eval.agreement import write_csv
    write_csv(out, rows, ["node_id", "name", "node_version", "status", "blind", "shown", "older_versions"])
    n = sum(1 for r in rows if r["status"] == "reviewed")
    stale = sum(1 for r in rows if r["older_versions"])
    console.print(f"{n} of {len(rows)} units reviewed on their current version; {stale} units have verdicts "
                  f"on an older version only or as well. {out}")
    return rows


def cmd_export(args) -> Path:
    reviews = read_reviews(args.university, Path(args.local) if args.local else None)
    if args.reviewer:
        reviews = [r for r in reviews if r.get("reviewer") == args.reviewer.lower()]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    from wikidata_discover.eval.agreement import write_csv
    write_csv(out, export_rows(reviews), EXPORT_COLUMNS)
    console.print(f"{len(reviews)} reviews: {out}")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Review sheets and the reviews table (week 7).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sheet", help="write a review sheet from a hierarchy file")
    s.add_argument("hierarchy")
    s.add_argument("--pass", dest="review_pass", choices=PASSES, default="blind")
    s.add_argument("--checks", help="node_checks.csv of a confidence run (default: read checks from BigQuery)")
    s.add_argument("--limit", type=int, help="only the first N rows (the same N rows for every reviewer)")
    s.add_argument("--seed", type=int, default=SEED)
    s.add_argument("--unreviewed", action="store_true", help="only units with no verdict on their current version")
    s.add_argument("--reviewed-by", help="only units this reviewer judged in the first pass, on the current version")
    s.add_argument("--reviewer", help="with --unreviewed: units this reviewer has not judged")
    s.add_argument("--local", help="read reviews from this JSONL file instead of BigQuery")
    s.add_argument("--out", help="the sheet file (.xlsx, or .csv)")
    l_ = sub.add_parser("load", help="save the verdicts of a filled sheet as new reviews rows")
    l_.add_argument("file")
    l_.add_argument("--reviewer", required=True)
    l_.add_argument("--source", choices=SOURCES, default="expert")
    l_.add_argument("--skip-invalid", action="store_true", help="save the good rows even when other rows have problems")
    l_.add_argument("--local", help="write to this JSONL file instead of BigQuery (for a test)")
    t = sub.add_parser("status", help="which units are reviewed on their current version")
    t.add_argument("hierarchy")
    t.add_argument("--reviewer")
    t.add_argument("--local")
    t.add_argument("--out")
    e = sub.add_parser("export", help="write the saved reviews to a CSV")
    e.add_argument("--university")
    e.add_argument("--reviewer")
    e.add_argument("--local")
    e.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "sheet":
        cmd_sheet(args)
    elif args.cmd == "load":
        return cmd_load(args)
    elif args.cmd == "status":
        cmd_status(args)
    else:
        cmd_export(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
