from wikidata_discover.scripts import check4_sample as c4


def cands():
    rows = [{"candidate_id": f"{i:04d}", "status": "missing", "name": f"School {i}",
             "university_qid": "Q1", "website": f"https://x.edu/{i}"} for i in range(50)]
    rows.append({"candidate_id": "9999", "status": "linked", "name": "Linked", "university_qid": "Q1"})
    return rows


def test_draw_takes_only_missing_and_ignores_input_order():
    a = c4.draw(cands())
    b = c4.draw(list(reversed(cands())))
    assert [r["candidate_id"] for r in a] == [r["candidate_id"] for r in b]
    assert len(a) == 20 and all(r["status"] == "missing" for r in a)


def test_sheet_rows_links_url_and_empty_columns():
    row = c4.sheet_rows([cands()[3]], {"Q1": "Test University"})[0]
    assert row["search_school_only"].endswith("search=School+3&fulltext=1")
    assert "search=Test+University+School+3" in row["search_university_plus_school"]
    assert row[c4.URL_COLUMN] == "https://x.edu/3"
    assert row["already on Wikidata (QID)"] == "" and row["really a school (yes/no)"] == ""
