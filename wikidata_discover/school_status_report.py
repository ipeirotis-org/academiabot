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
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from wikidata_discover import config

SNAPSHOT_RUN = "snapshot-us-tier1"
STATUSES = ("linked", "orphan", "missing", "unresolved")
LINKED_NOTE = "us-tier1 did not save which item a linked unit matched"
# Panos's scope rule (2026-10-29): schools, campuses, departments, and degree programs are
# in scope; offices are out. The groups are tried in this order and the first one whose
# words appear in the unit_type (whole words) wins, so "administrative division" is an
# office and "online campus" is a program. "other" is for a person to check.
TYPE_RULES = (
    ("office", ("office", "registrar", "service", "services", "administrative", "administration",
                "career", "affairs", "support", "headquarters", "legal division", "planning division",
                "communications division")),
    ("campus", ("regional center", "regional education center")),
    ("other", ("library", "museum", "chapel", "clinic", "building", "facility", "guesthouse", "camp",
               "athletics", "studio", "conference", "center", "research", "institute", "instituto",
               "training", "university", "institution", "affiliate", "unit", "unidad", "ministry")),
    ("program", ("program", "programme", "programa", "major", "pathway", "certificate", "diploma",
                 "degree", "online", "area of study", "academic area", "specialization",
                 "continuing education", "study abroad")),
    ("campus", ("campus", "site", "extension")),
    ("department", ("department", "departamento")),
    ("school", ("school", "schools", "college", "faculty", "facultad", "escuela", "division", "seminary",
                "conservatory", "academy", "graduate", "decanato", "كلية")),
)
IN_SCOPE = {"school": "yes", "campus": "yes", "department": "yes", "program": "yes", "office": "no",
            "other": "check"}
GROUPS = ("school", "campus", "department", "program", "office", "other")
OUT_DIR = config.RESULTS_DIR / "reports"


def norm_type(unit_type: Optional[str]) -> str:
    """The unit_type as the AI gave it, with case and spaces made uniform."""
    return " ".join((unit_type or "").lower().split()) or "(none)"


def type_group(unit_type: Optional[str]) -> str:
    """school, campus, department, program, office, or other, from the unit_type words."""
    t = norm_type(unit_type)
    for group, words in TYPE_RULES:
        if any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", t) for w in words):
            return group
    return "other"


def in_scope(unit_type: Optional[str]) -> str:
    return IN_SCOPE[type_group(unit_type)]


def pct(n: int, total: int) -> float:
    return round(100 * n / total, 1) if total else 0.0


def summarize(cands: List[dict], universities: List[Tuple[str, str]]) -> dict:
    """The numbers of the Summary sheet. Pure."""
    total = len(cands)
    by_status = Counter(c["status"] for c in cands)
    by_type: Dict[str, Counter] = defaultdict(Counter)
    by_group: Dict[str, Counter] = defaultdict(Counter)
    by_scope: Dict[str, Counter] = defaultdict(Counter)
    for c in cands:
        by_type[norm_type(c.get("unit_type"))][c["status"]] += 1
        by_group[type_group(c.get("unit_type"))][c["status"]] += 1
        by_scope[in_scope(c.get("unit_type"))][c["status"]] += 1
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
        "by_group": [(g, sum(by_group[g].values()), dict(by_group[g])) for g in GROUPS],
        "by_scope": [(k, sum(by_scope[k].values()), dict(by_scope[k])) for k in ("yes", "no", "check")],
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
        rows.append({"University": names.get(c["university_qid"], ""), "University QID": c["university_qid"],
                     "School": c["name"], "unit_type (as the AI gave it)": c.get("unit_type") or "",
                     "Status": c["status"], "Matched QID": c.get("matched_qid") or "",
                     "Matched label": c.get("matched_label") or "",
                     "Note": LINKED_NOTE if c["status"] == "linked" and not c.get("matched_qid") else "",
                     "Website": c.get("website") or "",
                     "type_group": type_group(c.get("unit_type")), "in_scope": in_scope(c.get("unit_type"))})
    return sorted(rows, key=lambda r: (r["University"].casefold(), r["University QID"], r["School"].casefold()))


def summary_md(s: dict, generated: str) -> str:
    """The one-page summary in plain language. Pure."""
    st = s["by_status"]
    openai = s["providers"].get("openai", 0)
    excluded = ", ".join(s["universities_not_included"]) or "none"

    def split(n, c):
        return (f"{n:,} ({pct(n, s['schools'])}%): {c.get('linked', 0):,} linked, "
                f"{c.get('orphan', 0):,} orphan, {c.get('missing', 0):,} missing")
    groups = "\n".join(f"  - {g}: {split(n, c)}" for g, n, c in s["by_group"])
    scope = {k: (n, c) for k, n, c in s["by_scope"]}
    return f"""# School status report: run us-tier1

Generated {generated} from BigQuery and the Wikidata snapshots. No Wikidata query was sent.

## What the data is

- Run us-tier1 ran on 2026-10-03. It asked an LLM for the schools and colleges of each of
  {s['universities_in_list']:,} U.S. universities. This is the school level only, not departments.
- {openai:,} of {s['schools']:,} proposed units ({pct(openai, s['schools'])}%) came from OpenAI.
- Each proposed unit was compared with Wikidata. "Linked" means Wikidata already has the
  unit under the university. "Orphan" means Wikidata has the unit, but it is not linked to
  the university. "Missing" means the search found no item for it.
- "Units on Wikidata" comes from the snapshot of each university, taken 2026-10-20 and 2026-10-21.

## Scope rule (Panos, 2026-10-29)

- In scope: schools, campuses, departments, and degree programs (online programs included).
- Out of scope: offices, for example the registrar, student services, career services, and
  administrative units.
- Each unit gets a type_group from the words of its unit_type: school, campus, department,
  program, office, or other. "Other" (centers, institutes, libraries, and similar) needs a
  person to check it.

## Headline counts

- Universities with proposed units: {s['universities_with_schools']:,} of {s['universities_in_list']:,}.
- Proposed units: {s['schools']:,}. Linked: {st['linked'][0]:,} ({st['linked'][1]}%). Orphan:
  {st['orphan'][0]:,} ({st['orphan'][1]}%). Missing: {st['missing'][0]:,} ({st['missing'][1]}%).
  Unresolved: {st['unresolved'][0]:,}.
- Universities with no linked unit: {s['universities_with_0_linked']:,}.
- In scope: {split(*scope["yes"])}.
- Out of scope (offices): {split(*scope["no"])}.
- To check (other): {split(*scope["check"])}.
- By type_group:
{groups}

## Week 3 baseline, recounted under the scope rule

Anya checked 20 random "missing" units by hand in week 3. Under Panos's rule:

- 1 is already on Wikidata: Penn State's graduate school (Q15265896).
- 2 are out of scope: an office of the registrar and a student services unit.
- 17 are in scope and missing: 12 schools, 1 campus, 3 degree programs, and 1 department.

The week 3 count was 6 "not schools". Under the new rule, 4 of those 6 are in scope: a campus
and 3 programs. With 20 units the true rates are uncertain: about 1% to 24% already exist, 3%
to 30% are out of scope, and 64% to 95% are in scope and missing.

## Caveats

1. "Missing" is too high. The baseline above found 1 of 20 already on Wikidata, and the search
   that made "missing" checks only the start of each label.
2. "Orphan" matches are not reliable. In Anya's week 1 spot check, 3 of 4 orphans were matched
   to the wrong Wikidata item.
3. Linked rows have no matched QID. The run did not save which item a linked unit matched, so
   those cells are empty in the workbook.
4. Not included: {excluded}. Its run failed, so it has no proposed units.
5. type_group comes from the words the LLM gave as unit_type, not from the unit's own page. In
   the 20 baseline rows it gave the right scope in all 20, but the right group in only 18.
6. "Units on Wikidata" counts every item linked to the university, not only schools. Endowed
   chairs, research centers, and departments are included, so it is not comparable with the
   number of proposed units.

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


def summary_lines(s: dict, generated: str) -> Tuple[List[tuple], List[int]]:
    """The rows of the Summary sheet, and which rows are headings. Pure."""
    status_cols = ("Linked", "Orphan", "Missing", "Unresolved")

    def split(c):
        return tuple(c.get(st, 0) for st in STATUSES)
    lines: List[tuple] = [
        ("School status report, run us-tier1 (2026-10-03, school level)",),
        (f"Generated {generated} from BigQuery (legacy us-tier1 candidates) and the snapshot-us-tier1 snapshots",),
        ("Scope rule (Panos, 2026-10-29): schools, campuses, departments, and degree programs are in scope; "
         "offices are out; other is for a person to check",),
        (),
        ("Universities in the us-tier1 list", s["universities_in_list"]),
        ("Universities with proposed units", s["universities_with_schools"]),
        ("Universities not included", ", ".join(s["universities_not_included"]) or "none"),
        ("Proposed units", s["schools"]),
        ("Universities with 0 linked units", s["universities_with_0_linked"]),
        ()]
    heads = [1]
    heads.append(len(lines) + 1)
    lines.append(("Status", "Units", "Percent"))
    lines += [(st, n, p) for st, (n, p) in s["by_status"].items()]
    lines += [("Total", s["schools"], 100.0), ()]
    heads.append(len(lines) + 1)
    lines.append(("in_scope", "Units", "Percent") + status_cols)
    lines += [(k, n, pct(n, s["schools"])) + split(c) for k, n, c in s["by_scope"]]
    lines.append(())
    heads.append(len(lines) + 1)
    lines.append(("type_group", "Units", "Percent") + status_cols + ("in_scope",))
    lines += [(g, n, pct(n, s["schools"])) + split(c) + (IN_SCOPE[g],) for g, n, c in s["by_group"]]
    lines.append(())
    heads.append(len(lines) + 1)
    lines.append(("unit_type (as the AI gave it, case made uniform)", "Units", "Percent") + status_cols
                 + ("type_group", "in_scope"))
    lines += [(t, n, pct(n, s["schools"])) + split(c) + (type_group(t), in_scope(t)) for t, n, c in s["by_type"]]
    return lines, heads


def write_xlsx(path: Path, s: dict, unis: List[dict], schools: List[dict], generated: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    bold = Font(bold=True)
    lines, heads = summary_lines(s, generated)
    for row in lines:
        ws.append(list(row))
    for r in heads:
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
