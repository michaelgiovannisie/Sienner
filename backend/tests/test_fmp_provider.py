"""Adapter tests. These prove the adapter handles a response SHAPE. They are
not evidence about FMP's field semantics - see tests/fixtures/README.md."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from sienner.config import Settings
from sienner.providers.base import RAW_BAR_COLUMNS, MalformedProviderResponse
from sienner.providers.fmp import FMPProvider
from sienner.providers.base import ProviderError, UnknownSymbolError


def make_provider(handler, *, key: str = "test-key") -> FMPProvider:
    transport = httpx.MockTransport(handler)
    settings = Settings(fmp_api_key=key)
    return FMPProvider(settings=settings, client=httpx.Client(transport=transport))


def json_handler(payload, status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handler


def test_parses_the_historical_envelope(load_fixture):
    provider = make_provider(json_handler(load_fixture("fmp_historical_envelope.json")))
    bars = provider.fetch_raw_bars("AAPL", date(2024, 1, 1), date(2024, 1, 8))

    assert list(bars.columns) == list(RAW_BAR_COLUMNS)
    assert len(bars) == 5
    assert bars["bar_date"].iloc[0] == date(2024, 1, 2)
    assert bars["close"].iloc[0] == pytest.approx(185.64)


def test_parses_the_bare_list_envelope(load_fixture):
    provider = make_provider(json_handler(load_fixture("fmp_bare_list_envelope.json")))
    bars = provider.fetch_raw_bars("AAPL", date(2024, 1, 1), date(2024, 1, 3))

    assert len(bars) == 2
    assert bars["bar_date"].tolist() == [date(2024, 1, 2), date(2024, 1, 3)]


def test_newest_first_input_comes_back_ascending(load_fixture):
    """FMP answers newest-first. Everything downstream assumes ascending."""
    provider = make_provider(json_handler(load_fixture("fmp_historical_envelope.json")))
    bars = provider.fetch_raw_bars("AAPL", date(2024, 1, 1), date(2024, 1, 8))

    assert bars["bar_date"].is_monotonic_increasing


def test_adjclose_is_not_consumed(load_fixture):
    """Stage 1a derives returns from the raw close. adjClose is a Stage 1b
    reconciliation input, never a source of truth."""
    provider = make_provider(json_handler(load_fixture("fmp_historical_envelope.json")))
    bars = provider.fetch_raw_bars("AAPL", date(2024, 1, 1), date(2024, 1, 8))

    assert "adjClose" not in bars.columns
    assert "adj_close" not in bars.columns


def test_empty_window_is_not_an_error():
    """A holiday stretch or a pre-listing range legitimately has no bars. The
    coverage ledger records that we asked; an exception here would make that
    impossible to distinguish from a failure."""
    provider = make_provider(json_handler({"symbol": "AAPL", "historical": []}))
    bars = provider.fetch_raw_bars("AAPL", date(2024, 12, 25), date(2024, 12, 25))

    assert bars.empty
    assert list(bars.columns) == list(RAW_BAR_COLUMNS)


def test_fmp_error_payload_is_surfaced():
    provider = make_provider(json_handler({"Error Message": "Invalid API KEY."}))
    with pytest.raises(ProviderError, match="Invalid API KEY"):
        provider.fetch_raw_bars("AAPL", date(2024, 1, 1), date(2024, 1, 8))


def test_404_is_an_unknown_symbol():
    provider = make_provider(json_handler({}, status_code=404))
    with pytest.raises(UnknownSymbolError):
        provider.fetch_raw_bars("NOPE", date(2024, 1, 1), date(2024, 1, 8))


def test_rate_limit_points_at_the_open_question():
    provider = make_provider(json_handler({}, status_code=429))
    with pytest.raises(ProviderError, match="rate limit"):
        provider.fetch_raw_bars("AAPL", date(2024, 1, 1), date(2024, 1, 8))


def test_missing_fields_are_rejected_by_name():
    provider = make_provider(json_handler([{"date": "2024-01-02", "close": 185.64}]))
    with pytest.raises(MalformedProviderResponse, match="missing fields"):
        provider.fetch_raw_bars("AAPL", date(2024, 1, 1), date(2024, 1, 8))


def test_non_positive_price_is_a_hard_reject():
    payload = [
        {"date": "2024-01-02", "open": 10.0, "high": 10.0, "low": 10.0, "close": 0.0, "volume": 1},
    ]
    provider = make_provider(json_handler(payload))
    with pytest.raises(MalformedProviderResponse, match="non-positive price"):
        provider.fetch_raw_bars("AAPL", date(2024, 1, 1), date(2024, 1, 8))


def test_duplicate_dates_are_a_hard_reject():
    row = {"date": "2024-01-02", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1}
    provider = make_provider(json_handler([row, dict(row)]))
    with pytest.raises(MalformedProviderResponse, match="duplicate bar dates"):
        provider.fetch_raw_bars("AAPL", date(2024, 1, 1), date(2024, 1, 8))


def test_missing_api_key_fails_before_the_network():
    provider = make_provider(json_handler([]), key="")
    with pytest.raises(ProviderError, match="FMP_API_KEY"):
        provider.fetch_raw_bars("AAPL", date(2024, 1, 1), date(2024, 1, 8))
