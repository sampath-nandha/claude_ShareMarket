"""Render every dashboard page headlessly (demo data) and fail on any exception."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1] / "mmm" / "dashboard"
PAGES = [ROOT / "app.py"] + sorted((ROOT / "pages").glob("*.py"), key=lambda p: int(p.name.split("_")[0]))


@pytest.mark.parametrize("page", PAGES, ids=[p.stem for p in PAGES])
def test_page_renders(page, tmp_path, monkeypatch):
    monkeypatch.setenv("MMM_DATA_DIR", str(tmp_path))
    from mmm import settings

    settings.get_secrets.cache_clear()
    at = AppTest.from_file(str(page), default_timeout=180)
    at.run()
    assert not at.exception, [e.value for e in at.exception]


def test_backtest_page_runs_backtest(tmp_path, monkeypatch):
    monkeypatch.setenv("MMM_DATA_DIR", str(tmp_path))
    from mmm import settings

    settings.get_secrets.cache_clear()
    at = AppTest.from_file(str(ROOT / "pages" / "8_Backtesting.py"), default_timeout=600)
    at.run()
    at.button[0].click().run()
    assert not at.exception, [e.value for e in at.exception]
