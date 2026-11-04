"""
Parallel extraction: do independent LLMs agree? (Shuo, week 3; see TASKS.md and SPECS.md)

Each provider lists the schools of each university on its own, several times (samples).
Name variants of one school are grouped. Each group gets one `checks` row (kind
`agreement`) that says which providers and runs named it. The groups are then scored
against the ground truth, and the overlap between providers estimates how many schools
each university has (research question S1, capture and recapture).

Usage:
    python -m wikidata_discover.eval.agreement                      # 12 eval + small universities
    python -m wikidata_discover.eval.agreement --universities Q49210 --providers openai gemini
    python -m wikidata_discover.eval.agreement --compare DIR_A DIR_B # are two runs identical?
    python -m wikidata_discover.eval.agreement --draw-small --seed 2026

Output (wikidata_discover/results/agreement/<run_id>/):
    proposals.csv               one row per school a provider named in one sample
    groups.csv                  one row per grouped school: agreement and score
    precision_by_agreement.csv  precision at each agreement level
    cross_provider.csv          how often B names a wrong school when A did
    wrong_by_provider.csv       each provider's wrong schools: invented, non-academic, ...
    s1_estimates.csv            capture-recapture school counts next to the true count
    single_provider_review.csv  a random sample of one-provider schools for a person to judge
    cache_files.csv             the cache file of every provider, university, and sample
    ids.csv                     candidate and LLM call ids (the only file that differs
                                between two runs of the same answers)
"""

import argparse
import csv
import json
import random
import re
import sys
from itertools import combinations
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from wikidata_discover.discovery import is_fuzzy_match

PROVIDERS = ("openai", "anthropic", "gemini")
DEFAULT_SAMPLES = 3
REVIEW_SIZE = 20
REVIEW_SEED = 2026
EVAL_DIR = Path(__file__).parent
SITELINKS_SNAPSHOT = EVAL_DIR / "us_tier1_sitelinks.csv"
TIER1_OBJECT = "universities_us_tier1.json"

# A wrong school whose name (or the type the LLM gave) has one of these words is a
# non-academic unit, not an invented one (AGENTS.md known issue 15).
NON_ACADEMIC = re.compile(
    r"\b(librar(y|ies)|athletics?|hospitals?|health system|medical cent(er|re)|museums?|press|"
    r"careers?|student (affairs|life|services|success)|admissions?|registrar|office|alumni|"
    r"foundation|administrative|services|bookstore|police|housing|dining|"
    r"financial aid|human resources|facilities|support)\b", re.I)
CAMPUS = re.compile(r"\bcampus(es)?\b", re.I)


# ─────────────────────────  GROUPING  ─────────────────────────

def provider_order(provider: str) -> int:
    return PROVIDERS.index(provider) if provider in PROVIDERS else len(PROVIDERS)


def group_names(proposals: Sequence[dict], match: Callable[[str, str], bool] = is_fuzzy_match) -> List[List[int]]:
    """Group the proposals of one university by name. Returns lists of indices into
    `proposals`. A proposal joins the first group (in order of creation) that has a
    member whose name matches; otherwise it starts a new group. The proposals are
    visited in a fixed order (provider, sample, position in the answer), so the same
    answers always give the same groups."""
    order = sorted(range(len(proposals)), key=lambda i: (
        provider_order(proposals[i]["provider"]), proposals[i]["sample"], proposals[i].get("index", 0), i))
    groups: List[List[int]] = []
    for i in order:
        name = proposals[i]["name"]
        for g in groups:
            if any(match(name, proposals[j]["name"]) for j in g):
                g.append(i)
                break
        else:
            groups.append([i])
    return groups


_SMALL_WORDS = {"of", "the", "at", "and", "in", "for"}


def university_names(label: str) -> List[str]:
    """Ways a school name can include its university: the full name, the name without
    "University" or "University of" ("Columbia", "California, Berkeley"), the part after
    a comma and "UC" with it ("Berkeley", "UC Berkeley"), and the initials ("NYU", "MIT").
    Longest first, so the longest one is removed."""
    label = re.sub(r"^the\s+", "", label.strip(), flags=re.I)
    core = re.sub(r"\b(university of|university|the)\b", " ", label, flags=re.I)
    core = re.sub(r"\s+", " ", core).strip(" ,")
    out = {label, core}
    if "," in label:
        tail = label.rsplit(",", 1)[1].strip()
        out |= {tail, f"UC {tail}"}
    words = [w for w in re.findall(r"[A-Za-z]+", label) if w.lower() not in _SMALL_WORDS]
    if len(words) >= 2:
        out.add("".join(w[0].upper() for w in words))
    return sorted((n for n in out if len(n) >= 2), key=len, reverse=True)


def strip_university(name: str, label: str) -> str:
    """The school name without its university's name, possessive, or a note in
    brackets: "Stanford Law School" -> "Law School", "Columbia University School of
    Nursing" -> "School of Nursing". The name itself when nothing would be left."""
    out = re.sub(r"\([^)]*\)", " ", name)
    for u in university_names(label):
        out = re.sub(rf"(?<![A-Za-z]){re.escape(u)}(?:'s)?(?![A-Za-z])", " ", out, flags=re.I)
    out = re.sub(r"\s+", " ", out).strip(" ,-:")
    return out or name


def university_matcher(label: str) -> Callable[[str, str], bool]:
    """is_fuzzy_match, also tried with the university's name removed from both names,
    so "Stanford Law School" matches "School of Law" at Stanford."""
    if not label:
        return is_fuzzy_match

    def match(a: str, b: str) -> bool:
        return is_fuzzy_match(a, b) or is_fuzzy_match(strip_university(a, label), strip_university(b, label))
    return match


def group_label(names: Sequence[str]) -> str:
    """The most frequent exact name of a group; the first one seen on a tie."""
    counts: Dict[str, int] = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    return max(counts, key=lambda n: (counts[n], -names.index(n)))


def run_label(provider: str, sample: int) -> str:
    return f"{provider}#{sample}"


# ─────────────────────────  MATCHING TO THE GROUND TRUTH  ─────────────────────────

def match_groups_to_truth(group_names_list: Sequence[Sequence[str]], truth: Sequence[str],
                          match: Callable[[str, str], bool] = is_fuzzy_match) -> Tuple[Dict[int, int], Dict[int, int]]:
    """One-to-one matching of groups to true schools. A group can match a true school
    when any of its names matches that school's name. Returns (matched, duplicates):
    matched maps group index to true school index for a maximum matching; duplicates
    maps each other group that matches some true school to the first such school (it
    names a real school that another group already took, so it counts as wrong)."""
    edges = [[t for t, tname in enumerate(truth) if any(match(n, tname) for n in names)]
             for names in group_names_list]
    owner: Dict[int, int] = {}   # true school -> group

    def augment(g: int, seen: set) -> bool:
        for t in edges[g]:
            if t in seen:
                continue
            seen.add(t)
            if t not in owner or augment(owner[t], seen):
                owner[t] = g
                return True
        return False

    # Groups named by more runs go first, so an ambiguous true school goes to the
    # stronger group when the maximum matching allows either.
    for g in sorted(range(len(edges)), key=lambda g: (-len(group_names_list[g]), g)):
        augment(g, set())
    matched = {g: t for t, g in owner.items()}
    duplicates = {g: edges[g][0] for g in range(len(edges)) if g not in matched and edges[g]}
    return matched, duplicates


def wrong_kind(names: Iterable[str], unit_types: Iterable[str]) -> str:
    """Why a group that matches no true school is wrong: non_academic (a library,
    an office, a hospital), campus, or invented (anything else: not a unit of this
    university as far as the ground truth knows)."""
    text = " | ".join([n for n in names if n] + [t for t in unit_types if t])
    if NON_ACADEMIC.search(text):
        return "non_academic"
    if CAMPUS.search(" | ".join(n for n in names if n)):
        return "campus"
    return "invented"


# ─────────────────────────  CAPTURE AND RECAPTURE (S1)  ─────────────────────────

def chapman(n1: int, n2: int, m: int) -> float:
    """Chapman's estimate of the number of units from two lists: n1 and n2 units,
    m on both. Defined even when m is 0."""
    return (n1 + 1) * (n2 + 1) / (m + 1) - 1


def mt_estimate(captures: Sequence[set]) -> Optional[float]:
    """Estimate of the number of units from k lists that are assumed independent
    (model M_t): the N that solves prod(1 - n_j / N) = 1 - r / N, where n_j is the
    size of list j and r the number of distinct units seen. With two lists this is
    Lincoln and Petersen's n1 * n2 / m. None when no unit is on two lists (the
    estimate has no upper bound)."""
    sizes = [len(c) for c in captures]
    r = len(set().union(*captures)) if captures else 0
    if r == 0:
        return 0.0
    if sum(sizes) == r:
        return None

    def f(n: float) -> float:
        prod = 1.0
        for s in sizes:
            prod *= 1 - s / n
        return prod - (1 - r / n)

    lo, hi = float(r), float(r) * 2
    if f(lo) <= 0:   # every list saw every unit
        return float(r)
    while f(hi) > 0:
        hi *= 2
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ─────────────────────────  ANALYSIS  ─────────────────────────

def analyze_university(qid: str, proposals: List[dict], truth: Sequence[str],
                       answered_runs: Sequence[Tuple[str, int]], university: str = "") -> List[dict]:
    """Group the proposals of one university and score each group. Returns one dict
    per group, in a fixed order (first seen first). `answered_runs` lists the
    (provider, sample) pairs that returned an answer, the denominator of the score.
    `university` is its name, which is ignored inside school names when they are compared."""
    match = university_matcher(university)
    idx_groups = group_names(proposals, match)
    names = [[proposals[i]["name"] for i in g] for g in idx_groups]
    matched, duplicates = match_groups_to_truth(names, truth, match)
    total_runs = len(answered_runs) or 1
    out = []
    for gi, g in enumerate(idx_groups):
        members = [proposals[i] for i in g]
        providers = sorted({m["provider"] for m in members}, key=provider_order)
        runs = sorted({(m["provider"], m["sample"]) for m in members}, key=lambda r: (provider_order(r[0]), r[1]))
        if gi in matched:
            status, truth_name, kind = "correct", truth[matched[gi]], ""
        elif gi in duplicates:
            status, truth_name, kind = "wrong", truth[duplicates[gi]], "duplicate"
        else:
            status, truth_name = "wrong", ""
            kind = wrong_kind([m["name"] for m in members], [m.get("unit_type") or "" for m in members])
        variants = []
        for m in members:
            if m["name"] not in variants:
                variants.append(m["name"])
        out.append({
            "university_qid": qid, "group_id": f"{qid}-g{gi + 1:02d}",
            "name": group_label([m["name"] for m in members]), "variants": variants,
            "providers": providers, "n_providers": len(providers),
            "runs": [run_label(p, s) for p, s in runs], "n_runs": len(runs),
            "score": round(len(runs) / total_runs, 4), "status": status,
            "truth_name": truth_name, "wrong_kind": kind, "member_index": list(g),
        })
    return out


def precision_by_agreement(groups: List[dict], n_providers: int, n_samples: int) -> List[dict]:
    """Precision of the groups at each agreement level: named by exactly k providers,
    by at least k providers, and by exactly k runs."""
    rows = []
    for uset in sorted({g["university_set"] for g in groups}) + ["all"]:
        gs = [g for g in groups if uset == "all" or g["university_set"] == uset]

        def row(level, k, sel):
            n = len(sel)
            c = sum(1 for g in sel if g["status"] == "correct")
            return {"university_set": uset, "level": level, "k": k, "schools": n, "correct": c,
                    "wrong": n - c, "precision": round(c / n, 4) if n else ""}
        for k in range(1, n_providers + 1):
            rows.append(row("providers_exactly", k, [g for g in gs if g["n_providers"] == k]))
        for k in range(1, n_providers + 1):
            rows.append(row("providers_at_least", k, [g for g in gs if g["n_providers"] >= k]))
        for k in range(1, n_providers * n_samples + 1):
            rows.append(row("runs_exactly", k, [g for g in gs if g["n_runs"] == k]))
    return rows


def cross_provider(groups: List[dict], providers: Sequence[str]) -> List[dict]:
    """For each ordered pair (A, B): of the wrong schools A named, the share B also
    named; and the same for correct schools, as the baseline. When B names A's
    wrong schools far more often than chance, the two make correlated errors."""
    rows = []
    for a in providers:
        for b in providers:
            if a == b:
                continue
            a_wrong = [g for g in groups if a in g["providers"] and g["status"] == "wrong"]
            a_right = [g for g in groups if a in g["providers"] and g["status"] == "correct"]
            bw = sum(1 for g in a_wrong if b in g["providers"])
            br = sum(1 for g in a_right if b in g["providers"])
            rows.append({"provider_a": a, "provider_b": b,
                         "a_wrong": len(a_wrong), "b_also_named_wrong": bw,
                         "rate_wrong": round(bw / len(a_wrong), 4) if a_wrong else "",
                         "a_correct": len(a_right), "b_also_named_correct": br,
                         "rate_correct": round(br / len(a_right), 4) if a_right else ""})
    return rows


WRONG_KINDS = ("invented", "non_academic", "campus", "duplicate")


def wrong_by_provider(groups: List[dict], providers: Sequence[str]) -> List[dict]:
    """Each provider's schools (groups it named in any sample), and its wrong ones by
    kind: invented, non_academic, campus, duplicate."""
    rows = []
    for uset in sorted({g["university_set"] for g in groups}) + ["all"]:
        gs = [g for g in groups if uset == "all" or g["university_set"] == uset]
        for p in providers:
            mine = [g for g in gs if p in g["providers"]]
            wrong = [g for g in mine if g["status"] == "wrong"]
            row = {"university_set": uset, "provider": p, "schools": len(mine), "wrong": len(wrong)}
            for k in WRONG_KINDS:
                row[k] = sum(1 for g in wrong if g["wrong_kind"] == k)
            row["only_this_provider_wrong"] = sum(1 for g in wrong if g["n_providers"] == 1)
            rows.append(row)
    return rows


def capture_sets(groups: List[dict], proposals: List[dict], mode: str) -> Dict[Tuple[str, int], set]:
    """For each (provider, sample): the set of units it named. mode `all` counts every
    group; mode `matched` counts only groups that match a true school (correct, or a
    duplicate of one), by that school, as a perfect verifier would."""
    caps: Dict[Tuple[str, int], set] = {}
    for g in groups:
        if mode == "matched" and not g["truth_name"]:
            continue
        unit = g["truth_name"] if mode == "matched" else g["group_id"]
        for i in g["member_index"]:
            p = proposals[i]
            caps.setdefault((p["provider"], p["sample"]), set()).add(unit)
    return caps


def s1_estimates(qid: str, label: str, uset: str, true_count: int, groups: List[dict],
                 proposals: List[dict], providers: Sequence[str],
                 answered_runs: Sequence[Tuple[str, int]]) -> List[dict]:
    """S1 for one university: estimates from each pair of providers (Chapman), from
    all providers together (M_t), and from the repeated samples of one provider
    (M_t), each counted two ways (`all` and `matched`)."""
    rows = []
    samples_of = {p: sorted(s for q, s in answered_runs if q == p) for p in providers}
    for mode in ("all", "matched"):
        caps = capture_sets(groups, proposals, mode)
        by_provider = {p: set().union(*(caps.get((p, s), set()) for s in samples_of[p])) for p in providers}

        def add(method, sources, estimate, seen):
            rows.append({"university_qid": qid, "university": label, "university_set": uset,
                         "mode": mode, "method": method, "sources": "+".join(sources),
                         "observed": len(seen),
                         "estimate": "" if estimate is None else round(estimate, 2),
                         "true_count": true_count,
                         "estimate_minus_true": "" if estimate is None else round(estimate - true_count, 2),
                         "ratio_to_true": "" if estimate is None or not true_count else round(estimate / true_count, 4)})
        live = [p for p in providers if samples_of[p]]
        for a, b in combinations(live, 2):
            A, B = by_provider[a], by_provider[b]
            add("chapman_pair", [a, b], chapman(len(A), len(B), len(A & B)), A | B)
        if len(live) >= 2:
            sets = [by_provider[p] for p in live]
            add("mt_providers", live, mt_estimate(sets), set().union(*sets))
        for p in live:
            if len(samples_of[p]) >= 2:
                sets = [caps.get((p, s), set()) for s in samples_of[p]]
                add("mt_samples", [run_label(p, s) for s in samples_of[p]], mt_estimate(sets), set().union(*sets))
    return rows


def review_sample(groups: List[dict], size: int = REVIEW_SIZE, seed: int = REVIEW_SEED) -> List[dict]:
    """A random sample of schools that only one provider named, for a person to judge
    (TASKS.md, Shuo week 3, "You do yourself" step 2). Same seed, same sample."""
    pool = sorted((g for g in groups if g["n_providers"] == 1), key=lambda g: g["group_id"])
    chosen = random.Random(seed).sample(pool, min(size, len(pool)))
    return sorted(chosen, key=lambda g: g["group_id"])


# ─────────────────────────  DRAWING THE SMALL UNIVERSITIES  ─────────────────────────

DRAW_SEED = 2026
DRAW_ORDER_SIZE = 20
# A person looked up each drawn university in order (2026-11-04, Shuo's rule): one with no
# top-level academic unit is skipped, and the first 6 with at least one are kept.
SKIPPED_NO_UNITS = {
    "Q6841624": "Middle Tennessee School of Anesthesia: one program, no units",
    "Q38251806": "Williamsburg Technical College: no named units found",
    "Q6467825": "Lac Courte Oreilles Ojibwe University: no named units found",
}
SMALL_UNIVERSITIES = ["Q4929506", "Q7114753", "Q7896431", "Q7208107", "Q7122600", "Q7429134"]


def draw_small(universities: Sequence[Tuple[str, str, int]], seed: int, k: int = DRAW_ORDER_SIZE) -> List[Tuple[str, str, int]]:
    """The first k universities of a random order of the bottom third of the list by
    Wikidata sitelinks. Rows are (qid, name, sitelinks). Ties are broken by the QID
    number, so the same list and seed always give the same order, and the first items
    are the same for any k."""
    ordered = sorted(universities, key=lambda u: (u[2], int(u[0][1:])))
    bottom = ordered[: len(ordered) // 3]
    return random.Random(seed).sample(bottom, k)


def keep_with_units(order: Sequence[Tuple[str, str, int]], skipped: Iterable[str], k: int = 6) -> List[str]:
    """Walk the drawn order and keep the first k universities that are not skipped
    (skipped: a person found no top-level academic unit)."""
    skipped = set(skipped)
    return [q for q, _, _ in order if q not in skipped][:k]


def fetch_sitelinks(qids: Sequence[str], batch: int = 200) -> Dict[str, int]:
    """Sitelink counts of the given items from the Wikidata SPARQL endpoint."""
    import time
    from wikidata_discover.sparql_helpers import execute_sparql_bindings
    out: Dict[str, int] = {}
    for i in range(0, len(qids), batch):
        values = " ".join(f"wd:{q}" for q in qids[i:i + batch])
        q = f"SELECT ?item ?n WHERE {{ VALUES ?item {{ {values} }} ?item wikibase:sitelinks ?n . }}"
        for b in execute_sparql_bindings(q):
            out[b["item"]["value"].rsplit("/", 1)[-1]] = int(b["n"]["value"])
        time.sleep(0.3)
    return out


def load_tier1() -> List[Tuple[str, str]]:
    from google.cloud import storage
    from wikidata_discover import config
    blob = storage.Client(project=config.GCP_PROJECT).bucket(config.RUNLOG_BUCKET).blob(TIER1_OBJECT)
    return [(q, n) for q, n in json.loads(blob.download_as_text())]


def sitelinks_table(path: Path = SITELINKS_SNAPSHOT) -> List[Tuple[str, str, int]]:
    """The us-tier1 universities with their sitelink counts. Read from the saved
    snapshot when it exists (live counts drift), else fetched and saved."""
    if path.exists():
        with path.open() as f:
            return [(r["qid"], r["name"], int(r["sitelinks"])) for r in csv.DictReader(f)]
    tier1 = load_tier1()
    counts = fetch_sitelinks([q for q, _ in tier1])
    rows = [(q, n, counts.get(q, 0)) for q, n in tier1]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["qid", "name", "sitelinks"])
        w.writerows(rows)
    return rows


# ─────────────────────────  RUNNING THE PROVIDERS  ─────────────────────────

def extractor(provider: str):
    from wikidata_discover.llm_helpers import LLMHelper
    return {"openai": LLMHelper.extract_divisions_openai,
            "anthropic": LLMHelper.extract_divisions_anthropic,
            "gemini": LLMHelper.extract_divisions_gemini}[provider]


def collect(qid: str, label: str, website: str, providers: Sequence[str], samples: int,
            log=None) -> Tuple[List[dict], List[dict]]:
    """Ask each provider for each sample. Returns (proposals, run rows). Each named
    school becomes a candidates row (status unaligned) when a run log is active."""
    from wikidata_discover import llm_helpers
    proposals, runs = [], []
    for p in providers:
        for s in range(1, samples + 1):
            key = llm_helpers.extract_cache_key(p, label, website, s)
            units = extractor(p)(label, website, sample=s) or []
            call_id = llm_helpers.last_extraction_call_id
            names = 0
            for idx, u in enumerate(units):
                name = (u.get("name") or u.get("unit") or "").strip()
                if not name:
                    continue
                cand = None
                if log is not None:
                    cand = log.add_candidate(parent_qid=qid, name=name, unit_type=u.get("unit_type"),
                                             status="unaligned", source_url=u.get("reference"),
                                             website=u.get("website"), provider=p,
                                             llm_call_ids=[call_id] if call_id else [])
                proposals.append({"university_qid": qid, "provider": p, "sample": s, "index": idx,
                                  "name": name, "unit_type": u.get("unit_type") or "",
                                  "source_url": u.get("reference") or "", "website": u.get("website") or "",
                                  "candidate_id": cand, "llm_call_id": call_id, "cache_key": key})
                names += 1
            cache_file = llm_helpers._CACHE_DIR / f"{key}.json"
            runs.append({"university_qid": qid, "provider": p, "sample": s, "cache_key": key,
                         "cache_file": str(cache_file), "cache_file_exists": cache_file.exists(),
                         "schools": names, "answered": names > 0})
    return proposals, runs


def write_checks(log, groups: List[dict], proposals: List[dict]) -> None:
    """One agreement check per group. It passes when 2 or more providers named the
    school. It names the group's first candidates row; detail lists all of them."""
    for g in groups:
        members = [proposals[i] for i in g["member_index"]]
        cands = [m["candidate_id"] for m in members if m["candidate_id"]]
        if not cands:
            continue
        calls = sorted({m["llm_call_id"] for m in members if m["llm_call_id"]})
        log.add_check(kind="agreement", result="pass" if g["n_providers"] >= 2 else "fail",
                      candidate_id=cands[0], score=g["score"], llm_call_ids=calls,
                      detail={"university_qid": g["university_qid"], "group_id": g["group_id"],
                              "name": g["name"], "variants": g["variants"], "providers": g["providers"],
                              "runs": g["runs"], "n_providers": g["n_providers"], "n_runs": g["n_runs"],
                              "candidate_ids": cands})


# ─────────────────────────  FILES  ─────────────────────────

def write_csv(path: Path, rows: List[dict], columns: Optional[List[str]] = None) -> None:
    columns = columns or (list(rows[0].keys()) if rows else [])
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("; ".join(map(str, v)) if isinstance(v, list) else v) for k, v in r.items()})


GROUP_COLUMNS = ["university_qid", "university_set", "group_id", "name", "variants", "providers",
                 "n_providers", "runs", "n_runs", "score", "status", "truth_name", "wrong_kind"]
PROPOSAL_COLUMNS = ["university_qid", "provider", "sample", "index", "name", "unit_type", "source_url",
                    "website", "group_id"]
COMPARED_FILES = ("proposals.csv", "groups.csv", "precision_by_agreement.csv", "cross_provider.csv",
                  "wrong_by_provider.csv", "s1_estimates.csv", "single_provider_review.csv")


def analyze_all(universities: List[dict], proposals_by_qid: Dict[str, List[dict]],
                runs_by_qid: Dict[str, List[dict]], providers: Sequence[str], samples: int,
                review_size: int = REVIEW_SIZE, review_seed: int = REVIEW_SEED) -> Dict[str, List[dict]]:
    """Every table of the report from the collected answers. Pure: no calls, no files."""
    groups_all, proposals_all, s1 = [], [], []
    for u in universities:
        qid = u["qid"]
        props = proposals_by_qid.get(qid, [])
        for p in props:
            p.setdefault("university_qid", qid)
        answered = [(r["provider"], r["sample"]) for r in runs_by_qid.get(qid, []) if r["answered"]]
        groups = analyze_university(qid, props, u["schools"], answered, u["name"])
        for g in groups:
            g["university_set"] = u["set"]
            for i in g["member_index"]:
                props[i]["group_id"] = g["group_id"]
        groups_all.extend(groups)
        proposals_all.extend(props)
        s1.extend(s1_estimates(qid, u["name"], u["set"], len(u["schools"]), groups, props, providers, answered))
    return {"groups": groups_all, "proposals": proposals_all,
            "precision_by_agreement": precision_by_agreement(groups_all, len(providers), samples),
            "cross_provider": cross_provider(groups_all, providers),
            "wrong_by_provider": wrong_by_provider(groups_all, providers),
            "s1_estimates": s1,
            "single_provider_review": review_sample(groups_all, review_size, review_seed)}


def write_report(out_dir: Path, tables: Dict[str, List[dict]], runs: List[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    proposals = sorted(tables["proposals"], key=lambda p: (p["university_qid"], provider_order(p["provider"]),
                                                            p["sample"], p["index"]))
    write_csv(out_dir / "proposals.csv", proposals, PROPOSAL_COLUMNS)
    write_csv(out_dir / "groups.csv", tables["groups"], GROUP_COLUMNS)
    write_csv(out_dir / "precision_by_agreement.csv", tables["precision_by_agreement"])
    write_csv(out_dir / "cross_provider.csv", tables["cross_provider"])
    write_csv(out_dir / "wrong_by_provider.csv", tables["wrong_by_provider"])
    write_csv(out_dir / "s1_estimates.csv", tables["s1_estimates"])
    review = [{**{k: g[k] for k in ("university_qid", "group_id", "name", "variants", "providers", "runs")},
               "real": "", "notes": ""} for g in tables["single_provider_review"]]
    write_csv(out_dir / "single_provider_review.csv", review,
              ["university_qid", "group_id", "name", "variants", "providers", "runs", "real", "notes"])
    write_csv(out_dir / "cache_files.csv", runs,
              ["university_qid", "provider", "sample", "cache_key", "cache_file", "cache_file_exists", "schools"])
    write_csv(out_dir / "ids.csv", proposals,
              ["university_qid", "provider", "sample", "index", "name", "candidate_id", "llm_call_id"])


def compare_dirs(a: Path, b: Path) -> List[str]:
    """Names of the report files that differ between two output folders (ids.csv and
    cache_files.csv are left out: ids are new on every run)."""
    return [f for f in COMPARED_FILES if (a / f).read_bytes() != (b / f).read_bytes()]


# ─────────────────────────  COMMAND  ─────────────────────────

def universities_to_run(qids: Optional[List[str]]) -> List[dict]:
    from wikidata_discover.eval import ground_truth
    sets = [("eval", ground_truth.GROUND_TRUTH), ("small", getattr(ground_truth, "SMALL_GROUND_TRUTH", {}))]
    known = {q: (s, v) for s, d in sets for q, v in d.items()}
    unknown = [q for q in (qids or []) if q not in known]
    if unknown:
        raise SystemExit(f"Unknown QIDs (not in the ground truth): {unknown}")
    chosen = qids or list(known)
    return [{"qid": q, "set": known[q][0], "name": known[q][1]["name"], "website": known[q][1].get("website") or "",
             "schools": list(known[q][1]["schools"])} for q in chosen]


def run(args) -> Path:
    from wikidata_discover import config, run_log
    from wikidata_discover.batch import operator_identity, source_identity, RESULTS_DIR
    from wikidata_discover.cli import run_config
    console = config.console

    key_sources = config.load_missing_keys() or {}
    have = {"openai": config.OPENAI_API_KEY, "anthropic": config.ANTHROPIC_API_KEY, "gemini": config.GOOGLE_API_KEY}
    providers = [p for p in (args.providers or PROVIDERS) if have[p]]
    if not providers:
        raise SystemExit("No provider has an API key.")
    universities = universities_to_run(args.universities)

    run_id = run_log.new_run_id("agreement")
    out_dir = Path(args.out) if args.out else RESULTS_DIR / "agreement" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    log = None
    if not args.no_run_log:
        identity = source_identity(out_dir)
        run_log.start(run_id, command="agreement", args={"argv": sys.argv[1:]},
                      qids=[u["qid"] for u in universities], who=operator_identity(), git_commit=identity,
                      run_config={**run_config(key_sources if isinstance(key_sources, dict) else {}),
                                  "samples": args.samples, "agreement_providers": providers})
        log = run_log.current()
    console.print(f"[bold]Agreement run {run_id}: {len(universities)} universities, "
                  f"providers {providers}, {args.samples} samples each[/bold]")
    proposals_by_qid, runs_by_qid, all_runs, outcome = {}, {}, [], "failed"
    try:
        for u in universities:
            if log is not None:
                log.set_university(u["qid"])
            props, runs = collect(u["qid"], u["name"], u["website"], providers, args.samples, log)
            proposals_by_qid[u["qid"]], runs_by_qid[u["qid"]] = props, runs
            all_runs.extend(runs)
            answered = [(r["provider"], r["sample"]) for r in runs if r["answered"]]
            if log is not None:
                groups = analyze_university(u["qid"], props, u["schools"], answered, u["name"])
                for g in groups:
                    g["university_set"] = u["set"]
                write_checks(log, groups, props)
                log.flush()
            failed = [run_label(r["provider"], r["sample"]) for r in runs if not r["answered"]]
            console.print(f"  {u['qid']} {u['name']}: {len(props)} schools named"
                          + (f"; no answer from {failed}" if failed else ""))
        tables = analyze_all(universities, proposals_by_qid, runs_by_qid, providers, args.samples,
                             args.review_size, args.review_seed)
        write_report(out_dir, tables, all_runs)
        outcome = "ok"
    finally:
        if log is not None:
            status = log.finish(outcome, {"qids_done": list(proposals_by_qid), "out_dir": str(out_dir)})
            console.print(f"[dim]{run_log.describe(status)}[/dim]")
    print_summary(tables, out_dir)
    return out_dir


def print_summary(tables: Dict[str, List[dict]], out_dir: Path) -> None:
    from wikidata_discover import config
    console = config.console
    console.print("\n[bold]Precision by number of providers[/bold]")
    for r in tables["precision_by_agreement"]:
        if r["university_set"] == "all" and r["level"] == "providers_exactly":
            console.print(f"  {r['k']} provider(s): {r['correct']}/{r['schools']} correct, precision {r['precision']}")
    console.print("\n[bold]S1, all providers together (M_t)[/bold]")
    for r in tables["s1_estimates"]:
        if r["method"] == "mt_providers":
            console.print(f"  {r['university']:<45} {r['mode']:<8} seen {r['observed']:>3}  "
                          f"estimate {r['estimate']!s:>7}  true {r['true_count']}")
    console.print(f"\nReport: {out_dir}")


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Parallel extraction: do independent LLMs agree? (Shuo, week 3)")
    parser.add_argument("--providers", nargs="+", choices=PROVIDERS)
    parser.add_argument("--universities", nargs="+", help="QIDs (default: every university with ground truth)")
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES, help="runs per provider (default 3)")
    parser.add_argument("--out", help="output folder (default results/agreement/<run_id>)")
    parser.add_argument("--no-run-log", action="store_true",
                        help="write no run log rows (no candidates or checks): for a report from cached answers only")
    parser.add_argument("--review-size", type=int, default=REVIEW_SIZE)
    parser.add_argument("--review-seed", type=int, default=REVIEW_SEED)
    parser.add_argument("--compare", nargs=2, metavar=("DIR_A", "DIR_B"), help="check that two runs gave the same report")
    parser.add_argument("--draw-small", action="store_true", help="draw the small universities and stop")
    parser.add_argument("--seed", type=int, help="seed for --draw-small")
    parser.add_argument("--k", type=int, default=DRAW_ORDER_SIZE, help="how long the order --draw-small prints is")
    args = parser.parse_args(argv)

    if args.samples < 1:
        parser.error("--samples must be 1 or more")
    if args.compare:
        differ = compare_dirs(Path(args.compare[0]), Path(args.compare[1]))
        print("Identical: every report file is the same." if not differ else f"Different: {differ}")
        sys.exit(1 if differ else 0)
    if args.draw_small:
        if args.seed is None:
            parser.error("--draw-small needs --seed")
        table = sitelinks_table()
        bottom = sorted(table, key=lambda u: (u[2], int(u[0][1:])))[: len(table) // 3]
        print(f"{len(table)} universities; bottom third: {len(bottom)} (at most {bottom[-1][2]} sitelinks); "
              f"seed {args.seed}; sitelinks from {SITELINKS_SNAPSHOT.name}")
        order = draw_small(table, args.seed, args.k)
        kept = keep_with_units(order, SKIPPED_NO_UNITS)
        for i, (q, n, s) in enumerate(order, 1):
            mark = "kept" if q in kept else ("skipped: " + SKIPPED_NO_UNITS[q] if q in SKIPPED_NO_UNITS else "")
            print(f"{i:>2}  {q}\t{n}\t{s} sitelinks\t{mark}")
        if args.seed == DRAW_SEED and kept != SMALL_UNIVERSITIES:
            print("Warning: this draw does not give SMALL_UNIVERSITIES.")
        return
    run(args)


if __name__ == "__main__":
    main()
