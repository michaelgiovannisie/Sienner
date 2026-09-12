from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from sienner.providers.base import RAW_BAR_COLUMNS, empty_raw_bars

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture():
    def _load(name: str):
        return json.loads((FIXTURES / name).read_text())

    return _load


def make_bars(closes: list[float], *, start: date = date(2024, 1, 2)) -> pd.DataFrame:
    """Consecutive-calendar-day bars with the given closes.

    OHLC is derived from the close so the frame satisfies the hard-reject rules;
    only `close` matters to the return series.
    """
    if not closes:
        return empty_raw_bars()

    rows = []
    for offset, close in enumerate(closes):
        rows.append(
            {
                "bar_date": date.fromordinal(start.toordinal() + offset),
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1_000_000,
            }
        )
    return pd.DataFrame(rows).loc[:, list(RAW_BAR_COLUMNS)]


@pytest.fixture
def bars_factory():
    return make_bars
