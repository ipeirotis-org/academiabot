"""
The LLM verifier (Shuo, week 6; see TASKS.md and SPECS.md).

Given a claim ("Unit is part of Parent") and the saved page it cites, the verifier answers
supported, unsupported, or unclear, with a quote of the text that decides it.

1. A page that could not be read is decided without a model. A 404 or 410 page is an error
   page, so the claim is unsupported (as in the annotator instructions). A page that refused
   the fetcher (401, 403, 405, 429), a server error, or an empty text is unclear.
2. Any other page is split into fixed, overlapping pieces of PIECE_CHARS characters, and the
   model reads each piece on its own.
3. A quote must be in the saved page word for word (spaces, curly quotes and dashes are
   evened out first). An answer whose quote is not on the page becomes unclear.
4. The claim is supported when any piece supports it with a good quote. Otherwise it is
   unclear when any piece is unclear (or got no answer), and unsupported when every piece
   was read and none supports it.

The cheap version is MiniCheck (`minicheck.py`, Flan-T5-Large). It answers only yes or no,
with a probability, and gives no quote, so it is scored as a yes-or-no checker.

Usage:
    python -m wikidata_discover.eval.verifier --pairs DIR/pairs_100.csv --claims DIR/claims.csv
    python -m wikidata_discover.eval.verifier ... --provider openai --minicheck
    python -m wikidata_discover.eval.verifier --long-page-check       # check 3: one very long page
    python -m wikidata_discover.eval.verifier --rescore OUT --claims DIR/claims.csv   # tables again, no calls

DIR is a week 5 page-check report folder: the `text_file` column of pairs_100.csv is read
relative to it.

Output (wikidata_discover/results/verifier/<run_id>/):
    verdicts.csv        one row per pair: your label, the verifier's answer and quote, MiniCheck
    accuracy.csv        accuracy per provider and overall (balanced and weighted), 3-way and 2-way
    confusion.csv       the confusion matrix per provider (label against answer)
    spot_check_10.csv   10 random supported quotes for you to check against the page
"""

import argparse
import csv
import json
import random
import re
import sys
import unicodedata
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from wikidata_discover import config
from wikidata_discover.eval.agreement import PROVIDERS, write_csv

console = config.console

PIECE_CHARS = 12000      # one piece of page text, in characters (about 3,000 tokens)
PIECE_OVERLAP = 1500     # characters shared by two neighbouring pieces, so no sentence is cut off
VERDICTS = ("supported", "unsupported", "unclear")
DEAD_STATUSES = {404, 410}
SPOT_SEED = 20261127
RESULTS_DIR = config.RESULTS_DIR / "verifier"
LABEL_ALIASES = {"not supported": "unsupported", "not_supported": "unsupported"}


# ─────────────────────────  PIECES AND QUOTES  ─────────────────────────

def split_pieces(text: str, size: int = PIECE_CHARS, overlap: int = PIECE_OVERLAP) -> List[str]:
    """Fixed, overlapping pieces of `text`: piece i starts at i * (size - overlap) and is at
    most `size` characters long. The last piece ends at the end of the text. A text of
    `size` characters or fewer is one piece. Pure."""
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError("size must be positive and overlap between 0 and size - 1")
    if len(text) <= size:
        return [text]
    step = size - overlap
    pieces, start = [], 0
    while True:
        pieces.append(text[start:start + size])
        if start + size >= len(text):
            return pieces
        start += step


_TYPOGRAPHY = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
                             "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u00a0": " ",
                             "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
                             "\u2026": "..."})


def even_out(text: str) -> str:
    """The text with spaces, curly quotes, dashes and Unicode forms evened out. Case and
    words are kept: only the look of the characters changes. Pure."""
    t = unicodedata.normalize("NFKC", text or "").translate(_TYPOGRAPHY)
    return re.sub(r"\s+", " ", t).strip()


def quote_parts(quote: str) -> List[str]:
    """The parts of a quote: a quote may join two places with ' ... ' and may start with
    '[title]', as in the annotator instructions. Pure."""
    q = even_out(quote)
    q = re.sub(r"^\[(title|logo)\]\s*", "", q, flags=re.I)
    return [p.strip() for p in re.split(r"\s*\.\.\.\s*", q) if p.strip()]


def quote_on_page(quote: str, page_text: str) -> bool:
    """True when every part of the quote is in the saved page word for word. An empty quote
    is never on the page. Pure."""
    parts = quote_parts(quote)
    if not parts:
        return False
    page = even_out(page_text)
    return all(p in page for p in parts)


# ─────────────────────────  ONE CLAIM  ─────────────────────────

def unreadable_verdict(fetch_outcome: str, http_status, text: str) -> Optional[Tuple[str, str]]:
    """(verdict, reason) for a page that no model needs to read, else None. A 404 or 410
    answer is an error page: unsupported. A page that was not fetched, refused the fetcher,
    or has no text is unclear. Pure."""
    try:
        status = int(http_status) if str(http_status or "").strip() else None
    except ValueError:
        status = None
    if status in DEAD_STATUSES:
        return "unsupported", f"page not found (HTTP {status})"
    if fetch_outcome != "ok":
        return "unclear", f"could not read the page ({fetch_outcome or 'not fetched'}{', HTTP ' + str(status) if status else ''})"
    if not (text or "").strip():
        return "unclear", "could not read the page (no text)"
    return None


def checked_answer(answer: Optional[dict], page_text: str) -> dict:
    """One piece's answer after the quote check. No answer is unclear. A quote that is not
    in the saved page word for word makes the answer unclear, and a supported answer must
    have a quote. Pure."""
    if answer is None:
        return {"verdict": "unclear", "quote": "", "reason": "no answer from the model", "quote_ok": ""}
    verdict, quote, reason = answer["verdict"], answer.get("quote") or "", answer.get("reason") or ""
    if not quote.strip():
        if verdict == "supported":
            return {"verdict": "unclear", "quote": "", "reason": "supported, but no quote: " + reason, "quote_ok": "no"}
        return {"verdict": verdict, "quote": "", "reason": reason, "quote_ok": ""}
    if not quote_on_page(quote, page_text):
        return {"verdict": "unclear", "quote": quote, "reason": f"{verdict}, but the quote is not on the page word for word: {reason}",
                "quote_ok": "no", "model_verdict": verdict}
    return {"verdict": verdict, "quote": quote, "reason": reason, "quote_ok": "yes"}


def combine(answers: Sequence[dict]) -> Tuple[dict, int]:
    """The claim's answer from its checked piece answers, and the 1-based piece it came
    from: the first supported piece; else the first unclear piece; else unsupported (every
    piece was read and none supports it), with the first quote any piece gave. Pure."""
    for wanted in ("supported", "unclear"):
        for i, a in enumerate(answers, 1):
            if a["verdict"] == wanted:
                return a, i
    for i, a in enumerate(answers, 1):
        if a["quote"]:
            return a, i
    return (answers[0] if answers else {"verdict": "unclear", "quote": "", "reason": "no pieces", "quote_ok": ""}), 1


Ask = Callable[..., Tuple[Optional[dict], Optional[str]]]


def verify_claim(unit: str, parent: str, page_text: str, fetch_outcome: str = "ok", http_status=200,
                 provider: str = "openai", ask: Optional[Ask] = None, size: int = PIECE_CHARS,
                 overlap: int = PIECE_OVERLAP) -> dict:
    """The verifier's answer for one claim: verdict, quote, reason, quote_ok, decided_by
    (page_status or the provider), pieces, piece (which piece decided it), llm_call_ids.
    `ask` is LLMHelper.verify_piece unless a test passes another one."""
    pre = unreadable_verdict(fetch_outcome, http_status, page_text)
    if pre is not None:
        return {"verdict": pre[0], "quote": "", "reason": pre[1], "quote_ok": "", "decided_by": "page_status",
                "pieces": 0, "piece": 0, "llm_call_ids": []}
    if ask is None:
        from wikidata_discover.llm_helpers import LLMHelper
        ask = LLMHelper.verify_piece
    pieces = split_pieces(page_text, size, overlap)
    answers, call_ids = [], []
    for i, piece in enumerate(pieces, 1):
        answer, call_id = ask(provider, unit, parent, piece, i, len(pieces))
        if call_id:
            call_ids.append(call_id)
        answers.append(checked_answer(answer, page_text))
        if answers[-1]["verdict"] == "supported":
            break   # one supporting piece decides the claim; the rest need not be read
    best, at = combine(answers)
    return {**best, "decided_by": provider, "pieces": len(pieces), "piece": at, "llm_call_ids": call_ids}


# ─────────────────────────  SCORING  ─────────────────────────

def two_way(verdict: str) -> str:
    """supported, or not (unsupported and unclear together). Pure."""
    return "supported" if verdict == "supported" else "not supported"


def confusion(rows: Sequence[dict], label_key: str, answer_key: str, classes: Sequence[str]) -> Dict[Tuple[str, str], int]:
    """Counts of (label, answer) over rows, for every pair of classes. Pure."""
    m = {(a, b): 0 for a in classes for b in classes}
    for r in rows:
        m[(r[label_key], r[answer_key])] += 1
    return m


def accuracy(rows: Sequence[dict], label_key: str, answer_key: str) -> Tuple[int, int, Optional[float]]:
    """(right, total, share right). Pure."""
    n = len(rows)
    k = sum(1 for r in rows if r[label_key] == r[answer_key])
    return k, n, (round(k / n, 4) if n else None)


def weighted(per_provider: Dict[str, Optional[float]], weights: Dict[str, float]) -> Optional[float]:
    """Sum of each provider's accuracy times its weight, over providers with an accuracy,
    with the weights scaled to add up to 1. Pure."""
    use = {p: w for p, w in weights.items() if per_provider.get(p) is not None and w > 0}
    total = sum(use.values())
    if not total:
        return None
    return round(sum(per_provider[p] * w / total for p, w in use.items()), 4)


def page_weights(claims: Sequence[dict]) -> Dict[str, float]:
    """Each provider's share of all saved pages: distinct (provider, page) among the
    week3, schools_on and depts_on claims (the sets the 100 pairs were drawn from). A page
    is its evidence id. A week 5 report made with --no-run-log has no evidence ids at all;
    there a page is its cited URL. A page that two providers cite counts once for each. Pure."""
    rows = [c for c in claims if c.get("setting") in ("week3", "schools_on", "depts_on")]
    key = "evidence_id" if any(c.get("evidence_id") for c in rows) else "url"
    seen = {(c["provider"], c[key]) for c in rows if c.get(key)}
    counts = {p: sum(1 for q, _ in seen if q == p) for p in PROVIDERS}
    total = sum(counts.values())
    return {p: (counts[p] / total if total else 0.0) for p in PROVIDERS}


def score_tables(rows: Sequence[dict], weights: Dict[str, float], methods: Sequence[str]) -> Tuple[List[dict], List[dict]]:
    """accuracy.csv and confusion.csv rows. For each method (an answer column of `rows`)
    the 3-way accuracy (LLM verifier only) and the 2-way accuracy (supported against the
    rest), per provider and overall, balanced (as sampled) and weighted by page share.
    MiniCheck has only the 2-way score. Each table is given twice: over all pairs, and over
    the pairs whose page the model read (not decided by the page status alone). Pure."""
    acc_rows, conf_rows = [], []
    read = [r for r in rows if r.get("decided_by") and r.get("decided_by") != "page_status"]
    for scope, scoped in (("all pairs", rows), ("pages the model read", read)):
        a, c = _score_scope(scoped, weights, methods)
        acc_rows += [{"scope": scope, **x} for x in a]
        conf_rows += [{"scope": scope, **x} for x in c]
    return acc_rows, conf_rows


def _score_scope(rows: Sequence[dict], weights: Dict[str, float], methods: Sequence[str]) -> Tuple[List[dict], List[dict]]:
    acc_rows, conf_rows = [], []
    for method in methods:
        ways = [("2-way", "label2", method + "_2")]
        if method != "minicheck":
            ways.insert(0, ("3-way", "label", method))
        for way, lkey, akey in ways:
            classes = VERDICTS if way == "3-way" else ("supported", "not supported")
            usable = [r for r in rows if r.get(akey)]
            per = {}
            for prov in PROVIDERS:
                mine = [r for r in usable if r["provider"] == prov]
                k, n, a = accuracy(mine, lkey, akey)
                per[prov] = a
                acc_rows.append({"method": method, "way": way, "provider": prov, "pairs": n, "right": k,
                                 "accuracy": a, "weight": round(weights.get(prov, 0), 4)})
                for (lab, ans), c in confusion(mine, lkey, akey, classes).items():
                    conf_rows.append({"method": method, "way": way, "provider": prov, "label": lab, "answer": ans, "pairs": c})
            k, n, a = accuracy(usable, lkey, akey)
            acc_rows.append({"method": method, "way": way, "provider": "all (balanced, as sampled)", "pairs": n,
                             "right": k, "accuracy": a, "weight": ""})
            acc_rows.append({"method": method, "way": way, "provider": "all (weighted by page share)", "pairs": n,
                             "right": "", "accuracy": weighted(per, weights), "weight": ""})
            for (lab, ans), c in confusion(usable, lkey, akey, classes).items():
                conf_rows.append({"method": method, "way": way, "provider": "all", "label": lab, "answer": ans, "pairs": c})
    return acc_rows, conf_rows


def spot_check(rows: Sequence[dict], method: str, n: int = 10, seed: int = SPOT_SEED,
               earlier: Sequence[dict] = ()) -> List[dict]:
    """`n` random pairs whose answer by `method` is supported with a quote, for a person to
    check the quote against the saved page. Same seed, same draw. `earlier` is the sheet
    already on disk: a row with the same pair and quote keeps its quote_on_page and notes,
    so --rescore never wipes a person's answers (review of pull request 29). Pure."""
    kept = {(e.get("pair_id"), e.get("quote")): e for e in earlier}
    pool = sorted((r for r in rows if r.get(method) == "supported" and r.get(method + "_quote")),
                  key=lambda r: int(r["pair_id"]))
    picked = random.Random(seed).sample(pool, min(n, len(pool)))
    return [{"pair_id": r["pair_id"], "provider": r["provider"], "claim": r["claim"], "text_file": r["text_file"],
             "quote": r[method + "_quote"],
             "quote_on_page": kept.get((str(r["pair_id"]), r[method + "_quote"]), {}).get("quote_on_page", ""),
             "notes": kept.get((str(r["pair_id"]), r[method + "_quote"]), {}).get("notes", "")}
            for r in sorted(picked, key=lambda r: int(r["pair_id"]))]


# ─────────────────────────  INPUT  ─────────────────────────

def split_claim(claim: str) -> Tuple[str, str]:
    """("Unit", "Parent") from "Unit is part of Parent". Pure."""
    unit, sep, parent = claim.rpartition(" is part of ")
    if not sep:
        raise ValueError(f"claim is not 'X is part of Y': {claim!r}")
    return unit.strip(), parent.strip()


def normal_label(label: str) -> str:
    lab = (label or "").strip().lower()
    lab = LABEL_ALIASES.get(lab, lab)
    if lab not in VERDICTS:
        raise ValueError(f"label must be one of {VERDICTS}, not {label!r}")
    return lab


def load_pairs(pairs_csv: Path) -> List[dict]:
    """The labelled pairs, with the page text read from `text_file` (relative to the CSV's
    folder). An empty or missing text file gives empty text."""
    base = pairs_csv.parent
    rows = list(csv.DictReader(pairs_csv.open(newline="", encoding="utf-8")))
    for r in rows:
        r["label"] = normal_label(r["label"])
        r["label2"] = two_way(r["label"])
        path = base / r["text_file"] if r.get("text_file") else None
        r["_text"] = path.read_text(encoding="utf-8") if path and path.is_file() else ""
    return rows


def candidate_ids(pairs: Sequence[dict], claims: Sequence[dict]) -> Dict[str, str]:
    """pair_id -> candidate_id. The week 5 draw kept the first claim of each (provider, unit,
    URL) in claims.csv order, so the same rule finds it again. Pure."""
    from wikidata_discover.eval.page_checks import claim_text
    first: Dict[Tuple[str, str, str], str] = {}
    for c in claims:
        if c.get("setting") in ("week3", "schools_on", "depts_on") and c.get("url"):
            first.setdefault((c["provider"], claim_text(c), c["url"]), c["candidate_id"])
    return {p["pair_id"]: first.get((p["provider"], p["claim"], p["url"]), "") for p in pairs}


# ─────────────────────────  RUN  ─────────────────────────

def run_pairs(pairs: List[dict], provider: str, ask: Optional[Ask] = None,
              minicheck_score: Optional[Callable[[str, str], float]] = None) -> List[dict]:
    """Add the verifier's answer (columns `llm`, `llm_2`, `llm_quote` ...) and, with
    `minicheck_score`, MiniCheck's (`minicheck_prob`, `minicheck_2`) to every pair."""
    for i, r in enumerate(pairs, 1):
        unit, parent = split_claim(r["claim"])
        v = verify_claim(unit, parent, r["_text"], r.get("fetch_outcome", "ok"), r.get("http_status"),
                         provider=provider, ask=ask)
        r.update({"llm": v["verdict"], "llm_2": two_way(v["verdict"]), "llm_quote": v["quote"],
                  "llm_quote_ok": v["quote_ok"], "llm_reason": v["reason"], "decided_by": v["decided_by"],
                  "pieces": v["pieces"], "piece": v["piece"], "llm_call_ids": v["llm_call_ids"],
                  "model_verdict": v.get("model_verdict", "")})
        if minicheck_score is not None:
            pre = unreadable_verdict(r.get("fetch_outcome", "ok"), r.get("http_status"), r["_text"])
            if pre is None:
                prob = minicheck_score(r["_text"], f"{unit} is part of {parent}.")
                r["minicheck_prob"] = round(prob, 4)
                r["minicheck_2"] = "supported" if prob >= 0.5 else "not supported"
            else:
                r["minicheck_prob"] = ""
                r["minicheck_2"] = two_way(pre[0])
        if i % 10 == 0:
            console.print(f"  {i} of {len(pairs)} pairs")
    return pairs


VERDICT_COLUMNS = ["pair_id", "provider", "setting", "search", "claim", "url", "fetch_outcome", "http_status",
                   "text_file", "label", "llm", "llm_quote", "llm_quote_ok", "llm_reason", "model_verdict",
                   "decided_by", "pieces", "piece", "label2", "llm_2", "minicheck_prob", "minicheck_2",
                   "candidate_id", "evidence_id"]


def record_checks(log, pairs: Sequence[dict], provider: str, model: str) -> int:
    """One `checks` row (kind verifier) per pair and method. result: pass (supported),
    fail (unsupported, or MiniCheck below 0.5), unclear."""
    to_result = {"supported": "pass", "unsupported": "fail", "unclear": "unclear", "not supported": "fail"}
    n = 0
    for r in pairs:
        if not r.get("candidate_id"):
            continue
        ev = [r["evidence_id"]] if r.get("evidence_id") else []
        log.add_check(kind="verifier", result=to_result[r["llm"]], candidate_id=r["candidate_id"],
                      detail=json.dumps({"method": "llm", "provider": provider, "model": model,
                                         "quote": r["llm_quote"], "quote_ok": r["llm_quote_ok"],
                                         "reason": r["llm_reason"], "decided_by": r["decided_by"],
                                         "pieces": r["pieces"], "piece": r["piece"], "url": r["url"]}, sort_keys=True),
                      llm_call_ids=r["llm_call_ids"], evidence_ids=ev)
        n += 1
        if r.get("minicheck_2"):
            log.add_check(kind="verifier", result=to_result[r["minicheck_2"]], candidate_id=r["candidate_id"],
                          score=r["minicheck_prob"] if r["minicheck_prob"] != "" else None,
                          detail=json.dumps({"method": "minicheck", "model": "lytang/MiniCheck-Flan-T5-Large",
                                             "threshold": 0.5, "url": r["url"],
                                             "decided_by": "page_status" if r["minicheck_prob"] == "" else "minicheck"},
                                            sort_keys=True),
                          evidence_ids=ev)
            n += 1
    return n


def long_test_page(sentence: str, filler_source: str, length: int) -> str:
    """A page of about `length` characters: `filler_source` repeated, with `sentence` placed
    near the end (in the last 5% of the page). Pure."""
    filler = (filler_source.strip() + " ") * (length // max(1, len(filler_source)) + 2)
    cut = int(length * 0.96)
    return filler[:cut] + " " + sentence + " " + filler[: length - cut]


def run(args) -> Path:
    from wikidata_discover import run_log
    from wikidata_discover.batch import operator_identity, source_identity
    config.load_missing_keys()
    model = {"openai": config.LLM_MODEL, "anthropic": config.ANTHROPIC_MODEL, "gemini": config.GEMINI_MODEL}[args.provider]
    run_id = run_log.new_run_id("verifier")
    out_dir = Path(args.out) if args.out else RESULTS_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    log = None
    if not args.no_run_log:
        identity = source_identity(out_dir)
        run_log.start(run_id, command="verifier", args={"argv": sys.argv[1:]}, qids=[],
                      who=operator_identity(), git_commit=identity,
                      run_config={"provider": args.provider, "model": model, "piece_chars": PIECE_CHARS,
                                  "piece_overlap": PIECE_OVERLAP, "minicheck": bool(args.minicheck),
                                  "long_page_check": bool(args.long_page_check)})
        log = run_log.current()
        if log is not None:
            log.attach_source_patch(out_dir, identity)
    outcome = "failed"
    try:
        if args.long_page_check:
            result = long_page_check(args.provider, out_dir, args.long_page_chars)
            console.print(result)
            outcome = "ok"
            return out_dir
        if args.rescore:
            pairs = list(csv.DictReader((Path(args.rescore) / "verdicts.csv").open(newline="", encoding="utf-8")))
            out_dir = Path(args.rescore)
        else:
            pairs = load_pairs(Path(args.pairs))
        claims = list(csv.DictReader(Path(args.claims).open(newline="", encoding="utf-8"))) if args.claims else []
        if not args.rescore:
            cids = candidate_ids(pairs, claims)
            for p in pairs:
                p["candidate_id"] = cids.get(p["pair_id"], "")
            scorer = None
            if args.minicheck:
                from wikidata_discover.eval.minicheck import MiniCheck
                scorer = MiniCheck().score
            run_pairs(pairs, args.provider, minicheck_score=scorer)
        weights = page_weights(claims) if claims else {p: 1 / len(PROVIDERS) for p in PROVIDERS}
        has_mc = args.minicheck or any(p.get("minicheck_2") for p in pairs)
        methods = ["llm"] + (["minicheck"] if has_mc else [])
        acc, conf = score_tables(pairs, weights, methods)
        rows = [{k: (json.dumps(p[k]) if isinstance(p.get(k), list) else p.get(k, "")) for k in VERDICT_COLUMNS} for p in pairs]
        write_csv(out_dir / "verdicts.csv", rows, VERDICT_COLUMNS)
        write_csv(out_dir / "accuracy.csv", acc, ["scope", "method", "way", "provider", "pairs", "right", "accuracy", "weight"])
        write_csv(out_dir / "confusion.csv", conf, ["scope", "method", "way", "provider", "label", "answer", "pairs"])
        sheet = out_dir / "spot_check_10.csv"
        earlier = list(csv.DictReader(sheet.open(newline="", encoding="utf-8"))) if sheet.exists() else []
        write_csv(sheet, spot_check(pairs, "llm", earlier=earlier),
                  ["pair_id", "provider", "claim", "text_file", "quote", "quote_on_page", "notes"])
        if log is not None and not args.rescore:
            console.print(f"{record_checks(log, pairs, args.provider, model)} checks rows")
        for a in acc:
            console.print(f"{a['scope']:22} {a['method']:9} {a['way']} {a['provider']:30} {a['right']}/{a['pairs']}  {a['accuracy']}")
        outcome = "ok"
    finally:
        if log is not None:
            console.print(f"[dim]{run_log.describe(log.finish(outcome, {'out_dir': str(out_dir)}))}[/dim]")
    console.print(f"Report: {out_dir}")
    return out_dir


LONG_PAGE_SENTENCE = ("The School of Applied Ceramics is one of the twelve schools of Northfield State University "
                      "and offers degrees in glaze chemistry.")


def long_page_check(provider: str, out_dir: Path, length: int) -> dict:
    """Check 3: one test page longer than the model can read at once (`length` characters;
    the default is more than gpt-4o's 128,000-token window), with the supporting sentence
    near the end. The answer must be supported."""
    filler = ("Northfield State University news. The library extends its hours during the exam period. "
              "Parking permits for the spring term are on sale at the campus office. The dining hall menu "
              "changes every week, and the gym is open from six in the morning. ")
    page = long_test_page(LONG_PAGE_SENTENCE, filler, length)
    (out_dir / "long_test_page.txt").write_text(page, encoding="utf-8")
    v = verify_claim("School of Applied Ceramics", "Northfield State University", page, provider=provider)
    result = {"page_chars": len(page), "sentence_at_char": page.index(LONG_PAGE_SENTENCE),
              "pieces": v["pieces"], "piece": v["piece"], "verdict": v["verdict"], "quote": v["quote"],
              "quote_ok": v["quote_ok"], "passed": v["verdict"] == "supported"}
    (out_dir / "long_page_check.json").write_text(json.dumps(result, indent=2))
    return result


def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pairs", help="the labelled pairs_100.csv of a week 5 report folder")
    p.add_argument("--claims", help="claims.csv of the same folder (candidate ids and page weights)")
    p.add_argument("--provider", choices=PROVIDERS, default="openai")
    p.add_argument("--minicheck", action="store_true", help="also run MiniCheck (needs torch and transformers)")
    p.add_argument("--long-page-check", action="store_true", help="check 3: one page longer than the model can read")
    p.add_argument("--long-page-chars", type=int, default=600_000)
    p.add_argument("--rescore", help="an earlier output folder: score its verdicts.csv again, no calls")
    p.add_argument("--out")
    p.add_argument("--no-run-log", action="store_true")
    args = p.parse_args(argv)
    if args.rescore:
        args.no_run_log = True
    if not (args.long_page_check or args.pairs or args.rescore):
        p.error("--pairs is required (or use --long-page-check or --rescore)")
    run(args)


if __name__ == "__main__":
    main()
