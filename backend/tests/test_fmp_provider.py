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


# --- route generation ------------------------------------------------------
#
# FMP retired the /api/v3 and /api/v4 routes (403 "Legacy Endpoint") in favour
# of /stable/, which takes the symbol as a query parameter instead of a path
# segment. These tests pin both shapes so the next generation change is caught
# by the suite rather than by an empty result file.


def capturing_handler(payload):
    """Records the request so the test can assert on the URL that was built."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=payload)

    return handler, seen


def test_stable_generation_puts_the_symbol_in_the_query(load_fixture):
    handler, seen = capturing_handler(load_fixture("fmp_bare_list_envelope.json"))
    settings = Settings(
        fmp_api_key="k",
        fmp_historical_path="/stable/historical-price-eod/full",
        fmp_symbol_in_query=True,
    )
    provider = FMPProvider(settings=settings, client=httpx.Client(transport=httpx.MockTransport(handler)))

    provider.fetch_raw_bars("aapl", date(2024, 1, 1), date(2024, 1, 3))

    assert seen["path"] == "/stable/historical-price-eod/full"
    assert seen["params"]["symbol"] == "AAPL", "symbol is upper-cased into the query"
    assert seen["params"]["from"] == "2024-01-01"
    assert seen["params"]["to"] == "2024-01-03"


def test_legacy_generation_puts_the_symbol_in_the_path(load_fixture):
    handler, seen = capturing_handler(load_fixture("fmp_historical_envelope.json"))
    settings = Settings(
        fmp_api_key="k",
        fmp_historical_path="/api/v3/historical-price-full",
        fmp_symbol_in_query=False,
    )
    provider = FMPProvider(settings=settings, client=httpx.Client(transport=httpx.MockTransport(handler)))

    provider.fetch_raw_bars("aapl", date(2024, 1, 1), date(2024, 1, 8))

    assert seen["path"] == "/api/v3/historical-price-full/AAPL"
    assert "symbol" not in seen["params"]


def test_the_api_key_never_lands_in_the_path():
    """It belongs in the query string. A key in a path ends up in access logs
    and in any error message that echoes the URL."""
    handler, seen = capturing_handler([])
    settings = Settings(fmp_api_key="secret-key", fmp_symbol_in_query=True)
    provider = FMPProvider(settings=settings, client=httpx.Client(transport=httpx.MockTransport(handler)))

    provider.fetch_raw_bars("AAPL", date(2024, 1, 1), date(2024, 1, 3))

    assert "secret-key" not in seen["path"]
    assert seen["params"]["apikey"] == "secret-key"


def routing_handler(by_path: dict[str, object]):
    """Dispatch by path, because fetch_corporate_actions calls TWO routes.

    A single-payload mock feeds the dividend body to the split parser, which
    correctly rejects it - so a shared handler tests the mock, not the code.
    """
    seen: dict[str, list] = {"paths": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["paths"].append(request.url.path)
        for fragment, payload in by_path.items():
            if fragment in request.url.path:
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json=[])

    return handler, seen


def test_stable_splits_route_parses(load_fixture):
    """The shape /stable/splits returns: a bare list with numerator and
    denominator, plus a splitType field Sienner does not consume."""
    handler, seen = routing_handler({
        "/splits": [
            {"symbol": "AAPL", "date": "2020-08-31", "numerator": 4, "denominator": 1, "splitType": "forward"},
            {"symbol": "AAPL", "date": "2014-06-09", "numerator": 7, "denominator": 1, "splitType": "forward"},
        ],
        "/dividends": [],
    })
    settings = Settings(fmp_api_key="k", fmp_symbol_in_query=True)
    provider = FMPProvider(settings=settings, client=httpx.Client(transport=httpx.MockTransport(handler)))

    actions = provider.fetch_corporate_actions("AAPL", date(2014, 1, 1), date(2021, 1, 1))
    splits = actions[actions["action_type"] == "split"]

    assert any("/splits" in path for path in seen["paths"])
    assert any("/dividends" in path for path in seen["paths"]), "both routes are called"
    assert len(splits) == 2
    assert set(splits["split_ratio"]) == {4.0, 7.0}
    assert "splitType" not in actions.columns, "a field Sienner does not consume"


def test_stable_dividends_route_carries_adj_dividend_without_consuming_it():
    """The /stable/dividends shape: date (ex), declarationDate, recordDate,
    paymentDate, dividend, adjDividend, frequency. adjDividend is stored for
    the spike's cross-check and never used to compute a return."""
    handler, _ = routing_handler({
        "/splits": [],
        "/dividends": [
            {
                "symbol": "AAPL", "date": "2020-08-07", "recordDate": "2020-08-10",
                "paymentDate": "2020-08-13", "declarationDate": "2020-07-30",
                "adjDividend": 0.205, "dividend": 0.82, "frequency": "Quarterly",
            }
        ],
    })
    settings = Settings(fmp_api_key="k", fmp_symbol_in_query=True)
    provider = FMPProvider(settings=settings, client=httpx.Client(transport=httpx.MockTransport(handler)))

    actions = provider.fetch_corporate_actions("AAPL", date(2020, 1, 1), date(2020, 12, 31))
    dividends = actions[actions["action_type"] == "cash_dividend"]

    assert len(dividends) == 1
    row = dividends.iloc[0]
    assert row["ex_date"] == date(2020, 8, 7), "`date` is treated as the ex-date"
    assert float(row["cash_dividend"]) == 0.82, "the as-declared amount"
    assert float(row["provider_adj_dividend"]) == 0.205, "carried, not consumed"
