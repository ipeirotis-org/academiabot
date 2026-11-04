"""Command-line checks that stop a bad argument before any AI call. No network."""
import sys

import pytest

from wikidata_discover import cli, llm_helpers


@pytest.fixture
def run(monkeypatch):
    """Run the CLI with these arguments. Returns the subunits calls it would make."""
    calls = []
    monkeypatch.setattr(cli, "subunits_with_run_log", lambda qids, argv, fetch_pages=True:
                        calls.append((qids, llm_helpers.SUBUNIT_MAX_SEARCHES)))
    monkeypatch.setattr(llm_helpers, "SUBUNIT_MAX_SEARCHES", 5)

    def go(*args):
        monkeypatch.setattr(sys, "argv", ["prog", *args])
        cli.run_cli()
        return calls
    return go


@pytest.mark.parametrize("value", ["0", "-2", "1.5", "two"])
def test_max_searches_below_one_or_not_whole_stops_before_any_call(run, capsys, value):
    with pytest.raises(SystemExit) as e:
        run("subunits", "Q770467", f"--max-searches={value}")
    assert e.value.code == 2
    assert "must be a whole number of 1 or more" in capsys.readouterr().err
    assert llm_helpers.SUBUNIT_MAX_SEARCHES == 5


def test_a_valid_max_searches_is_used(run):
    assert run("subunits", "Q770467", "--max-searches", "3") == [(["Q770467"], 3)]


def test_without_max_searches_the_default_stays(run):
    assert run("subunits", "Q770467") == [(["Q770467"], 5)]
