"""
Does the cited page exist, and does it name the unit? (Shuo, week 5; see TASKS.md and SPECS.md.)

A claim is one unit that one provider proposed in one run, with the URL it cited as
its source: "X is a school (or department) of Y, see URL". This command fetches every
cited URL once, saves it as an evidence row (Anya's week 4 fetcher), and runs four
mechanical checks on each claim. Each result is one `checks` row:

    page_status   dead link: the page does not answer, or answers 404 or 410
    soft_404      the page answers 200 but says "not found", or a deep link lands on the home page
    on_domain     the page is not on the university's own website (its P856 domain)
    name_on_page  the unit's name is on the page (small differences allowed)

Results are pass, fail, or unclear. A site that refuses the fetcher (401, 403, 405, 429)
is unclear, never dead: nyu.edu answers 403 or 405 to it on some pages.

Claim sets, each reported on its own:
    week3        the week 3 school lists (run agreement-20261109T204358Z-32c21a). Search
                 was off for Anthropic and Gemini, and on but not forced for OpenAI.
    schools_on   new school lists with search on for every provider (`subunits <university>
                 --provider P --sample N`, 2026-11-24)
    depts_on     the department lists of the six ground-truth schools (week 4, `subunits
                 --provider P --sample N`, search on for every provider)
    us_tier1     300 random school websites from run us-tier1 (almost all OpenAI), seed 20261124

Usage:
    python -m wikidata_discover.eval.page_checks                        # fetch, check, report
    python -m wikidata_discover.eval.page_checks --rescore DIR          # checks again from saved pages, no fetch
    python -m wikidata_discover.eval.page_checks --check10 FILE.csv     # your 10 hand-picked URLs

Output (wikidata_discover/results/page_checks/<run_id>/):
    claims.csv            one row per claim, with the four results
    pages.csv             one row per URL fetched (pages/<n>.txt holds the text)
    summary.csv           per claim set and provider: share of each result, with 95% intervals
    pairs_100.csv         100 claim-and-page pairs for you to label (a third per provider)
    failures_20.csv       20 failed claims for you to sort
    s1_page_pass.csv      S1, the third way: count only schools whose page check passes
"""

import argparse
import csv
import json
import math
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from wikidata_discover import config, fetcher
from wikidata_discover.eval.agreement import PROVIDERS, write_csv

console = config.console

WEEK3_RUN = "agreement-20261109T204358Z-32c21a"
DEPTS_SINCE = "2026-11-17T05:00:00Z"   # the week 4 department runs (judge README)
DEPTS_UNTIL = "2026-11-17T07:00:00Z"
SCHOOLS_SINCE = "2026-11-24T06:00:00Z"  # the week 5 search-on school runs (fresh start, 2026-11-24)
DRAW_SEED = 20261124
TIER1_SIZE = 300
PAIRS = 100
FAILURES = 20
FETCH_WORKERS = 8
SETTINGS = ("week3", "schools_on", "depts_on", "us_tier1")
KINDS = ("page_status", "soft_404", "on_domain", "name_on_page")
DEPT_SCHOOLS = Path(__file__).parent / "dept_schools.csv"
RESULTS_DIR = config.RESULTS_DIR / "page_checks"

# Statuses of a site that refuses automated readers. The page may well exist.
REFUSING_STATUSES = {401, 403, 405, 406, 429, 451, 999}
DEAD_STATUSES = {404, 410}

NOT_FOUND = re.compile(
    r"(page|file|document|content|resource|url)\s+(was\s+|is\s+|could\s+)?(not|cannot|can't|couldn't)\s+"
    r"(be\s+)?(found|located|exist)|\b404\b|not\s+found\b|no\s+longer\s+(exists|available)"
    r"|page\s+(you|you're)\s+(are\s+)?(looking|requested|trying)|does\s+not\s+exist|doesn't\s+exist"
    r"|page\s+has\s+(been\s+)?(moved|removed)|oops", re.I)


NOT_FOUND_PATH = re.compile(r"(not[-_]?found|\b404\b|error)", re.I)


# ─────────────────────────  SMALL PURE HELPERS  ─────────────────────────

def wilson(k: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    """95% Wilson score interval for k successes in n trials. (None, None) when n is 0."""
    if n <= 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return round(max(0.0, mid - half), 4), round(min(1.0, mid + half), 4)


TWO_LEVEL = {"ac.uk", "co.uk", "org.uk", "edu.au", "com.au", "ac.jp", "edu.cn", "com.cn", "ac.kr",
             "edu.sg", "edu.hk", "ac.il", "ac.nz", "edu.mx", "edu.tr", "ac.in", "edu.pk", "edu.ar",
             "com.br", "edu.br", "ac.za", "ac.ae", "edu.qa", "edu.sa", "edu.co", "edu.pe"}


def site_domain(url_or_host: str) -> str:
    """The registrable domain of a URL or host: "www.stern.nyu.edu" -> "nyu.edu",
    "www.ox.ac.uk" -> "ox.ac.uk". Lower case, "" when there is no host. Pure."""
    s = (url_or_host or "").strip()
    if not s:
        return ""
    host = urlsplit(s if "://" in s else "http://" + s).hostname or ""
    host = host.lower().rstrip(".")
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    if ".".join(parts[-2:]) in TWO_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def html_title(content: Optional[bytes], content_type: str = "") -> str:
    if not content or fetcher.media_type(content_type) not in fetcher.HTML_TYPES + ("",):
        return ""
    m = re.search(rb"<title[^>]*>(.*?)</title>", content[:200000], re.I | re.S)
    if not m:
        return ""
    raw = m.group(1).decode("utf-8", "replace")
    return " ".join(re.sub(r"<[^>]+>", " ", raw).replace("&amp;", "&").split())[:300]


_GENERIC = ("department of ", "dept. of ", "dept of ", "school of ", "college of ", "division of ",
            "program in ", "institute of ", "faculty of ", "the ")
_SWAP = (("department of ", " department"), ("school of ", " school"), ("college of ", " college"),
         ("division of ", " division"), ("institute of ", " institute"), ("program in ", " program"))


def name_variants(name: str) -> List[str]:
    """Other ways a page writes the same name: "Department of Finance" as "Finance
    Department", without a leading "The", "Dept." spelled out. Never the bare subject
    ("Finance"), which would match nearly any page. Pure."""
    n = " ".join((name or "").split())
    low = n.lower()
    out = [n]
    if low.startswith("the "):
        out.append(n[4:])
    if low.startswith(("dept. of ", "dept of ")):
        out.append("Department of " + n.split(" ", 2)[2])
    for prefix, suffix in _SWAP:
        if low.startswith(prefix):
            out.append(n[len(prefix):] + suffix)
        if low.endswith(suffix) and len(low) > len(suffix):
            out.append(prefix + n[: -len(suffix)])
    return list(dict.fromkeys(v for v in out if v.strip()))


def core_name(name: str) -> str:
    """The name without its leading "The", "School of", "Department of", and so on, when
    what is left has 2 words or more ("Social Work" from "School of Social Work"). Empty
    for a 1-word subject ("Business"), which too many pages would contain. Pure."""
    n = " ".join((name or "").split())
    changed = True
    while changed:
        changed = False
        for g in _GENERIC:
            if n.lower().startswith(g):
                n, changed = n[len(g):], True
    if n == " ".join((name or "").split()) or len(n.split()) < 2:
        return ""
    return n


def name_check(name: str, text: str, threshold: int = 90) -> Tuple[str, float, str]:
    """(result, score, how) for "is the unit's name on the page". Exact whole words
    first, then the variants of name_variants, then a close match (rapidfuzz
    partial_ratio of at least `threshold`, only for names of 10 or more letters). The
    core name ("Social Work" for "School of Social Work") counts too. Pure."""
    if not name or not text:
        return "fail", 0.0, "no text" if not text else "no name"
    if fetcher.name_in_text(name, text):
        return "pass", 1.0, "exact"
    for v in name_variants(name)[1:]:
        if fetcher.name_in_text(v, text):
            return "pass", 0.95, f"variant: {v}"
    core = core_name(name)
    if core and fetcher.name_in_text(core, text):
        return "pass", 0.8, f"core name: {core}"
    norm = fetcher._norm_words(name)
    if len(norm.replace(" ", "")) >= 10:
        from rapidfuzz import fuzz
        hay = fetcher._norm_words(text)[:400000]
        score = fuzz.partial_ratio(norm, hay)
        if score >= threshold:
            return "pass", round(score / 100, 3), f"close match ({score:.0f})"
        return "fail", round(score / 100, 3), f"best close match {score:.0f}"
    return "fail", 0.0, "not found"


# ─────────────────────────  THE FOUR CHECKS  ─────────────────────────

def check_status(page: Optional[dict]) -> Tuple[str, str]:
    """Dead link. pass: the page answered 2xx. fail: no URL, the host does not exist or
    does not answer, 404 or 410, a URL that is not http(s). unclear: the site refuses
    the fetcher, a server error, a timeout, too many redirects, or a TLS problem."""
    if page is None:
        return "fail", "no URL cited"
    outcome, status, detail = page["outcome"], page.get("status"), page.get("detail", "")
    if outcome in ("ok", "too_large"):
        return "pass", f"HTTP {status}" + (" (over 5 MB, not read)" if outcome == "too_large" else "")
    if outcome == "http_error":
        if status in DEAD_STATUSES:
            return "fail", f"HTTP {status}"
        if status in REFUSING_STATUSES:
            return "unclear", f"HTTP {status}: the site refuses the fetcher"
        return "unclear", f"HTTP {status}"
    if outcome == "refused":
        if "proxy refused" in detail:
            return "unclear", detail
        return "fail", detail
    if outcome == "error":
        if "could not reach the host" in detail or "Name or service not known" in detail:
            return "fail", detail
        return "unclear", detail
    return "unclear", f"{outcome}: {detail}"


def _path_depth(url: str) -> int:
    path = urlsplit(url or "").path.strip("/")
    return len([p for p in path.split("/") if p and p.lower() not in ("index.html", "index.php", "home", "en")])


def check_soft_404(page: Optional[dict]) -> Tuple[str, str]:
    """A page that answers 200 but is an error page: its title or its first 300
    characters say "not found", or a cited deep link ends on the home page. Only for a
    page that arrived; anything else is unclear."""
    if page is None or page["outcome"] != "ok":
        return "unclear", "no page"
    title, head = page.get("title", ""), (page.get("text") or "")[:300]
    for where, s in (("title", title), ("start of the page", head)):
        m = NOT_FOUND.search(s or "")
        if m:
            return "fail", f"{where} says {m.group(0)!r}"
    final = page.get("final_url") or ""
    if final and final != page["url"] and NOT_FOUND_PATH.search(urlsplit(final).path):
        return "fail", f"redirected to an error page {final}"
    if _path_depth(page["url"]) >= 1 and _path_depth(page.get("final_url") or page["url"]) == 0 \
            and page.get("final_url") and page["final_url"].rstrip("/") != page["url"].rstrip("/"):
        return "fail", f"redirected to the home page {page['final_url']}"
    if page.get("name_check", "done") != "done":
        return "pass", page["name_check"]
    return "pass", ""


def check_domain(url: str, page: Optional[dict], domains: Sequence[str]) -> Tuple[str, str]:
    """pass when the page (after redirects) is on one of the university's domains, fail
    when it is elsewhere, unclear when there is no URL or no known domain."""
    if not url:
        return "unclear", "no URL cited"
    if not domains:
        return "unclear", "the university's website is not known"
    final = (page or {}).get("final_url") or url
    got = site_domain(final)
    if got in domains:
        return "pass", got
    cited = site_domain(url)
    return "fail", f"{got} is not {'/'.join(sorted(domains))}" + (f" (cited {cited})" if cited != got else "")


def check_name(name: str, page: Optional[dict]) -> Tuple[str, float, str]:
    if page is None or page["outcome"] not in ("ok",):
        return "unclear", 0.0, "no page"
    if page.get("name_check", "done") != "done":
        return "unclear", 0.0, page["name_check"]
    return name_check(name, page.get("text") or "")


def run_checks(claim: dict, page: Optional[dict], domains: Sequence[str]) -> Dict[str, Tuple[str, float, str]]:
    """The four results for one claim: kind -> (result, score, detail). Pure."""
    out = {}
    r, d = check_status(page)
    out["page_status"] = (r, 1.0 if r == "pass" else 0.0, d)
    r, d = check_soft_404(page)
    out["soft_404"] = (r, 1.0 if r == "pass" else 0.0, d)
    r, d = check_domain(claim.get("url") or "", page, domains)
    out["on_domain"] = (r, 1.0 if r == "pass" else 0.0, d)
    out["name_on_page"] = check_name(claim["name"], page)
    return out


def overall(results: Dict[str, Tuple[str, float, str]]) -> str:
    """pass when all four pass, fail when any fails, else unclear. Pure."""
    rs = [results[k][0] for k in KINDS]
    if "fail" in rs:
        return "fail"
    return "pass" if all(r == "pass" for r in rs) else "unclear"


# ─────────────────────────  CLAIMS  ─────────────────────────

def _bq():
    from google.cloud import bigquery
    return bigquery.Client(project="wikidata-academia")


CLAIM_QUERY = """
SELECT c.candidate_id, c.run_id, c.university_qid, c.parent_qid, c.name, c.unit_type, c.source_url,
       c.website, c.provider, c.llm_call_ids, c.created_at, l.request_config
FROM `wikidata-academia.academiabot.candidates` c
LEFT JOIN `wikidata-academia.academiabot.llm_calls` l ON l.llm_call_id = c.llm_call_ids[SAFE_OFFSET(0)]
WHERE {where}
"""


def _config_of(row: dict) -> dict:
    try:
        return json.loads(row.get("request_config") or "{}")
    except (TypeError, ValueError):
        return {}


def search_flag(request_config: dict) -> str:
    """"on" when the request had a search tool, else "off". Pure."""
    return "on" if request_config.get("tools") else "off"


def latest_lists(rows: List[dict]) -> List[dict]:
    """Keep, for each (parent, provider, sample), only the rows of the latest run that
    produced that list. The week 4 department step was run more than once for some
    schools; the last run is the list the judge report scored. Pure."""
    latest: Dict[Tuple, str] = {}
    for r in rows:
        key = (r["parent_qid"], r["provider"], r["sample"])
        if r["run_id"] > latest.get(key, ""):
            latest[key] = r["run_id"]
    return [r for r in rows if latest[(r["parent_qid"], r["provider"], r["sample"])] == r["run_id"]]


def to_claim(row: dict, setting: str) -> dict:
    cfg = _config_of(row)
    return {"setting": setting, "candidate_id": row["candidate_id"], "run_id": row["run_id"],
            "provider": row["provider"], "sample": cfg.get("sample"), "search": search_flag(cfg),
            "university_qid": row["university_qid"], "parent_qid": row["parent_qid"],
            "name": row["name"], "unit_type": (row.get("unit_type") or "").lower(),
            "url": (row.get("source_url") or "").strip(), "website": row.get("website") or "",
            "llm_call_ids": list(row.get("llm_call_ids") or [])}


def load_claims(settings: Sequence[str], client=None, seed: int = DRAW_SEED,
                tier1_size: int = TIER1_SIZE) -> List[dict]:
    from google.cloud import bigquery
    client = client or _bq()

    def query(where, params=()):
        job = client.query(CLAIM_QUERY.format(where=where),
                           job_config=bigquery.QueryJobConfig(query_parameters=list(params)))
        return [dict(r) for r in job.result()]

    schools = {s["school_qid"] for s in read_dept_schools()}
    from wikidata_discover.eval import agreement
    universities = {u["qid"] for u in agreement.universities_to_run(None)}
    claims: List[dict] = []
    if "week3" in settings:
        rows = query("c.run_id = @run", [bigquery.ScalarQueryParameter("run", "STRING", WEEK3_RUN)])
        claims += [to_claim(r, "week3") for r in rows]
    if "schools_on" in settings:
        rows = query("c.run_id LIKE 'subunits-%' AND c.created_at >= @since AND c.parent_qid IN UNNEST(@q)",
                     [bigquery.ScalarQueryParameter("since", "TIMESTAMP", SCHOOLS_SINCE),
                      bigquery.ArrayQueryParameter("q", "STRING", sorted(universities))])
        claims += latest_lists([to_claim(r, "schools_on") for r in rows])
    if "depts_on" in settings:
        rows = query("c.run_id LIKE 'subunits-%' AND c.created_at BETWEEN @a AND @b AND c.parent_qid IN UNNEST(@q)",
                     [bigquery.ScalarQueryParameter("a", "TIMESTAMP", DEPTS_SINCE),
                      bigquery.ScalarQueryParameter("b", "TIMESTAMP", DEPTS_UNTIL),
                      bigquery.ArrayQueryParameter("q", "STRING", sorted(schools))])
        claims += latest_lists([to_claim(r, "depts_on") for r in rows])
    if "us_tier1" in settings:
        rows = query("c.run_id = 'us-tier1' AND c.legacy AND c.website IS NOT NULL AND c.website != ''")
        claims += draw_tier1(rows, tier1_size, seed)
    return sorted(claims, key=claim_order)


def draw_tier1(rows: List[dict], size: int, seed: int) -> List[dict]:
    """`size` random us-tier1 rows with a website, one per distinct website URL. The
    website is the cited page (those answers had no reference field). Same seed, same
    draw. Pure."""
    by_url: Dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: r["candidate_id"]):
        u = (r.get("website") or "").strip()
        if u.lower().startswith(("http://", "https://")) and u not in by_url:
            by_url[u] = r
    pool = [by_url[u] for u in sorted(by_url)]
    chosen = random.Random(seed).sample(pool, min(size, len(pool)))
    out = []
    for r in chosen:
        c = to_claim({**r, "source_url": r["website"]}, "us_tier1")
        c["search"] = "on" if c["provider"] == "openai" else "unknown"
        out.append(c)
    return out


def claim_order(c: dict):
    return (SETTINGS.index(c["setting"]), c["university_qid"] or "", c["parent_qid"] or "",
            c["provider"] or "", c["sample"] or 0, c["name"], c["url"], c["candidate_id"])


def read_dept_schools(path: Path = DEPT_SCHOOLS) -> List[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


# ─────────────────────────  WEBSITES OF THE UNIVERSITIES  ─────────────────────────

WEBSITE_QUERY = """SELECT ?u ?w WHERE { VALUES ?u { %s } ?u wdt:P856 ?w . }"""


def university_domains(qids: Iterable[str], known: Optional[Dict[str, List[str]]] = None,
                       run_query: Optional[Callable[[str], list]] = None) -> Dict[str, List[str]]:
    """For each university QID, the domains of its P856 websites on Wikidata, plus any
    website we already know (the ground truth lists)."""
    if run_query is None:
        from wikidata_discover.sparql_helpers import execute_sparql_bindings as run_query
    out: Dict[str, set] = {q: set() for q in qids if q}
    for q, urls in (known or {}).items():
        if q in out:
            out[q].update(site_domain(u) for u in urls if u)
    todo = sorted(out)
    for i in range(0, len(todo), 100):
        values = " ".join(f"wd:{q}" for q in todo[i:i + 100])
        for b in run_query(WEBSITE_QUERY % values):
            q = b["u"]["value"].rsplit("/", 1)[-1]
            out.setdefault(q, set()).add(site_domain(b["w"]["value"]))
    return {q: sorted(d for d in v if d) for q, v in out.items()}


# ─────────────────────────  FETCHING  ─────────────────────────

def page_record(result: fetcher.FetchResult, evidence_id: Optional[str]) -> dict:
    text, check = fetcher.read_result(result)
    return {"url": result.url, "outcome": result.outcome, "status": result.status,
            "final_url": result.final_url or "", "detail": result.detail,
            "content_type": result.content_type, "title": html_title(result.content, result.content_type),
            "name_check": check or "", "text": text or "", "bytes": len(result.content or b""),
            "evidence_id": evidence_id or ""}


def fetch_pages(urls: Sequence[str], log=None, fetch=fetcher.fetch, workers: int = FETCH_WORKERS,
                names_for: Optional[Dict[str, List[str]]] = None) -> Dict[str, dict]:
    """Fetch each URL once, in parallel; save each as an evidence row (in this thread,
    in URL order, so the run log is written by one thread only)."""
    urls = sorted(set(u for u in urls if u))
    pages: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (url, result) in enumerate(zip(urls, pool.map(fetch, urls)), 1):
            eid = None
            if log is not None:
                eid = fetcher.record(log, result, (names_for or {}).get(url, []))
            pages[url] = page_record(result, eid)
            if i % 50 == 0:
                console.print(f"[dim]fetched {i} of {len(urls)} pages[/dim]")
    return pages


def write_pages(out_dir: Path, pages: Dict[str, dict]) -> None:
    (out_dir / "pages").mkdir(parents=True, exist_ok=True)
    rows = []
    for i, url in enumerate(sorted(pages), 1):
        p = dict(pages[url])
        text = p.pop("text", "")
        p["text_file"] = f"pages/{i:05d}.txt"
        (out_dir / p["text_file"]).write_text(text, encoding="utf-8")
        rows.append(p)
    write_csv(out_dir / "pages.csv", rows, PAGE_COLUMNS)


PAGE_COLUMNS = ["url", "outcome", "status", "final_url", "detail", "content_type", "title", "name_check",
                "bytes", "evidence_id", "text_file"]


def read_pages(out_dir: Path) -> Dict[str, dict]:
    pages = {}
    with (out_dir / "pages.csv").open(newline="") as f:
        for r in csv.DictReader(f):
            r["status"] = int(r["status"]) if r["status"] else None
            r["text"] = (out_dir / r["text_file"]).read_text(encoding="utf-8")
            pages[r["url"]] = r
    return pages


# ─────────────────────────  REPORT  ─────────────────────────

CLAIM_COLUMNS = ["setting", "provider", "search", "sample", "university_qid", "parent_qid", "parent_label",
                 "name", "unit_type", "url", "final_url", "fetch_outcome", "http_status"] + \
                [f"{k}{s}" for k in KINDS for s in ("", "_detail")] + \
                ["name_score", "all_four", "candidate_id", "run_id", "evidence_id"]


def check_claims(claims: List[dict], pages: Dict[str, dict], domains: Dict[str, List[str]]) -> List[dict]:
    """claims.csv rows: each claim with its four results. Pure."""
    rows = []
    for c in claims:
        page = pages.get(c["url"]) if c["url"] else None
        res = run_checks(c, page, domains.get(c["university_qid"], []))
        row = {**c, "final_url": (page or {}).get("final_url", ""), "fetch_outcome": (page or {}).get("outcome", "no URL"),
               "http_status": (page or {}).get("status") or "", "evidence_id": (page or {}).get("evidence_id", ""),
               "name_score": res["name_on_page"][1], "all_four": overall(res)}
        for k in KINDS:
            row[k], row[f"{k}_detail"] = res[k][0], res[k][2]
        row["_results"] = res
        rows.append(row)
    return rows


BOOT_REPS = 2000


def cluster_interval(rows: List[dict], test, reps: int = BOOT_REPS, seed: int = DRAW_SEED):
    """95% percentile bootstrap interval of the share, resampling cited URLs (clusters)
    rather than claims: many claims cite one list page, so they are not independent and
    the Wilson interval on claims is too narrow. A claim without a URL is its own
    cluster. Same seed, same interval. Pure."""
    clusters: Dict[str, List[int]] = {}
    for i, r in enumerate(rows):
        clusters.setdefault(r.get("url") or f"#no-url-{i}", []).append(1 if test(r) else 0)
    stats = [(sum(v), len(v)) for _, v in sorted(clusters.items())]
    if not stats:
        return None, None
    rng = random.Random(seed)
    shares = []
    for _ in range(reps):
        k = n = 0
        for _ in range(len(stats)):
            a, b = stats[rng.randrange(len(stats))]
            k += a
            n += b
        shares.append(k / n)
    shares.sort()
    return round(shares[int(0.025 * reps)], 4), round(shares[min(reps - 1, int(0.975 * reps))], 4)


def _share(rows, test, clustered: bool = True) -> dict:
    n = len(rows)
    k = sum(1 for r in rows if test(r))
    lo, hi = wilson(k, n)
    clo, chi = cluster_interval(rows, test) if clustered else (None, None)
    blank = lambda v: v if v is not None else ""  # noqa: E731
    return {"k": k, "n": n, "share": round(k / n, 4) if n else "", "ci_low": blank(lo), "ci_high": blank(hi),
            "url_ci_low": blank(clo), "url_ci_high": blank(chi),
            "urls": len({r.get("url") or f"#{i}" for i, r in enumerate(rows)})}


MEASURES = (
    ("page_exists", lambda r: r["page_status"] == "pass"),
    ("dead", lambda r: r["page_status"] == "fail"),
    ("site_refused_or_unclear", lambda r: r["page_status"] == "unclear"),
    ("soft_404", lambda r: r["soft_404"] == "fail"),
    ("off_domain", lambda r: r["on_domain"] == "fail"),
    ("name_on_page", lambda r: r["name_on_page"] == "pass"),
    ("all_four_pass", lambda r: r["all_four"] == "pass"),
)


def summary(rows: List[dict]) -> List[dict]:
    """Per claim set, provider (and all providers), and measure: k of n claims, with a
    95% Wilson interval and a 95% interval that resamples cited URLs (url_ci_*; use this
    one, because claims that cite the same page are not independent). Also page_exists
    per distinct URL. Pure."""
    out = []
    for setting in SETTINGS:
        srows = [r for r in rows if r["setting"] == setting]
        if not srows:
            continue
        provs = [p for p in PROVIDERS if any(r["provider"] == p for r in srows)]
        provs += sorted({r["provider"] for r in srows} - set(PROVIDERS) - {None})
        for prov in provs + ["all"]:
            prow = [r for r in srows if prov == "all" or r["provider"] == prov]
            search = sorted({r["search"] for r in prow})
            for m, test in MEASURES:
                out.append({"setting": setting, "provider": prov, "search": "/".join(search),
                            "unit": "claim", "measure": m, **_share(prow, test)})
            answered = [r for r in prow if r["page_status"] == "pass"]
            for m, test in (("name_on_page_when_page_exists", lambda r: r["name_on_page"] == "pass"),
                            ("all_four_pass_when_page_exists", lambda r: r["all_four"] == "pass")):
                out.append({"setting": setting, "provider": prov, "search": "/".join(search),
                            "unit": "claim", "measure": m, **_share(answered, test)})
            urls = {}
            for r in prow:
                if r["url"]:
                    urls.setdefault(r["url"], r)
            out.append({"setting": setting, "provider": prov, "search": "/".join(search), "unit": "url",
                        "measure": "page_exists", **_share(list(urls.values()), MEASURES[0][1])})
    return out


def draw_pairs(rows: List[dict], size: int = PAIRS, seed: int = DRAW_SEED) -> List[dict]:
    """`size` claim-and-page pairs for a person to label, a third per provider (the
    first providers get the remainder), drawn from every claim with a cited URL in the
    week3, schools_on, and depts_on sets, dead and odd pages included. One pair per
    distinct (name, URL). Same seed, same draw. Pure."""
    pool = {}
    for r in rows:
        if r["setting"] in ("week3", "schools_on", "depts_on") and r["url"]:
            pool.setdefault((r["provider"], r["name"], r["url"]), r)
    rng = random.Random(seed)
    per = [size // len(PROVIDERS) + (1 if i < size % len(PROVIDERS) else 0) for i in range(len(PROVIDERS))]
    out = []
    for prov, n in zip(PROVIDERS, per):
        mine = [pool[k] for k in sorted(pool) if k[0] == prov]
        out += rng.sample(mine, min(n, len(mine)))
    return out


def draw_failures(rows: List[dict], size: int = FAILURES, seed: int = DRAW_SEED) -> List[dict]:
    """`size` random claims that failed at least one check (every claim set but us_tier1,
    one per distinct (name, URL)). Pure."""
    pool = {}
    for r in rows:
        if r["setting"] != "us_tier1" and r["all_four"] == "fail":
            pool.setdefault((r["provider"], r["name"], r["url"]), r)
    keys = sorted(pool)
    return [pool[k] for k in random.Random(seed + 1).sample(keys, min(size, len(keys)))]


def claim_text(r: dict) -> str:
    """The claim as the annotation pilot writes it: "Unit is part of Parent". Pure."""
    return f"{r['name']} is part of {r.get('parent_label') or r['parent_qid']}"


def s1_page_pass(rows: List[dict], week3_dir: Optional[Path]) -> List[dict]:
    """S1, the third way (SPECS.md, week 3): the agreement estimates counting only the
    week 3 school groups with at least one proposal whose four checks all pass. Needs
    the proposals.csv, groups.csv and cache_files.csv of a week 3 report."""
    if week3_dir is None or not (week3_dir / "proposals.csv").exists():
        return []
    from wikidata_discover.eval import agreement
    read = lambda name: list(csv.DictReader((week3_dir / name).open(newline="")))  # noqa: E731
    props, groups, runs = read("proposals.csv"), read("groups.csv"), read("cache_files.csv")
    passed = {(r["university_qid"], r["provider"], int(r["sample"] or 0), r["name"], r["url"])
              for r in rows if r["setting"] == "week3" and r["all_four"] == "pass"}
    out = []
    for u in agreement.universities_to_run(None):
        q = u["qid"]
        uprops = [dict(p, sample=int(p["sample"])) for p in props if p["university_qid"] == q]
        for i, p in enumerate(uprops):
            p["_i"] = i
        good = {p["group_id"] for p in uprops
                if (q, p["provider"], p["sample"], p["name"], p["source_url"]) in passed}
        ugroups = []
        for g in groups:
            if g["university_qid"] != q or g["group_id"] not in good:
                continue
            ugroups.append({**g, "member_index": [p["_i"] for p in uprops if p["group_id"] == g["group_id"]],
                            "truth_name": g.get("truth_name") or ""})
        answered = [(r["provider"], int(r["sample"])) for r in runs
                    if r["university_qid"] == q and str(r.get("cache_file_exists")) == "True"]
        for e in agreement.s1_estimates(q, u["name"], u["set"], len(u["schools"]), ugroups, uprops,
                                        PROVIDERS, answered):
            if e["mode"] == "all":
                out.append({**e, "mode": "page_pass"})
    return out


def parent_labels() -> Dict[str, str]:
    from wikidata_discover.eval import agreement
    labels = {u["qid"]: u["name"] for u in agreement.universities_to_run(None)}
    labels.update({s["school_qid"]: s["school_label"] for s in read_dept_schools()})
    return labels


def write_report(out_dir: Path, rows: List[dict], week3_dir: Optional[Path]) -> Dict[str, List[dict]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "claims.csv", rows, CLAIM_COLUMNS)
    tables = {"summary": summary(rows)}
    write_csv(out_dir / "summary.csv", tables["summary"],
              ["setting", "provider", "search", "unit", "measure", "k", "n", "share", "ci_low", "ci_high",
               "urls", "url_ci_low", "url_ci_high"])
    pair_cols = ["pair_id", "provider", "setting", "search", "claim", "url", "final_url", "fetch_outcome",
                 "http_status", "text_file", "evidence_id", "label", "reason", "passage", "notes"]
    pages_index = {}
    if (out_dir / "pages.csv").exists():
        pages_index = {r["url"]: r["text_file"] for r in csv.DictReader((out_dir / "pages.csv").open(newline=""))}
    pairs = [{**r, "pair_id": i, "claim": claim_text(r), "text_file": pages_index.get(r["url"], ""),
              "label": "", "reason": "", "passage": "", "notes": ""} for i, r in enumerate(draw_pairs(rows), 1)]
    write_csv(out_dir / "pairs_100.csv", pairs, pair_cols)
    fail_cols = ["failure_id", "provider", "setting", "claim", "url", "final_url"] + \
                [k for k in KINDS] + [f"{k}_detail" for k in KINDS] + ["text_file", "category", "notes"]
    fails = [{**r, "failure_id": i, "claim": claim_text(r), "text_file": pages_index.get(r["url"], ""),
              "category": "", "notes": ""} for i, r in enumerate(draw_failures(rows), 1)]
    write_csv(out_dir / "failures_20.csv", fails, fail_cols)
    tables["s1"] = s1_page_pass(rows, week3_dir)
    if tables["s1"]:
        write_csv(out_dir / "s1_page_pass.csv", tables["s1"])
    return tables


def record_checks(log, rows: List[dict]) -> int:
    n = 0
    for r in rows:
        for k in KINDS:
            result, score, detail = r["_results"][k]
            log.add_check(kind=k, result=result, candidate_id=r["candidate_id"], score=score,
                          detail=json.dumps({"setting": r["setting"], "provider": r["provider"],
                                             "sample": r["sample"], "search": r["search"], "url": r["url"],
                                             "final_url": r["final_url"], "why": detail}, sort_keys=True),
                          llm_call_ids=r["llm_call_ids"],
                          evidence_ids=[r["evidence_id"]] if r["evidence_id"] else [])
            n += 1
    return n


# ─────────────────────────  CHECK 1: YOUR 10 URLS  ─────────────────────────

CHECK10_LABELS = ("dead", "other_page", "correct")


def machine_label(results: Dict[str, Tuple[str, float, str]]) -> str:
    """How the checks sort one URL, in the words of the 10-URL test: dead (the link is
    dead), correct (all four pass), other_page (the page exists but fails another
    check), or unclear (the site refused the fetcher, or no check failed and one was
    unclear). Pure."""
    if results["page_status"][0] == "fail":
        return "dead"
    if results["page_status"][0] == "unclear":
        return "unclear"
    o = overall(results)
    return {"pass": "correct", "fail": "other_page"}.get(o, "unclear")


def run_check10(path: Path, out_dir: Path, fetch=fetcher.fetch, run_query=None) -> List[dict]:
    """Read your 10 URLs (url, unit_name, university_qid, your_label), fetch and check
    them, and write check10_result.csv with the machine's label next to yours."""
    with path.open(newline="") as f:
        items = [r for r in csv.DictReader(f) if (r.get("url") or "").strip()]
    bad = [r["your_label"] for r in items if r.get("your_label") not in CHECK10_LABELS]
    if bad:
        raise SystemExit(f"your_label must be one of {CHECK10_LABELS}; got {bad}")
    domains = university_domains({r["university_qid"] for r in items}, run_query=run_query)
    pages = fetch_pages([r["url"].strip() for r in items], fetch=fetch)
    out = []
    for r in items:
        url = r["url"].strip()
        claim = {"name": r["unit_name"], "url": url, "university_qid": r["university_qid"]}
        res = run_checks(claim, pages.get(url), domains.get(r["university_qid"], []))
        label = machine_label(res)
        out.append({"url": url, "unit_name": r["unit_name"], "university_qid": r["university_qid"],
                    "your_label": r["your_label"], "machine_label": label, "same": label == r["your_label"],
                    **{k: res[k][0] for k in KINDS}, **{f"{k}_detail": res[k][2] for k in KINDS}})
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "check10_result.csv", out)
    return out


# ─────────────────────────  COMMAND  ─────────────────────────

def run(args) -> Path:
    from wikidata_discover import run_log
    from wikidata_discover.batch import RESULTS_DIR as _R, operator_identity, source_identity  # noqa: F401
    if args.check10:
        out_dir = Path(args.out or RESULTS_DIR / "check10")
        rows = run_check10(Path(args.check10), out_dir)
        same = sum(1 for r in rows if r["same"])
        console.print(f"Check 1: {same} of {len(rows)} URLs sorted as you sorted them. {out_dir / 'check10_result.csv'}")
        return out_dir
    week3_dir = Path(args.week3_report) if args.week3_report else None
    if args.rescore:
        out_dir = Path(args.rescore)
        claims = json.loads((out_dir / "claims_input.json").read_text())
        pages = read_pages(out_dir)
        domains = json.loads((out_dir / "domains.json").read_text())
        rows = check_claims(claims, pages, domains)
        for r in rows:
            r["parent_label"] = r.get("parent_label") or ""
        tables = write_report(out_dir, rows, week3_dir)
        print_summary(tables)
        return out_dir

    settings = args.settings or list(SETTINGS)
    run_id = run_log.new_run_id("page-checks")
    out_dir = Path(args.out) if args.out else RESULTS_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    claims = load_claims(settings, tier1_size=args.tier1_size)
    labels = parent_labels()
    for c in claims:
        c["parent_label"] = labels.get(c["parent_qid"], "")
    console.print(f"{len(claims)} claims, {len({c['url'] for c in claims if c['url']})} distinct URLs")
    from wikidata_discover.eval import agreement
    known = {u["qid"]: [u["website"]] for u in agreement.universities_to_run(None)}
    domains = university_domains({c["university_qid"] for c in claims}, known=known)
    (out_dir / "claims_input.json").write_text(json.dumps(claims, indent=1, sort_keys=True))
    (out_dir / "domains.json").write_text(json.dumps(domains, indent=1, sort_keys=True))
    names_for: Dict[str, List[str]] = {}
    for c in claims:
        if c["url"]:
            names_for.setdefault(c["url"], []).append(c["name"])
    log = None
    if not args.no_run_log:
        identity = source_identity(out_dir)
        run_log.start(run_id, command="page-checks", args={"argv": sys.argv[1:]},
                      qids=sorted({c["university_qid"] for c in claims if c["university_qid"]}),
                      who=operator_identity(), git_commit=identity,
                      run_config={"settings": settings, "seed": DRAW_SEED, "tier1_size": args.tier1_size,
                                  "week3_run": WEEK3_RUN})
        log = run_log.current()
        if log is not None:
            log.attach_source_patch(out_dir, identity)
    outcome = "failed"
    try:
        pages = fetch_pages([c["url"] for c in claims], log=log, names_for=names_for)
        write_pages(out_dir, pages)
        rows = check_claims(claims, pages, domains)
        tables = write_report(out_dir, rows, week3_dir)
        if log is not None:
            n = record_checks(log, rows)
            console.print(f"{n} checks rows")
        outcome = "ok"
    finally:
        if log is not None:
            console.print(f"[dim]{run_log.describe(log.finish(outcome, {'out_dir': str(out_dir)}))}[/dim]")
    print_summary(tables)
    console.print(f"Report: {out_dir}")
    return out_dir


def print_summary(tables: Dict[str, List[dict]]) -> None:
    for r in tables["summary"]:
        if r["unit"] == "claim" and r["measure"] in ("page_exists", "all_four_pass"):
            console.print(f"{r['setting']:<11} {r['provider']:<10} {r['measure']:<14} "
                          f"{r['k']:>4} of {r['n']:<4} = {r['share']}  [{r['url_ci_low']}, {r['url_ci_high']}]")


def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--settings", nargs="+", choices=SETTINGS)
    p.add_argument("--tier1-size", type=int, default=TIER1_SIZE)
    p.add_argument("--out")
    p.add_argument("--no-run-log", action="store_true")
    p.add_argument("--rescore", help="an earlier output folder: check its saved pages again, no fetch")
    p.add_argument("--week3-report", help="a week 3 agreement report folder, for S1 page_pass")
    p.add_argument("--check10", help="your 10-URL CSV (url, unit_name, university_qid, your_label)")
    run(p.parse_args(argv))


if __name__ == "__main__":
    main()
