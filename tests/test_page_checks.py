"""Shuo, week 5: does the cited page exist and name the unit? (page_checks.py)"""
import csv

import pytest

from wikidata_discover import fetcher, run_log
from wikidata_discover.eval import page_checks as pc


def page(**kw):
    base = {"url": "https://www.uni.edu/schools", "outcome": "ok", "status": 200,
            "final_url": "https://www.uni.edu/schools", "detail": "", "title": "Schools | Uni",
            "name_check": "done", "text": "Our schools: the School of Social Work and the Department of Finance.",
            "evidence_id": "e1"}
    base.update(kw)
    return base


# ── small helpers ──

def test_wilson_interval_is_known_values():
    assert pc.wilson(0, 0) == (None, None)
    lo, hi = pc.wilson(50, 100)
    assert lo == pytest.approx(0.4038, abs=1e-4) and hi == pytest.approx(0.5962, abs=1e-4)
    lo, hi = pc.wilson(10, 10)
    assert hi == 1.0 and lo == pytest.approx(0.7225, abs=1e-4)


@pytest.mark.parametrize("url,dom", [("https://www.stern.nyu.edu/x", "nyu.edu"), ("nyu.edu", "nyu.edu"),
                                     ("https://www.ox.ac.uk/", "ox.ac.uk"), ("", ""),
                                     ("https://en.wikipedia.org/wiki/X", "wikipedia.org")])
def test_site_domain(url, dom):
    assert pc.site_domain(url) == dom


def test_html_title():
    assert pc.html_title(b"<html><head><title> 404 &amp; Not <b>Found</b></title>", "text/html") == "404 & Not Found"
    assert pc.html_title(b"%PDF", "application/pdf") == ""


# ── name on page ──

def test_exact_name_passes_and_a_one_word_core_never_does():
    assert pc.name_check("School of Social Work", "the School of Social Work is here")[0] == "pass"
    assert pc.name_check("School of Business", "We teach business.")[0] == "fail"


def test_swapped_and_core_names_pass_with_lower_scores():
    r, score, how = pc.name_check("Department of Finance", "The Finance Department teaches")
    assert (r, score) == ("pass", 0.95) and how.startswith("variant")
    r, score, how = pc.name_check("School of Social Work", "Schools: Social Work, Law")
    assert (r, score) == ("pass", 0.8) and how == "core name: Social Work"


def test_a_close_spelling_passes_and_a_different_name_fails():
    assert pc.name_check("Steinhardt School of Culture, Education, and Human Development",
                         "NYU Steinhardt School of Culture, Education and Human Development")[0] == "pass"
    assert pc.name_check("Graduate School of Journalism", "School of Law and School of Nursing")[0] == "fail"


def test_core_name():
    assert pc.core_name("The School of Social Work") == "Social Work"
    assert pc.core_name("School of Business") == ""
    assert pc.core_name("Tisch School of the Arts") == ""


# ── the four checks ──

@pytest.mark.parametrize("p,result", [
    (None, "fail"),
    (page(), "pass"),
    (page(outcome="http_error", status=404), "fail"),
    (page(outcome="http_error", status=410), "fail"),
    (page(outcome="http_error", status=403), "unclear"),
    (page(outcome="http_error", status=405), "unclear"),
    (page(outcome="http_error", status=503), "unclear"),
    (page(outcome="error", detail="the proxy could not reach the host (CONNECT 502 Bad Gateway)"), "fail"),
    (page(outcome="refused", detail="the host name does not resolve (gaierror)"), "fail"),
    (page(outcome="refused", detail="the proxy refused the host (CONNECT 403)"), "unclear"),
    (page(outcome="timeout", detail="over 20 s"), "unclear"),
    (page(outcome="error", detail="SSLCertVerificationError: x"), "unclear"),
])
def test_dead_link(p, result):
    assert pc.check_status(p)[0] == result


def test_a_405_from_nyu_is_not_dead():
    r, why = pc.check_status(page(url="https://www.nyu.edu/a", outcome="http_error", status=405))
    assert r == "unclear" and "refuses the fetcher" in why


@pytest.mark.parametrize("p,result", [
    (page(), "pass"),
    (page(title="Page Not Found | Uni"), "fail"),
    (page(text="Oops! The page you are looking for does not exist."), "fail"),
    (page(url="https://www.uni.edu/a/b", final_url="https://www.uni.edu/"), "fail"),
    (page(url="https://www.uni.edu/a/b", final_url="https://www.uni.edu/page-not-found"), "fail"),
    (page(url="https://www.uni.edu/", final_url="https://www.uni.edu/"), "pass"),
    (page(outcome="http_error", status=404), "unclear"),
])
def test_soft_404(p, result):
    assert pc.check_soft_404(p)[0] == result


def test_on_domain():
    assert pc.check_domain("https://www.stern.nyu.edu/x", page(final_url="https://www.stern.nyu.edu/x"), ["nyu.edu"])[0] == "pass"
    assert pc.check_domain("https://en.wikipedia.org/wiki/NYU", None, ["nyu.edu"])[0] == "fail"
    # a redirect onto the university's site counts
    assert pc.check_domain("https://bit.ly/x", page(final_url="https://www.nyu.edu/x"), ["nyu.edu"])[0] == "pass"
    assert pc.check_domain("", None, ["nyu.edu"])[0] == "unclear"
    assert pc.check_domain("https://www.nyu.edu/", None, [])[0] == "unclear"


def test_name_is_unclear_without_a_readable_page():
    assert pc.check_name("X", None)[0] == "unclear"
    assert pc.check_name("X", page(name_check="skipped: PDF text is not extracted"))[0] == "unclear"


def test_overall_and_machine_label():
    claim = {"name": "School of Social Work", "url": "https://www.uni.edu/schools"}
    good = pc.run_checks(claim, page(), ["uni.edu"])
    assert pc.overall(good) == "pass" and pc.machine_label(good) == "correct"
    other = pc.run_checks({**claim, "name": "School of Dentistry"}, page(), ["uni.edu"])
    assert pc.overall(other) == "fail" and pc.machine_label(other) == "other_page"
    dead = pc.run_checks(claim, page(outcome="http_error", status=404), ["uni.edu"])
    assert pc.machine_label(dead) == "dead"
    refused = pc.run_checks(claim, page(outcome="http_error", status=403), ["uni.edu"])
    assert pc.machine_label(refused) == "unclear"


# ── claims and draws ──

def test_latest_lists_keeps_the_last_run_of_each_list():
    rows = [{"parent_qid": "Q1", "provider": "openai", "sample": 1, "run_id": "subunits-1", "name": "A"},
            {"parent_qid": "Q1", "provider": "openai", "sample": 1, "run_id": "subunits-2", "name": "B"},
            {"parent_qid": "Q1", "provider": "openai", "sample": 2, "run_id": "subunits-1", "name": "C"}]
    assert [r["name"] for r in pc.latest_lists(rows)] == ["B", "C"]


def test_search_flag():
    assert pc.search_flag({"tools": [{"type": "web_search_preview"}]}) == "on"
    assert pc.search_flag({"tools": []}) == "off"
    assert pc.search_flag({}) == "off"


def _rows(n_per_provider=60):
    rows = []
    for p in pc.PROVIDERS:
        for i in range(n_per_provider):
            rows.append({"setting": "week3", "provider": p, "name": f"U{i}", "url": f"https://u.edu/{p}/{i}",
                         "all_four": "fail" if i % 3 == 0 else "pass"})
    return rows


def test_100_pairs_a_third_per_provider_same_seed_same_draw():
    a = pc.draw_pairs(_rows())
    b = pc.draw_pairs(_rows())
    assert a == b and len(a) == 100
    counts = {p: sum(1 for r in a if r["provider"] == p) for p in pc.PROVIDERS}
    assert counts == {"openai": 34, "anthropic": 33, "gemini": 33}
    assert len({(r["provider"], r["name"], r["url"]) for r in a}) == 100


def test_pairs_leave_out_us_tier1_and_claims_without_a_url():
    rows = _rows(5) + [{"setting": "us_tier1", "provider": "openai", "name": "T", "url": "https://t.edu", "all_four": "pass"},
                       {"setting": "week3", "provider": "openai", "name": "N", "url": "", "all_four": "fail"}]
    names = {r["name"] for r in pc.draw_pairs(rows)}
    assert "T" not in names and "N" not in names


def test_20_failures_are_failed_claims():
    f = pc.draw_failures(_rows())
    assert len(f) == 20 and all(r["all_four"] == "fail" for r in f)
    assert f == pc.draw_failures(_rows())


def test_tier1_draw_is_one_per_website_and_repeatable():
    rows = [{"candidate_id": f"c{i}", "run_id": "us-tier1", "university_qid": "Q1", "parent_qid": "Q1",
             "name": f"S{i}", "unit_type": "School", "source_url": "", "website": f"https://s{i % 400}.edu",
             "provider": "openai", "llm_call_ids": [], "request_config": None} for i in range(800)]
    a = pc.draw_tier1(rows, 300, 7)
    assert len(a) == 300 and len({c["url"] for c in a}) == 300
    assert [c["url"] for c in a] == [c["url"] for c in pc.draw_tier1(rows, 300, 7)]
    assert all(c["setting"] == "us_tier1" and c["search"] == "on" for c in a)


def test_summary_has_intervals_per_provider():
    rows = pc.check_claims([{"setting": "week3", "provider": "openai", "search": "on", "university_qid": "Q1",
                             "name": "School of Social Work", "url": "https://www.uni.edu/schools",
                             "candidate_id": "c1", "llm_call_ids": []}],
                           {"https://www.uni.edu/schools": page()}, {"Q1": ["uni.edu"]})
    s = {(r["provider"], r["unit"], r["measure"]): r for r in pc.summary(rows)}
    assert s[("openai", "claim", "all_four_pass")]["k"] == 1
    assert s[("openai", "claim", "all_four_pass")]["ci_low"] == pytest.approx(0.2065, abs=1e-4)
    assert ("all", "url", "page_exists") in s


def test_cluster_interval_is_wider_when_claims_share_a_page():
    shared = [{"url": "https://u.edu/list" if i < 50 else f"https://u.edu/{i}", "ok": i < 50} for i in range(100)]
    lo, hi = pc.cluster_interval(shared, lambda r: r["ok"], reps=500)
    wlo, whi = pc.wilson(50, 100)
    assert (hi - lo) > (whi - wlo)
    assert pc.cluster_interval(shared, lambda r: r["ok"], reps=500) == (lo, hi)
    assert pc.cluster_interval([], lambda r: True) == (None, None)


def test_university_domains_merge_wikidata_and_known_websites():
    def fake_query(q):
        assert "wd:Q1" in q
        return [{"u": {"value": "http://www.wikidata.org/entity/Q1"}, "w": {"value": "https://www.uni.edu/"}}]
    d = pc.university_domains(["Q1", "Q2"], known={"Q2": ["https://www.two.edu"]}, run_query=fake_query)
    assert d == {"Q1": ["uni.edu"], "Q2": ["two.edu"]}


# ── checks rows and the run log ──

class FakeLog:
    def __init__(self):
        self.checks, self.evidence = [], []

    def add_check(self, **kw):
        assert kw["kind"] in run_log.CHECK_KINDS and kw["result"] in run_log.CHECK_RESULTS
        self.checks.append(kw)
        return "k"

    def add_evidence(self, **kw):
        self.evidence.append(kw)
        return f"e{len(self.evidence)}"


def fake_fetch(url):
    if "dead" in url:
        return fetcher.FetchResult(url=url, outcome="http_error", status=404, final_url=url,
                                   content=b"nope", content_type="text/html")
    body = b"<html><title>Schools</title><body>School of Social Work</body></html>"
    return fetcher.FetchResult(url=url, outcome="ok", status=200, final_url=url, content=body,
                               content_type="text/html")


def test_each_url_is_fetched_once_and_each_claim_gets_four_checks_rows():
    log = FakeLog()
    claims = [{"setting": "week3", "provider": p, "sample": 1, "search": "off", "university_qid": "Q1",
               "name": "School of Social Work", "url": u, "candidate_id": f"c{p}{u[-1]}", "llm_call_ids": ["l1"]}
              for p in ("openai", "gemini") for u in ("https://www.uni.edu/a", "https://www.uni.edu/dead")]
    pages = pc.fetch_pages([c["url"] for c in claims], log=log, fetch=fake_fetch)
    assert len(log.evidence) == 2
    rows = pc.check_claims(claims, pages, {"Q1": ["uni.edu"]})
    assert pc.record_checks(log, rows) == 16
    kinds = {c["kind"] for c in log.checks}
    assert kinds == set(pc.KINDS)
    dead = [c for c in log.checks if c["kind"] == "page_status" and c["result"] == "fail"]
    assert len(dead) == 2 and all(c["evidence_ids"] for c in dead)


def test_new_check_kinds_are_documented():
    text = (pc.Path(__file__).parents[1] / "docs" / "RUN_LOG_SCHEMA.md").read_text()
    for k in ("soft_404", "on_domain"):
        assert k in text


def test_pages_survive_a_write_and_read(tmp_path):
    pages = {"https://www.uni.edu/a": pc.page_record(fake_fetch("https://www.uni.edu/a"), "e1")}
    pc.write_pages(tmp_path, pages)
    back = pc.read_pages(tmp_path)
    assert back["https://www.uni.edu/a"]["text"] == pages["https://www.uni.edu/a"]["text"]
    assert back["https://www.uni.edu/a"]["status"] == 200


# ── check 1: the 10 URLs ──

def test_check10_puts_the_machine_label_next_to_yours(tmp_path):
    f = tmp_path / "ten.csv"
    with f.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["url", "unit_name", "university_qid", "your_label"])
        w.writerow(["https://www.uni.edu/a", "School of Social Work", "Q1", "correct"])
        w.writerow(["https://www.uni.edu/dead", "School of Social Work", "Q1", "dead"])
        w.writerow(["https://www.uni.edu/b", "School of Dentistry", "Q1", "other_page"])
    rows = pc.run_check10(f, tmp_path / "out", fetch=fake_fetch,
                          run_query=lambda q: [{"u": {"value": "x/Q1"}, "w": {"value": "https://uni.edu"}}])
    assert [r["machine_label"] for r in rows] == ["correct", "dead", "other_page"]
    assert all(r["same"] for r in rows)
    assert (tmp_path / "out" / "check10_result.csv").exists()


def test_check10_refuses_an_unknown_label(tmp_path):
    f = tmp_path / "ten.csv"
    f.write_text("url,unit_name,university_qid,your_label\nhttps://u.edu,X,Q1,maybe\n")
    with pytest.raises(SystemExit):
        pc.run_check10(f, tmp_path, fetch=fake_fetch, run_query=lambda q: [])


def test_malformed_url_gives_no_domain_and_four_results():
    from wikidata_discover.eval import page_checks as pc
    assert pc.site_domain("https://[broken") == ""
    page = {"url": "https://[broken", "outcome": "refused", "status": None, "final_url": "",
            "detail": "not a valid URL", "text": "", "name_check": ""}
    res = pc.run_checks({"name": "School of Law", "url": "https://[broken"}, page, ["nyu.edu"])
    assert set(res) == set(pc.KINDS)
    assert res["page_status"][0] == "fail"


def test_oversized_error_page_keeps_its_http_status():
    from wikidata_discover.eval import page_checks as pc
    big = lambda status: {"outcome": "too_large", "status": status, "detail": "over 5 MB"}  # noqa: E731
    assert pc.check_status(big(404))[0] == "fail"
    assert pc.check_status(big(403))[0] == "unclear"
    assert pc.check_status(big(503))[0] == "unclear"
    assert pc.check_status(big(200))[0] == "pass"


def test_page_shorter_than_the_name_is_not_a_close_match():
    from wikidata_discover.eval import page_checks as pc
    assert pc.name_check("School of Business", "Business")[0] == "fail"
    assert pc.name_check("School of Business", "School of")[0] == "fail"
    assert pc.name_check("Department of Mathematics", "News from the Departmant of Mathematics today")[0] == "pass"


def test_a_cited_error_path_fails_the_soft_404_check_without_a_redirect():
    from wikidata_discover.eval import page_checks as pc
    page = {"url": "https://www.x.edu/page-not-found", "final_url": "https://www.x.edu/page-not-found",
            "outcome": "ok", "status": 200, "title": "X University", "text": "Welcome to X University"}
    assert pc.check_soft_404(page)[0] == "fail"
    lab = dict(page, url="https://www.x.edu/error-correction-lab", final_url="https://www.x.edu/error-correction-lab")
    assert pc.check_soft_404(lab)[0] == "pass"


def test_a_rescore_keeps_the_labels_a_person_wrote(tmp_path):
    from wikidata_discover.eval import page_checks as pc
    rows = [{"provider": "openai", "claim": "A is part of B", "url": "https://b.edu/a", "label": "", "reason": "",
             "passage": "", "notes": ""}]
    pc.write_csv(tmp_path / "pairs_100.csv", [dict(rows[0], label="supported", passage="A is a school of B")],
                 list(rows[0]))
    pc.keep_annotations(tmp_path / "pairs_100.csv", rows, ("label", "reason", "passage", "notes"))
    assert rows[0]["label"] == "supported" and rows[0]["passage"] == "A is a school of B"


def test_s1_page_pass_skips_a_broken_cache_file(tmp_path, monkeypatch):
    from wikidata_discover.eval import agreement
    from wikidata_discover.eval import page_checks as pc
    seen = []
    monkeypatch.setattr(agreement, "universities_to_run", lambda _: [{"qid": "Q1", "name": "U", "set": "eval", "schools": []}])
    monkeypatch.setattr(agreement, "s1_estimates", lambda q, n, st, k, g, p, provs, answered: seen.append(answered) or [])
    pc.write_csv(tmp_path / "proposals.csv", [], ["university_qid", "provider", "sample", "name", "source_url", "group_id"])
    pc.write_csv(tmp_path / "groups.csv", [], ["university_qid", "group_id", "truth_name"])
    pc.write_csv(tmp_path / "cache_files.csv", [
        {"university_qid": "Q1", "provider": "openai", "sample": 1, "cache_file_exists": True, "missing_from_cache": False},
        {"university_qid": "Q1", "provider": "anthropic", "sample": 1, "cache_file_exists": True, "missing_from_cache": True}],
        ["university_qid", "provider", "sample", "cache_file_exists", "missing_from_cache"])
    pc.s1_page_pass([], tmp_path)
    assert seen == [[("openai", 1)]]


def test_a_rescore_without_the_week3_report_removes_an_old_s1_file(tmp_path):
    from wikidata_discover.eval import page_checks as pc
    (tmp_path / "s1_page_pass.csv").write_text("old\n")
    pc.write_report(tmp_path, [], None)
    assert not (tmp_path / "s1_page_pass.csv").exists()
