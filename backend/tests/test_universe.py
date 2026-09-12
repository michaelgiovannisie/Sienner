from __future__ import annotations

import pytest

from sienner.universe import TickerRejected, validate_ticker


@pytest.mark.parametrize("raw,expected", [
    ("aapl", "AAPL"),
    ("  spy  ", "SPY"),
    ("BRK.B", "BRK.B"),
    ("F", "F"),
    ("GOOGL", "GOOGL"),
])
def test_accepts_us_listed_shapes(raw, expected):
    assert validate_ticker(raw) == expected


@pytest.mark.parametrize("raw,reason_fragment", [
    ("BTCUSD", "24/7"),
    ("ETHUSD", "24/7"),
    ("RY.TO", "per-exchange calendar"),
    ("VOD.L", "per-exchange calendar"),
    ("VFIAX", "forward-priced NAV"),
])
def test_rejects_out_of_universe_with_the_reason(raw, reason_fragment):
    with pytest.raises(TickerRejected) as excinfo:
        validate_ticker(raw)
    assert raw in str(excinfo.value)
    assert reason_fragment in str(excinfo.value)


@pytest.mark.parametrize("raw", ["", "   ", "TOOLONGTICKER", "A B", "123", "AA$"])
def test_rejects_malformed(raw):
    with pytest.raises(TickerRejected):
        validate_ticker(raw)
