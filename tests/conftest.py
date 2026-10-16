import pytest

from wikidata_discover import run_log


@pytest.fixture(autouse=True)
def _no_real_run_log(monkeypatch):
    """No test writes to the real BigQuery or bucket: run_log.start() returns a log that
    records nothing. Tests of the run log build a RunLog with fake clients instead."""
    monkeypatch.setattr(run_log, "DISABLED", True)
    monkeypatch.setattr(run_log, "_current", None)
