"""Score a list of departments against a hand-built department ground truth.

The ground truth is a CSV with one row per department and the columns
parent, name, type, source_url, qid. The qid is a Wikidata QID, or "none" when
someone searched Wikidata and found no item. Both files come from one university.

Usage:
    python -m wikidata_discover.eval.dept_scorer --truth gt.csv --proposed list.csv
    python -m wikidata_discover.eval.dept_scorer --truth gt.csv --proposed stern.csv \
        --parent "Stern School of Business"

The proposed list needs a name column. A parent column turns on edge F1 and
ancestor F1. A qid column turns on QID alignment accuracy (blank, "none" or
"new" means the list says the unit is new).

Matching rules (SPECS.md, Shuo week 2):
- One to one: one proposed row matches at most one true department.
- A name repeated under one parent counts once; the extra copy is a wrong row.
- Two departments with the same name under different schools are different rows.
- A match under the same parent always comes first. A proposed row can also match a
  true department of the same name under another school. That counts as found for
  name recall, but its parent link is wrong (edge F1), and it gets 1 of its 2
  ancestors right (ancestor F1).
- The assignment finds the most one-to-one pairs. Among those, it keeps the most pairs
  under the same parent, and then the most similar names.
"""

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pandas as pd
from rapidfuzz import fuzz
from rich.table import Table

from wikidata_discover.config import RESULTS_DIR, console
from wikidata_discover.discovery import is_fuzzy_match, normalize_name

TRUTH_COLUMNS = ["parent", "name", "type", "source_url", "qid"]
NEW = "new"  # the unit has no Wikidata item
QID_RE = re.compile(r"^Q[1-9]\d*$")

# Generic words at the start or end of a department name. The school-level
# matcher keeps them, so "Department of Economics" would not match "Economics".
_PREFIX_RE = re.compile(
    r"^\s*(?:the\s+)?(?:department|dept\.?|division|program|programme)\s+(?:of|in|for)\s+",
    re.IGNORECASE,
)
_SUFFIX_RE = re.compile(r"\s+(?:department|dept\.?)\s*$", re.IGNORECASE)

# Status of each row in the details file.
FOUND = "found"
WRONG_PARENT = "wrong_parent"
NOT_IN_KEY = "not_in_answer_key"
DUPLICATE = "duplicate"
PARENT_NOT_IN_KEY = "parent_not_in_answer_key"
MISSED = "missed"


class InputError(ValueError):
    """A CSV the scorer cannot use. `problems` lists every problem found."""

    def __init__(self, what: str, problems: List[str]):
        self.problems = problems
        super().__init__(f"{what}: " + "; ".join(problems))


@dataclass
class TrueDept:
    parent: str
    name: str
    type: str
    source_url: str
    qid: Optional[str]  # "Q..." or NEW; None when the cell is blank (not checked)


@dataclass
class GroundTruth:
    rows: List[TrueDept]
    warnings: List[str] = field(default_factory=list)


@dataclass
class ProposedDept:
    name: str
    parent: Optional[str] = None
    qid: Optional[str] = None  # "Q..." or NEW; None when the list has no qid column


@dataclass
class ProposedList:
    rows: List[ProposedDept]
    has_parents: bool
    has_qids: bool


# ── names ──────────────────────────────────────────────────────────────────


def strip_generic(name: str) -> str:
    """Remove "Department of", "Program in" and similar words around a name."""
    stripped = _SUFFIX_RE.sub("", _PREFIX_RE.sub("", name)).strip()
    return stripped or name.strip()


def dept_key(name: str) -> str:
    """Exact comparison key: two names with the same key are the same name."""
    return normalize_name(strip_generic(name))


def dept_names_match(a: str, b: str) -> bool:
    return is_fuzzy_match(strip_generic(a), strip_generic(b))


def _similarity(a: str, b: str) -> float:
    ka, kb = dept_key(a), dept_key(b)
    if ka == kb:
        return 101.0  # exact matches go first
    return float(fuzz.token_sort_ratio(ka, kb))


def _parse_qid(value: str, allow_new_words: Sequence[str]) -> Optional[str]:
    """Return "Q...", NEW, or raise ValueError. Blank returns None."""
    v = value.strip()
    if not v:
        return None
    if v.lower() in allow_new_words:
        return NEW
    if QID_RE.match(v.upper()):
        return v.upper()
    raise ValueError(v)


# ── loading ────────────────────────────────────────────────────────────────


def _read_csv(path: Path):
    """Return (column names, rows). A file with only a header has columns but no rows."""
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except FileNotFoundError:
        raise InputError(str(path), ["the file does not exist"])
    except (IsADirectoryError, PermissionError) as e:
        raise InputError(str(path), [f"the file cannot be read ({type(e).__name__})"])
    except pd.errors.EmptyDataError:
        raise InputError(str(path), ["the file is empty"])
    except (pd.errors.ParserError, UnicodeDecodeError) as e:
        raise InputError(str(path), [f"the file is not a readable CSV ({type(e).__name__}: {e})"])
    df.columns = [str(c).strip().lower() for c in df.columns]
    return list(df.columns), df.to_dict("records")


def parse_ground_truth(records: List[Dict[str, str]]) -> GroundTruth:
    """Check the ground-truth rows and return them. Raises InputError on bad rows."""
    problems: List[str] = []
    warnings: List[str] = []
    columns = set(records[0].keys()) if records else set()
    missing_cols = [c for c in TRUTH_COLUMNS if c not in columns]
    if not records:
        raise InputError("ground truth", ["the file has no rows"])
    if missing_cols:
        raise InputError("ground truth", [f"missing column(s): {', '.join(missing_cols)}"])

    rows: List[TrueDept] = []
    seen: Dict[tuple, int] = {}
    spelling: Dict[str, tuple] = {}  # parent key -> (first spelling, its row)
    for i, rec in enumerate(records, start=2):  # row 1 is the header
        name = (rec.get("name") or "").strip()
        parent = (rec.get("parent") or "").strip()
        if parent:
            first = spelling.setdefault(dept_key(parent), (parent, i))
            if first[0] != parent:
                warnings.append(f"row {i}: parent {parent!r} is written {first[0]!r} in row {first[1]}; "
                                f"using {first[0]!r}")
                parent = first[0]
        url = (rec.get("source_url") or "").strip()
        if not name:
            problems.append(f"row {i}: name is empty")
        if not parent:
            problems.append(f"row {i}: parent is empty")
        if not (rec.get("type") or "").strip():
            warnings.append(f"row {i}: type is empty")
        if not url:
            warnings.append(f"row {i}: source_url is empty")
        elif not url.lower().startswith(("http://", "https://")):
            problems.append(f"row {i}: source_url {url!r} does not start with http:// or https://")
        try:
            qid = _parse_qid(rec.get("qid") or "", ("none",))
        except ValueError as e:
            problems.append(f"row {i}: qid {e.args[0]!r} is not a QID (Q123) or \"none\"")
            qid = None
        else:
            if qid is None:
                warnings.append(f"row {i}: qid is empty, so this row is left out of alignment accuracy")
        if name and parent:
            key = (dept_key(parent), dept_key(name))
            if key in seen:
                problems.append(f"row {i}: {name!r} under {parent!r} repeats row {seen[key]}")
            else:
                seen[key] = i
        rows.append(TrueDept(parent, name, (rec.get("type") or "").strip(), url, qid))

    if problems:
        raise InputError("ground truth", problems)
    return GroundTruth(rows, warnings)


def load_ground_truth(path) -> GroundTruth:
    return parse_ground_truth(_read_csv(Path(path))[1])


def parse_proposed(records: List[Dict[str, str]], columns: Optional[Sequence[str]] = None) -> ProposedList:
    """Read a proposed list: a name column, and optional parent and qid columns.

    `columns` is the file header; give it so that a file with no rows is still checked.
    """
    columns = set(columns) if columns is not None else set(records[0].keys()) if records else set()
    name_col = "name" if "name" in columns else "unit" if "unit" in columns else None
    if name_col is None:
        raise InputError("proposed list", ["missing column: name"])
    has_parents = "parent" in columns
    has_qids = "qid" in columns
    problems: List[str] = []
    rows: List[ProposedDept] = []
    for i, rec in enumerate(records, start=2):
        name = (rec.get(name_col) or "").strip()
        if not name:
            if any((v or "").strip() for v in rec.values()):
                problems.append(f"row {i}: name is empty")
            continue  # a fully empty line is not a proposal
        parent = (rec.get("parent") or "").strip() if has_parents else None
        if has_parents and not parent:
            problems.append(f"row {i}: parent is empty")
        qid = None
        if has_qids:
            try:
                qid = _parse_qid(rec.get("qid") or "", ("none", "new")) or NEW
            except ValueError as e:
                problems.append(f"row {i}: qid {e.args[0]!r} is not a QID (Q123), \"none\" or \"new\"")
        rows.append(ProposedDept(name, parent, qid))
    if problems:
        raise InputError("proposed list", problems)
    return ProposedList(rows, has_parents, has_qids)


def load_proposed(path) -> ProposedList:
    columns, records = _read_csv(Path(path))
    return parse_proposed(records, columns)


# ── scoring ────────────────────────────────────────────────────────────────


@dataclass
class ScoreResult:
    metrics: Dict[str, Optional[float]]
    details: List[Dict[str, str]]


def _resolve_parent(name: str, truth_parents: List[str]) -> Optional[str]:
    """The answer-key parent that a proposed parent name refers to, or None."""
    key = dept_key(name)
    for p in truth_parents:
        if dept_key(p) == key:
            return p
    best = [p for p in truth_parents if dept_names_match(name, p)]
    if len(best) == 1:
        return best[0]
    if best:
        return None  # it fits two schools: never guess
    # A short form on one side: "Stern" in the list and "Stern School of Business" in
    # the answer key, or the other way round. Exactly one parent must fit.
    words = set(key.split())
    if words:
        fits = [
            p for p in truth_parents
            if words <= set(dept_key(p).split()) or set(dept_key(p).split()) <= words
        ]
        fits = [p for p in fits if dept_key(p)]
        if len(fits) == 1:
            return fits[0]
    return None


def _hungarian(cost: List[List[float]]) -> Dict[int, int]:
    """Minimum-cost assignment for an n x m matrix with n <= m. Returns row -> column."""
    n, m = len(cost), len(cost[0])
    inf = float("inf")
    u, v = [0.0] * (n + 1), [0.0] * (m + 1)
    p, way = [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [inf] * (m + 1), [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    return {p[j] - 1: j - 1 for j in range(1, m + 1) if p[j]}


def _match_pairs(pairs):
    """The best one-to-one assignment.

    pairs: (same_parent, similarity, p_index, t_index), one per allowed pair. The
    assignment has, in this order of priority: the most pairs, then the most pairs
    under the same parent, then the highest total name similarity.
    """
    if not pairs:
        return []
    props = sorted({pi for _, _, pi, _ in pairs})
    trues = sorted({ti for _, _, _, ti in pairs})
    n = max(len(props), len(trues)) + 1
    same_w = 102.0 * n            # beats any sum of similarities (each at most 101)
    pair_w = (same_w + 102.0) * n  # beats any sum of same-parent and similarity weights
    weight = {(pi, ti): pair_w + (same_w if same else 0.0) + sim for same, sim, pi, ti in pairs}

    rows, cols = (props, trues) if len(props) <= len(trues) else (trues, props)
    flip = rows is trues
    cost = [[-weight.get((c, r) if flip else (r, c), 0.0) for c in cols] for r in rows]
    out = []
    for ri, ci in _hungarian(cost).items():
        pi, ti = (cols[ci], rows[ri]) if flip else (rows[ri], cols[ci])
        if (pi, ti) in weight:
            out.append((pi, ti))
    return sorted(out)


def _prf(tp: float, n_pred: float, n_true: float):
    p = tp / n_pred if n_pred else None
    r = tp / n_true if n_true else None
    f = 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)
    return p, r, f


def score(truth: GroundTruth, proposed: ProposedList, parent: Optional[str] = None) -> ScoreResult:
    """Score a proposed list against the ground truth.

    `parent` scores a list without a parent column against one school only.
    """
    true_rows = list(truth.rows)
    props = list(proposed.rows)
    use_parents = proposed.has_parents

    if parent is not None:
        if proposed.has_parents:
            raise InputError("proposed list", ["use --parent only with a list that has no parent column"])
        target = _resolve_parent(parent, sorted({t.parent for t in true_rows}))
        if target is None:
            raise InputError("--parent", [f"{parent!r} is not a parent in the ground truth"])
        true_rows = [t for t in true_rows if t.parent == target]

    truth_parents = sorted({t.parent for t in true_rows})
    status: List[Optional[str]] = [None] * len(props)
    resolved: List[Optional[str]] = [None] * len(props)

    # Parent of each proposed row, as an answer-key parent.
    if use_parents:
        for i, p in enumerate(props):
            resolved[i] = _resolve_parent(p.parent, truth_parents)
            if resolved[i] is None:
                status[i] = PARENT_NOT_IN_KEY

    # A name repeated under one parent counts once.
    seen = set()
    for i, p in enumerate(props):
        if status[i] is not None:
            continue
        key = (resolved[i], dept_key(p.name))
        if key in seen:
            status[i] = DUPLICATE
        else:
            seen.add(key)

    open_p = [i for i in range(len(props)) if status[i] is None]
    matched_t: Dict[int, int] = {}  # proposed index -> truth index

    # One assignment over all name matches: the most pairs, then the most pairs under the
    # same parent, then the most similar names (see _match_pairs).
    pairs = []
    for i in open_p:
        for j, t in enumerate(true_rows):
            if not dept_names_match(props[i].name, t.name):
                continue
            same = not use_parents or t.parent == resolved[i]
            pairs.append((same, _similarity(props[i].name, t.name), i, j))
    for i, j in _match_pairs(pairs):
        matched_t[i] = j
        same = not use_parents or true_rows[j].parent == resolved[i]
        status[i] = FOUND if same else WRONG_PARENT

    for i in open_p:
        if status[i] is None:
            status[i] = NOT_IN_KEY

    # Counts.
    in_scope = [i for i in range(len(props)) if status[i] != PARENT_NOT_IN_KEY]
    n_pred, n_true = len(in_scope), len(true_rows)
    n_found = sum(1 for s in status if s in (FOUND, WRONG_PARENT))
    n_edge = sum(1 for s in status if s == FOUND)

    metrics: Dict[str, Optional[float]] = {
        "n_true": n_true,
        "n_proposed": n_pred,
        "n_found": n_found,
        "n_wrong_parent": sum(1 for s in status if s == WRONG_PARENT),
        "n_not_in_answer_key": sum(1 for s in status if s == NOT_IN_KEY),
        "n_duplicate": sum(1 for s in status if s == DUPLICATE),
        "n_missed": n_true - n_found,
        "n_parent_not_in_answer_key": len(props) - n_pred,
    }
    metrics["name_precision"], metrics["name_recall"], metrics["name_f1"] = _prf(n_found, n_pred, n_true)

    if use_parents:
        metrics["edge_precision"], metrics["edge_recall"], metrics["edge_f1"] = _prf(n_edge, n_pred, n_true)
        # Each department has 2 ancestors: its school and the university.
        anc_right = 2 * n_edge + (n_found - n_edge)
        metrics["ancestor_precision"], metrics["ancestor_recall"], metrics["ancestor_f1"] = _prf(
            anc_right, 2 * n_pred, 2 * n_true
        )
    else:
        for k in ("edge", "ancestor"):
            for m in ("precision", "recall", "f1"):
                metrics[f"{k}_{m}"] = None

    # QID alignment: right QID, or rightly new, over found departments with a checked QID.
    qid_result: Dict[int, str] = {}
    if proposed.has_qids:
        for i, j in matched_t.items():
            tq = true_rows[j].qid
            if tq is None:
                qid_result[i] = "not_checked"
            else:
                qid_result[i] = "right" if props[i].qid == tq else "wrong"
        n_align = sum(1 for v in qid_result.values() if v != "not_checked")
        n_right = sum(1 for v in qid_result.values() if v == "right")
        metrics["n_alignment_scored"] = n_align
        metrics["n_alignment_not_checked"] = len(qid_result) - n_align
        metrics["alignment_accuracy"] = n_right / n_align if n_align else None
    else:
        metrics["n_alignment_scored"] = 0
        metrics["n_alignment_not_checked"] = 0
        metrics["alignment_accuracy"] = None

    # One details row per proposed row, then one per missed true department.
    details: List[Dict[str, str]] = []
    for i, p in enumerate(props):
        t = true_rows[matched_t[i]] if i in matched_t else None
        details.append({
            "status": status[i],
            "proposed_name": p.name,
            "proposed_parent": p.parent or "",
            "proposed_qid": p.qid or "",
            "true_name": t.name if t else "",
            "true_parent": t.parent if t else "",
            "true_qid": (t.qid or "") if t else "",
            "qid_result": qid_result.get(i, ""),
        })
    found_t = set(matched_t.values())
    for j, t in enumerate(true_rows):
        if j not in found_t:
            details.append({
                "status": MISSED,
                "proposed_name": "",
                "proposed_parent": "",
                "proposed_qid": "",
                "true_name": t.name,
                "true_parent": t.parent,
                "true_qid": t.qid or "",
                "qid_result": "",
            })
    return ScoreResult(metrics, details)


# ── command line ───────────────────────────────────────────────────────────


def _fmt(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.3f}"


def print_result(result: ScoreResult) -> None:
    m = result.metrics
    table = Table(title="Department scores", show_header=True, header_style="bold magenta")
    table.add_column("Score")
    table.add_column("Precision", justify="right")
    table.add_column("Recall", justify="right")
    table.add_column("F1", justify="right")
    for k, label in (("name", "Names"), ("edge", "Edge (right school)"), ("ancestor", "Ancestor")):
        table.add_row(label, _fmt(m[f"{k}_precision"]), _fmt(m[f"{k}_recall"]), _fmt(m[f"{k}_f1"]))
    console.print(table)
    console.print(
        f"QID alignment accuracy: {_fmt(m['alignment_accuracy'])} "
        f"({m['n_alignment_scored']} departments scored, {m['n_alignment_not_checked']} with no QID in the answer key)"
    )
    console.print(
        f"{m['n_true']} true departments, {m['n_proposed']} proposed: {m['n_found']} found "
        f"({m['n_wrong_parent']} under the wrong school), {m['n_not_in_answer_key']} not in the answer key, "
        f"{m['n_duplicate']} repeated, {m['n_missed']} missed."
    )
    if m["n_parent_not_in_answer_key"]:
        console.print(
            f"[yellow]{m['n_parent_not_in_answer_key']} proposed rows have a parent that is not in the "
            f"answer key. They are not scored.[/yellow]"
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Score a department list against a department ground truth")
    parser.add_argument("--truth", required=True, help="Ground-truth CSV: parent, name, type, source_url, qid")
    parser.add_argument("--proposed", required=True, help="CSV to score: name, and optional parent and qid")
    parser.add_argument("--parent", help="Score a list with no parent column against this school only")
    parser.add_argument("--details", help="Where to write the per-row CSV "
                        "(default: wikidata_discover/results/dept_scores/<proposed>_vs_<truth>.csv)")
    args = parser.parse_args(argv)

    try:
        truth = load_ground_truth(args.truth)
        proposed = load_proposed(args.proposed)
        result = score(truth, proposed, parent=args.parent)
    except InputError as e:
        console.print(f"[red]Cannot score. {str(e).split(':')[0]} has these problems:[/red]")
        for p in e.problems:
            console.print(f"  - {p}")
        return 1

    for w in truth.warnings:
        console.print(f"[yellow]Ground truth {w}[/yellow]")
    print_result(result)

    out = Path(args.details) if args.details else (
        RESULTS_DIR / "dept_scores" / f"{Path(args.proposed).stem}_vs_{Path(args.truth).stem}.csv"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(result.details).to_csv(out, index=False)
    console.print(f"[green]Wrote one row per department to {out}[/green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
