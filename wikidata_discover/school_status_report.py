"""School status report for Panos: what run us-tier1 proposed at the school level, and what
Wikidata already has for each university.

Sends no query to Wikidata and changes no table. It reads:
- the legacy us-tier1 rows in BigQuery (candidates, legacy = true);
- the university list gs://academiabot/universities_us_tier1.json;
- the snapshots of run snapshot-us-tier1 (local copy under results/runs/, else the bucket).

It writes two files to wikidata_discover/results/reports/:
- school_status_report.xlsx (sheets Summary, By university, All schools);
- school_status_summary.md (one page in plain language).

    python -m wikidata_discover.school_status_report
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from wikidata_discover import config

SNAPSHOT_RUN = "snapshot-us-tier1"
STATUSES = ("linked", "orphan", "missing", "unresolved")
LINKED_NOTE = "us-tier1 did not save which item a linked unit matched"
# A unit_type that contains one of these words is flagged "likely not a school".
NOT_SCHOOL_WORDS = ("campus", "office", "program", "programme", "service", "administrative",
                    "administration", "library", "museum", "building", "facility", "site",
                    "location", "platform", "clinic", "chapel", "camp", "headquarters",
                    "athletics", "ministry", "guesthouse", "pathway", "major", "certificate",
                    "diploma", "course", "online")
OUT_DIR = config.RESULTS_DIR / "reports"


def type_group(unit_type: Optional[str]) -> str:
    """The unit_type as the AI gave it, with case and spaces made uniform."""
    return " ".join((unit_type or "").lower().split()) or "(none)"


def not_school_word(unit_type: Optional[str]) -> str:
    """The first word that marks this unit_type as likely not a school, or ""."""
    t = type_group(unit_type)
    return next((w for w in NOT_SCHOOL_WORDS if w in t), "")


def pct(n: int, total: int) -> float:
    return round(100 * n / total, 1) if total else 0.0


def summarize(cands: List[dict], universities: List[Tuple[str, str]]) -> dict:
    """The numbers of the Summary sheet. Pure."""
    total = len(cands)
    by_status = Counter(c["status"] for c in cands)
    by_type: Dict[str, Counter] = defaultdict(Counter)
    for c in cands:
        by_type[type_group(c.get("unit_type"))][c["status"]] += 1
    with_rows = {c["university_qid"] for c in cands}
    linked_unis = {c["university_qid"] for c in cands if c["status"] == "linked"}
    return {
        "universities_in_list": len(universities),
        "universities_with_schools": len(with_rows),
        "universities_not_included": [f"{n} ({q})" for q, n in sorted(universities) if q not in with_rows],
        "schools": total,
        "by_status": {s: (by_status.get(s, 0), pct(by_status.get(s, 0), total)) for s in STATUSES},
        "by_type": sorted(((t, sum(c.values()), dict(c)) for t, c in by_type.items()),
                          key=lambda x: (-x[1], x[0])),
        "flagged_not_school": sum(1 for c in cands if not_school_word(c.get("unit_type"))),
        "universities_with_0_linked": len(with_rows - linked_unis),
        "providers": dict(Counter(c.get("provider") or "(none)" for c in cands)),
    }


def snapshot_counts(snap: dict) -> Tuple[int, int]:
    """(units below the university at any depth, units directly below it)."""
    root = snap["university_qid"]
    units = [u for q, u in snap["units"].items() if u.get("in_tree") and q != root]
    return len(units), sum(1 for u in units if u.get("depth") == 1)


def university_rows(cands: List[dict], universities: List[Tuple[str, str]],
                    snapshots: Dict[str, dict]) -> List[dict]:
    """One row per university in the list, sorted by name. Pure."""
    counts: Dict[str, Counter] = defaultdict(Counter)
    for c in cands:
        counts[c["university_qid"]][c["status"]] += 1
    rows = []
    for qid, name in universities:
        cnt = counts.get(qid, Counter())
        all_units, direct = snapshot_counts(snapshots[qid]) if qid in snapshots else (None, None)
        rows.append({"QID": qid, "University": name, "Schools proposed": sum(cnt.values()),
                     "Linked": cnt["linked"], "Orphan": cnt["orphan"], "Missing": cnt["missing"],
                     "Unresolved": cnt["unresolved"],
                     "Units on Wikidata (all levels)": all_units,
                     "Units on Wikidata (directly below)": direct})
    return sorted(rows, key=lambda r: (r["University"].casefold(), r["QID"]))


def school_rows(cands: List[dict], names: Dict[str, str]) -> List[dict]:
    """One row per proposed school, sorted by university name and school name. Pure."""
    rows = []
    for c in cands:
        word = not_school_word(c.get("unit_type"))
        rows.append({"University": names.get(c["university_qid"], ""), "University QID": c["university_qid"],
                     "School": c["name"], "unit_type (as the AI gave it)": c.get("unit_type") or "",
                     "Status": c["status"], "Matched QID": c.get("matched_qid") or "",
                     "Matched label": c.get("matched_label") or "",
                     "Note": LINKED_NOTE if c["status"] == "linked" and not c.get("matched_qid") else "",
                     "Website": c.get("website") or "",
                     "Likely not a school": "yes" if word else "", "Flag reason": word})
    return sorted(rows, key=lambda r: (r["University"].casefold(), r["University QID"], r["School"].casefold()))


def summary_md(s: dict, generated: str) -> str:
    """The one-page summary in plain language. Pure."""
    st = s["by_status"]
    top = ", ".join(f"{t} ({n:,})" for t, n, _ in s["by_type"][:6])
    openai = s["providers"].get("openai", 0)
    excluded = ", ".join(s["universities_not_included"]) or "none"
    return f"""# School status report: run us-tier1

Generated {generated} from BigQuery and the Wikidata snapshots. No Wikidata query was sent.

## What the data is

- Run us-tier1 ran on 2026-10-03. It asked an LLM for the schools and colleges of each of
  {s['universities_in_list']:,} U.S. universities. This is the school level only, not departments.
- {openai:,} of {s['schools']:,} proposed schools ({pct(openai, s['schools'])}%) came from OpenAI.
- Each proposed school was compared with Wikidata. "Linked" means Wikidata already has the
  school under the university. "Orphan" means Wikidata has the school, but it is not linked to
  the university. "Missing" means the search found no item for it.
- "Units on Wikidata" comes from the snapshot of each university, taken 2026-10-20 and 2026-10-21.

## Headline counts

- Universities with proposed schools: {s['universities_with_schools']:,} of {s['universities_in_list']:,}.
- Proposed schools: {s['schools']:,}.
- Linked: {st['linked'][0]:,} ({st['linked'][1]}%). Orphan: {st['orphan'][0]:,} ({st['orphan'][1]}%).
  Missing: {st['missing'][0]:,} ({st['missing'][1]}%). Unresolved: {st['unresolved'][0]:,}.
- Universities with no linked school: {s['universities_with_0_linked']:,}.
- Most common unit types, as the AI gave them: {top}.
- {s['flagged_not_school']:,} proposed schools have a unit type such as campus, office, program, or
  service. The workbook flags them as "likely not a school".

## Caveats

1. "Missing" is too high. A hand check of 20 random "missing" units found that 1 is already on
   Wikidata (Penn State's graduate school, Q15265896) and 6 are not schools. With 20 units, the
   true rates are uncertain: about 1% to 24% already exist, and 15% to 52% are not schools.
2. "Orphan" matches are not reliable. In Anya's week 1 spot check, 3 of 4 orphans were matched
   to the wrong Wikidata item.
3. Linked rows have no matched QID. The run did not save which item a linked school matched, so
   those cells are empty in the workbook.
4. Not included: {excluded}. Its run failed, so it has no proposed schools.
5. "Units on Wikidata" counts every item linked to the university, not only schools. Endowed
   chairs, research centers, and departments are included, so it is not comparable with the
   number of proposed schools.

## Files

- `school_status_report.xlsx`: sheets Summary, By university, and All schools.
- Rebuild both files: `python -m wikidata_discover.school_status_report`.
"""


# ── reading and writing (I/O) ────────────────────────────────────────────

def read_candidates(bq) -> List[dict]:
    from wikidata_discover import run_log
    q = (f"SELECT university_qid, name, unit_type, status, matched_qid, matched_label, website, provider "
         f"FROM `{run_log.table_ref('candidates')}` WHERE run_id = 'us-tier1' AND legacy")
    return [dict(r) for r in bq.query(q).result()]


def read_snapshots(bucket, qids: Iterable[str]) -> Dict[str, dict]:
    local = config.RESULTS_DIR / "runs" / SNAPSHOT_RUN / "snapshots"
    out = {}
    for q in qids:
        f = local / f"{q}.json"
        if f.exists():
            out[q] = json.loads(f.read_text())
        else:
            blob = bucket.blob(f"runs/{SNAPSHOT_RUN}/snapshots/{q}.json")
            if blob.exists():
                out[q] = json.loads(blob.download_as_text())
    return out


def write_xlsx(path: Path, s: dict, unis: List[dict], schools: List[dict], generated: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    bold = Font(bold=True)
    lines = [("School status report, run us-tier1 (2026-10-03, school level)", None, None),
             (f"Generated {generated} from BigQuery (legacy us-tier1 candidates) and the snapshot-us-tier1 snapshots", None, None),
             (None, None, None),
             ("Universities in the us-tier1 list", s["universities_in_list"], None),
             ("Universities with proposed schools", s["universities_with_schools"], None),
             ("Universities not included", ", ".join(s["universities_not_included"]) or "none", None),
             ("Proposed schools", s["schools"], None),
             ("Universities with 0 linked schools", s["universities_with_0_linked"], None),
             ("Proposed schools flagged likely not a school", s["flagged_not_school"], None),
             (None, None, None),
             ("Status", "Schools", "Percent")]
    lines += [(st, n, p) for st, (n, p) in s["by_status"].items()]
    lines += [("Total", s["schools"], 100.0), (None, None, None),
              ("unit_type (as the AI gave it, case made uniform)", "Schools", "Percent", "Linked", "Orphan",
               "Missing", "Unresolved", "Likely not a school")]
    for t, n, c in s["by_type"]:
        lines.append((t, n, pct(n, s["schools"])) + tuple(c.get(st, 0) for st in STATUSES)
                     + ("yes" if not_school_word(t) else "",))
    for row in lines:
        ws.append(list(row))
    for r in (1, 11, 11 + len(STATUSES) + 3):
        for cell in ws[r]:
            cell.font = bold
    ws.column_dimensions["A"].width = 52
    for name, rows in (("By university", unis), ("All schools", schools)):
        sh = wb.create_sheet(name)
        sh.append(list(rows[0]))
        for cell in sh[1]:
            cell.font = bold
        for r in rows:
            sh.append(list(r.values()))
        sh.freeze_panes = "A2"
        sh.auto_filter.ref = sh.dimensions
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def main(argv=None) -> int:
    from datetime import datetime, timezone
    from google.cloud import bigquery, storage
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bucket = storage.Client(project=config.GCP_PROJECT).bucket(config.RUNLOG_BUCKET)
    universities = [tuple(u) for u in json.loads(bucket.blob("universities_us_tier1.json").download_as_text())]
    cands = read_candidates(bigquery.Client(project=config.GCP_PROJECT))
    snapshots = read_snapshots(bucket, [q for q, _ in universities])
    s = summarize(cands, universities)
    unis = university_rows(cands, universities, snapshots)
    schools = school_rows(cands, dict(universities))
    write_xlsx(OUT_DIR / "school_status_report.xlsx", s, unis, schools, generated)
    (OUT_DIR / "school_status_summary.md").write_text(summary_md(s, generated))
    config.console.print(f"{s['schools']:,} schools from {s['universities_with_schools']:,} universities; "
                         f"{len(snapshots):,} snapshots. Wrote {OUT_DIR}/school_status_report.xlsx and "
                         f"school_status_summary.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
