"""Tests of the week 6 verifier (Shuo; SPECS.md, "Shuo, week 6")."""

import csv
import json

import pytest

from wikidata_discover import llm_helpers
from wikidata_discover.eval import verifier as v
from wikidata_discover.eval.minicheck import word_chunks
from wikidata_discover.llm_helpers import LLMHelper, parse_verify_answer, verify_cache_key, verify_prompt

SENTENCE = "The School of Applied Ceramics is one of the twelve schools of Northfield State University."
FILLER = ("Campus news. The library extends its hours during the exam period. Parking permits are on sale. ")


class FakeModel:
    """A model that can read at most `limit` characters at once. It answers supported, with
    the sentence as its quote, only when the sentence is in the piece it was given."""

    def __init__(self, limit=v.PIECE_CHARS + 2000, quote=SENTENCE, verdict_without="unsupported"):
        self.limit, self.quote, self.verdict_without = limit, quote, verdict_without
        self.calls = []

    def __call__(self, provider, unit, parent, piece, piece_no, pieces):
        prompt = verify_prompt(unit, parent, piece, piece_no, pieces)
        if len(piece) > v.PIECE_CHARS or len(prompt) > self.limit:
            raise AssertionError("the model was given more than it can read at once")
        self.calls.append(piece_no)
        if SENTENCE in piece:
            return {"verdict": "supported", "quote": self.quote, "reason": "named as a school"}, f"call{piece_no}"
        return {"verdict": self.verdict_without, "quote": "", "reason": "unit not on this piece"}, f"call{piece_no}"


# ─────────────────────────  The three SPECS.md tests  ─────────────────────────

def test_long_page_with_the_sentence_near_the_end_is_supported():
    page = v.long_test_page(SENTENCE, FILLER, 100_000)
    assert len(page) > 100_000 - 10 and page.index(SENTENCE) > 0.9 * len(page)
    model = FakeModel()
    out = v.verify_claim("School of Applied Ceramics", "Northfield State University", page, ask=model)
    assert out["verdict"] == "supported" and out["quote_ok"] == "yes"
    assert out["pieces"] == len(v.split_pieces(page)) > 1
    assert out["piece"] >= out["pieces"] - 1          # found in one of the last pieces
    assert model.calls == list(range(1, out["piece"] + 1))


def test_quote_not_on_the_page_word_for_word_gives_unclear():
    page = "Our Schools: " + SENTENCE + " " + FILLER
    model = FakeModel(quote="The School of Applied Ceramics is one of twelve schools at Northfield State University.")
    out = v.verify_claim("School of Applied Ceramics", "Northfield State University", page, ask=model)
    assert out["verdict"] == "unclear" and out["quote_ok"] == "no"
    assert out["model_verdict"] == "supported"


def test_page_read_in_full_that_never_names_the_unit_is_unsupported():
    page = FILLER * 400   # several pieces, none names the unit
    model = FakeModel()
    out = v.verify_claim("School of Applied Ceramics", "Northfield State University", page, ask=model)
    assert out["verdict"] == "unsupported"
    assert model.calls == list(range(1, out["pieces"] + 1)) and out["pieces"] > 1


# ─────────────────────────  Pieces and quotes  ─────────────────────────

def test_split_pieces_are_fixed_and_overlap():
    text = "".join(chr(97 + i % 26) for i in range(30_000))
    pieces = v.split_pieces(text, size=10_000, overlap=1_000)
    assert [len(p) for p in pieces[:-1]] == [10_000] * (len(pieces) - 1)
    assert all(a[-1_000:] == b[:1_000] for a, b in zip(pieces, pieces[1:]))
    assert pieces[-1] == text[-len(pieces[-1]):]
    assert v.split_pieces("short", 10, 2) == ["short"]
    with pytest.raises(ValueError):
        v.split_pieces(text, size=10, overlap=10)


def test_a_sentence_on_a_piece_boundary_is_whole_in_one_piece():
    text = "x" * (v.PIECE_CHARS - 50) + " " + SENTENCE + " " + "y" * 20_000
    assert any(SENTENCE in p for p in v.split_pieces(text))


def test_quote_check_evens_out_spaces_and_typography_only():
    page = "Home › Schools  of\nthe University – the “School of Law” is here."
    assert v.quote_on_page('the "School of Law" is here', page)
    assert v.quote_on_page("Schools of the University - the", page)
    assert v.quote_on_page("[title] Home ... School of Law", page)
    assert not v.quote_on_page("the school of law is here", page)      # case is kept
    assert not v.quote_on_page("the School of Laws", page)
    assert not v.quote_on_page("", page)


def test_supported_without_a_quote_is_unclear_and_no_answer_is_unclear():
    a = v.checked_answer({"verdict": "supported", "quote": "", "reason": "r"}, "page")
    assert a["verdict"] == "unclear"
    assert v.checked_answer(None, "page")["verdict"] == "unclear"
    b = v.checked_answer({"verdict": "unsupported", "quote": "", "reason": "not named"}, "page")
    assert b["verdict"] == "unsupported"


def test_combine_supported_beats_unclear_beats_unsupported():
    s = {"verdict": "supported", "quote": "q", "reason": "", "quote_ok": "yes"}
    u = {"verdict": "unclear", "quote": "", "reason": "", "quote_ok": ""}
    n = {"verdict": "unsupported", "quote": "", "reason": "", "quote_ok": ""}
    assert v.combine([n, u, s]) == (s, 3)
    assert v.combine([n, u, n]) == (u, 2)
    assert v.combine([n, n])[0]["verdict"] == "unsupported"


def test_unreadable_pages_need_no_model():
    def no_model(*a):
        raise AssertionError("no model call expected")
    dead = v.verify_claim("X", "Y", "404 Not Found", "http_error", "404", ask=no_model)
    assert dead["verdict"] == "unsupported" and dead["decided_by"] == "page_status"
    for outcome, status, text in (("http_error", "403", "Forbidden"), ("http_error", "503", "x"),
                                  ("refused", "", ""), ("ok", "200", "  ")):
        assert v.verify_claim("X", "Y", text, outcome, status, ask=no_model)["verdict"] == "unclear"


def test_one_supporting_piece_stops_the_reading():
    page = SENTENCE + " " + FILLER * 400
    model = FakeModel()
    out = v.verify_claim("School of Applied Ceramics", "Northfield State University", page, ask=model)
    assert out["verdict"] == "supported" and model.calls == [1] and out["llm_call_ids"] == ["call1"]


# ─────────────────────────  The LLM call  ─────────────────────────

def test_parse_verify_answer():
    assert parse_verify_answer('```json\n{"verdict": "supported", "quote": "a", "reason": "b"}\n```') == \
        {"verdict": "supported", "quote": "a", "reason": "b"}
    for bad in ("", "no json", '{"verdict": "maybe", "quote": "", "reason": ""}', '{"quote": "a"}'):
        with pytest.raises(ValueError):
            parse_verify_answer(bad)


def test_verify_cache_key_changes_with_every_input():
    base = ("openai", "School of Law", "NYU", "piece text", 1, 2)
    k = verify_cache_key(*base)
    for i, other in enumerate(("anthropic", "School of Laws", "Columbia", "piece text!", 2, 3)):
        changed = list(base)
        changed[i] = other
        assert verify_cache_key(*changed) != k
    assert verify_cache_key(*base) == k


def test_verify_piece_caches_a_good_answer_and_retries_a_bad_one(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_helpers, "_CACHE_DIR", tmp_path)
    answers = iter([(None, "not json"), (None, '{"verdict": "supported", "quote": "q", "reason": "r"}')])
    sent = []

    def fake(provider, model, prompt):
        sent.append(prompt)
        return next(answers)
    monkeypatch.setattr(llm_helpers, "_verify_request", fake)
    got, _ = LLMHelper.verify_piece("openai", "School of Law", "NYU", "the School of Law")
    assert got["verdict"] == "supported" and len(sent) == 2
    monkeypatch.setattr(llm_helpers, "_verify_request", lambda *a: pytest.fail("cache not used"))
    again, _ = LLMHelper.verify_piece("openai", "School of Law", "NYU", "the School of Law")
    assert again == got


def test_verify_piece_caches_nothing_when_every_attempt_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_helpers, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm_helpers, "_verify_request", lambda *a: (None, "nope"))
    assert LLMHelper.verify_piece("openai", "A", "B", "text") == (None, None)
    assert list(tmp_path.iterdir()) == []


# ─────────────────────────  Scoring  ─────────────────────────

def _rows():
    rows = []
    for prov, label, llm, mc in (("openai", "supported", "supported", "supported"),
                                 ("openai", "unsupported", "unsupported", "not supported"),
                                 ("anthropic", "unclear", "unsupported", "not supported"),
                                 ("anthropic", "supported", "unclear", "supported"),
                                 ("gemini", "supported", "supported", "not supported"),
                                 ("gemini", "unclear", "unclear", "not supported")):
        rows.append({"provider": prov, "label": label, "label2": v.two_way(label), "llm": llm,
                     "llm_2": v.two_way(llm), "minicheck_2": mc,
                     "decided_by": "page_status" if label == "unclear" else "openai"})
    return rows


def test_accuracy_and_confusion_per_provider_balanced_and_weighted():
    acc, conf = v.score_tables(_rows(), {"openai": 0.5, "anthropic": 0.25, "gemini": 0.25}, ["llm", "minicheck"])
    get = {(a["method"], a["way"], a["provider"]): a for a in acc if a["scope"] == "all pairs"}
    assert get[("llm", "3-way", "openai")]["accuracy"] == 1.0
    assert get[("llm", "3-way", "anthropic")]["accuracy"] == 0.0
    assert get[("llm", "3-way", "gemini")]["accuracy"] == 1.0
    assert get[("llm", "3-way", "all (balanced, as sampled)")]["accuracy"] == round(4 / 6, 4)
    assert get[("llm", "3-way", "all (weighted by page share)")]["accuracy"] == 0.75
    # two-way: the anthropic unclear-vs-unsupported pair is now right
    assert get[("llm", "2-way", "anthropic")]["accuracy"] == 0.5
    assert ("minicheck", "3-way", "openai") not in get
    assert get[("minicheck", "2-way", "all (balanced, as sampled)")]["accuracy"] == round(5 / 6, 4)
    conf = [c for c in conf if c["scope"] == "all pairs"]
    cell = [c for c in conf if c["method"] == "llm" and c["way"] == "3-way" and c["provider"] == "anthropic"
            and c["label"] == "unclear" and c["answer"] == "unsupported"]
    assert cell[0]["pairs"] == 1
    assert sum(c["pairs"] for c in conf if c["method"] == "llm" and c["way"] == "3-way" and c["provider"] == "all") == 6


def test_page_weights_count_each_providers_distinct_pages():
    claims = [{"provider": "openai", "evidence_id": "e1", "setting": "week3"},
              {"provider": "openai", "evidence_id": "e1", "setting": "depts_on"},
              {"provider": "anthropic", "evidence_id": "e1", "setting": "week3"},
              {"provider": "gemini", "evidence_id": "e2", "setting": "schools_on"},
              {"provider": "gemini", "evidence_id": "e3", "setting": "us_tier1"},
              {"provider": "gemini", "evidence_id": "", "setting": "week3"}]
    assert v.page_weights(claims) == {"openai": 1 / 3, "anthropic": 1 / 3, "gemini": 1 / 3}


def test_page_weights_use_the_url_when_the_report_has_no_evidence_ids():
    """Review of pull request 29: a week 5 report made with --no-run-log has no evidence ids."""
    claims = [{"provider": "openai", "evidence_id": "", "url": "https://a.edu/x", "setting": "week3"},
              {"provider": "openai", "evidence_id": "", "url": "https://a.edu/x", "setting": "depts_on"},
              {"provider": "anthropic", "evidence_id": "", "url": "https://a.edu/y", "setting": "week3"},
              {"provider": "anthropic", "evidence_id": "", "url": "https://a.edu/z", "setting": "schools_on"},
              {"provider": "gemini", "evidence_id": "", "url": "https://a.edu/w", "setting": "us_tier1"}]
    assert v.page_weights(claims) == {"openai": 1 / 3, "anthropic": 2 / 3, "gemini": 0.0}


def test_spot_check_draws_ten_supported_quotes_the_same_way_each_time():
    rows = [{"pair_id": str(i), "provider": "openai", "claim": "A is part of B", "text_file": "t",
             "llm": "supported" if i % 2 else "unsupported", "llm_quote": f"q{i}"} for i in range(40)]
    a, b = v.spot_check(rows, "llm"), v.spot_check(rows, "llm")
    assert a == b and len(a) == 10 and all(int(r["pair_id"]) % 2 for r in a)


def test_spot_check_keeps_a_persons_answers_on_rescore():
    """Review of pull request 29: the same pair with the same quote keeps quote_on_page and notes."""
    rows = [{"pair_id": str(i), "provider": "openai", "claim": "A is part of B", "text_file": "t",
             "llm": "supported", "llm_quote": f"q{i}"} for i in range(3)]
    earlier = [{"pair_id": "0", "quote": "q0", "quote_on_page": "yes", "notes": "title only"},
               {"pair_id": "1", "quote": "old quote", "quote_on_page": "no", "notes": "x"}]
    got = {r["pair_id"]: r for r in v.spot_check(rows, "llm", earlier=earlier)}
    assert (got["0"]["quote_on_page"], got["0"]["notes"]) == ("yes", "title only")
    assert (got["1"]["quote_on_page"], got["1"]["notes"]) == ("", "")   # the quote changed: ask again


def test_anthropic_verify_call_sends_temperature_0(monkeypatch):
    """Review of pull request 29: the Anthropic request had no temperature."""
    sent = {}

    class Msgs:
        def create(self, **kw):
            sent.update(kw)
            return type("R", (), {"content": []})()

    class Client:
        messages = Msgs()

        def with_options(self, **kw):
            return self

    monkeypatch.setattr(llm_helpers, "_get_anthropic_client", lambda: Client())
    llm_helpers._verify_request("anthropic", "m", "prompt")
    assert sent["temperature"] == 0
    import hashlib
    from wikidata_discover import config
    old_extra = f"parent=P|piece={hashlib.sha256(b'x').hexdigest()[:16]}|1/1|max_tokens={llm_helpers.VERIFY_MAX_TOKENS}"
    old_key = llm_helpers._cache_key("U", "anthropic", config.ANTHROPIC_MODEL, purpose="verify",
                                     prompt_hash=llm_helpers.VERIFY_PROMPT_HASH, extra=old_extra)
    assert verify_cache_key("anthropic", "U", "P", "x", 1, 1) != old_key   # an answer without temperature 0 is not reused


def test_load_pairs_reads_text_and_labels(tmp_path):
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "1.txt").write_text("hello")
    with (tmp_path / "pairs.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["pair_id", "provider", "claim", "url", "text_file", "label"])
        w.writeheader()
        w.writerow({"pair_id": "1", "provider": "openai", "claim": "A is part of B", "url": "u",
                    "text_file": "pages/1.txt", "label": "Supported"})
        w.writerow({"pair_id": "2", "provider": "gemini", "claim": "C is part of D", "url": "u",
                    "text_file": "pages/missing.txt", "label": "unsupported"})
    rows = v.load_pairs(tmp_path / "pairs.csv")
    assert rows[0]["_text"] == "hello" and rows[0]["label"] == "supported"
    assert rows[1]["_text"] == "" and rows[1]["label2"] == "not supported"


def test_candidate_ids_follow_the_week5_draw():
    claims = [{"setting": "week3", "provider": "openai", "name": "A", "parent_label": "B", "parent_qid": "Q1",
               "url": "u", "candidate_id": "c1"},
              {"setting": "schools_on", "provider": "openai", "name": "A", "parent_label": "B", "parent_qid": "Q1",
               "url": "u", "candidate_id": "c2"}]
    pairs = [{"pair_id": "1", "provider": "openai", "claim": "A is part of B", "url": "u"}]
    assert v.candidate_ids(pairs, claims) == {"1": "c1"}


def test_split_claim():
    assert v.split_claim("Division of Arts is part of the College is part of X") == \
        ("Division of Arts is part of the College", "X")
    with pytest.raises(ValueError):
        v.split_claim("no claim here")


def test_run_pairs_with_minicheck_uses_page_status_for_unreadable_pages():
    pairs = [{"pair_id": "1", "provider": "openai", "claim": "School of Applied Ceramics is part of Northfield State University",
              "_text": SENTENCE, "fetch_outcome": "ok", "http_status": "200"},
             {"pair_id": "2", "provider": "gemini", "claim": "A is part of B", "_text": "Forbidden",
              "fetch_outcome": "http_error", "http_status": "403"}]
    v.run_pairs(pairs, "openai", ask=FakeModel(), minicheck_score=lambda doc, claim: 0.9)
    assert pairs[0]["llm"] == "supported" and pairs[0]["minicheck_2"] == "supported"
    assert pairs[1]["llm"] == "unclear" and pairs[1]["minicheck_prob"] == "" and pairs[1]["minicheck_2"] == "not supported"


def test_record_checks_writes_one_verifier_row_per_method():
    class Log:
        rows = []

        def add_check(self, **kw):
            self.rows.append(kw)
    pairs = [{"candidate_id": "c1", "evidence_id": "e1", "url": "u", "llm": "supported", "llm_quote": "q",
              "llm_quote_ok": "yes", "llm_reason": "r", "decided_by": "openai", "pieces": 1, "piece": 1,
              "llm_call_ids": ["k"], "minicheck_2": "not supported", "minicheck_prob": 0.2},
             {"candidate_id": "", "llm": "unclear"}]
    log = Log()
    assert v.record_checks(log, pairs, "openai", "gpt-4o") == 2
    assert [r["result"] for r in log.rows] == ["pass", "fail"]
    assert all(r["kind"] == "verifier" for r in log.rows)
    assert json.loads(log.rows[0]["detail"])["quote"] == "q" and log.rows[1]["score"] == 0.2


def test_minicheck_word_chunks_overlap():
    text = " ".join(f"w{i}" for i in range(1000))
    chunks = word_chunks(text, 400, 50)
    assert [len(c.split()) for c in chunks] == [400, 400, 300]
    assert chunks[0].split()[-50:] == chunks[1].split()[:50]
    assert word_chunks("") == [""]


def test_scores_are_also_given_for_the_pages_the_model_read():
    acc, _ = v.score_tables(_rows(), {"openai": 0.5, "anthropic": 0.25, "gemini": 0.25}, ["llm"])
    read = {(a["way"], a["provider"]): a for a in acc if a["scope"] == "pages the model read" and a["method"] == "llm"}
    assert read[("3-way", "all (balanced, as sampled)")]["pairs"] == 4
    assert read[("3-way", "anthropic")]["pairs"] == 1 and read[("3-way", "anthropic")]["accuracy"] == 0.0
