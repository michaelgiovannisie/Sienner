"""Cache behaviour. The README's claim is that the tool works fully offline for
any ticker-range already cached, and that a backfill is idempotent rather than a
rewrite. These are the tests that hold it to that."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from sienner.config import Settings
from sienner.coverage import DateRange
from sienner.providers.base import ProviderError, empty_raw_bars
from sienner.repository import InMemoryBarRepository
from sienner.service import NoDataAvailable, SeriesService
from sienner.universe import TickerRejected
from tests.conftest import make_bars


class RecordingProvider:
    """Counts calls and records the ranges asked for."""

    name = "recording"

    def __init__(self, bars: pd.DataFrame | None = None, fail: bool = False) -> None:
        self._bars = empty_raw_bars() if bars is None else bars
        self.calls: list[DateRange] = []
        self.fail = fail

    def fetch_raw_bars(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        self.calls.append(DateRange(start, end))
        if self.fail:
            raise ProviderError("provider is down")
        frame = self._bars
        mask = (frame["bar_date"] >= start) & (frame["bar_date"] <= end)
        return frame.loc[mask].reset_index(drop=True)


@pytest.fixture
def settings() -> Settings:
    return Settings(fmp_api_key="test-key")


def build(bars: pd.DataFrame | None = None, *, fail: bool = False, settings=None):
    provider = RecordingProvider(bars, fail=fail)
    repository = InMemoryBarRepository()
    return provider, repository, SeriesService(provider, repository, settings)


def test_first_request_fetches_and_second_does_not(settings):
    bars = make_bars([100.0, 110.0, 99.0])
    provider, _, service = build(bars, settings=settings)
    window = (date(2024, 1, 2), date(2024, 1, 4))

    first = service.get_series("AAPL", *window)
    assert len(provider.calls) == 1
    assert first.fetched_ranges == [DateRange(*window)]

    second = service.get_series("AAPL", *window)
    assert len(provider.calls) == 1, "a cached range must issue no provider call"
    assert second.fetched_ranges == []
    assert [p.value for p in second.points] == [p.value for p in first.points]


def test_widening_the_window_fetches_only_the_new_edges(settings):
    bars = make_bars([100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0])
    provider, _, service = build(bars, settings=settings)

    service.get_series("AAPL", date(2024, 1, 4), date(2024, 1, 6))
    provider.calls.clear()

    service.get_series("AAPL", date(2024, 1, 2), date(2024, 1, 8))

    assert provider.calls == [
        DateRange(date(2024, 1, 2), date(2024, 1, 3)),
        DateRange(date(2024, 1, 7), date(2024, 1, 8)),
    ]


def test_a_fetched_range_with_no_bars_is_not_re_fetched(settings):
    """Market holiday, or before listing. 'we asked and there was nothing' must
    not look like 'we never asked'."""
    bars = make_bars([100.0, 110.0])
    provider, _, service = build(bars, settings=settings)

    service.get_series("AAPL", date(2024, 1, 2), date(2024, 1, 3))
    provider.calls.clear()

    # A range with no bars in it at all, requested twice.
    with pytest.raises(NoDataAvailable):
        service.get_series("AAPL", date(2024, 2, 1), date(2024, 2, 5))
    assert len(provider.calls) == 1

    with pytest.raises(NoDataAvailable):
        service.get_series("AAPL", date(2024, 2, 1), date(2024, 2, 5))
    assert len(provider.calls) == 1, "coverage must be recorded even for an empty fetch"


def test_re_ingesting_the_same_bars_writes_nothing_new(settings):
    bars = make_bars([100.0, 110.0, 99.0])
    _, repository, service = build(bars, settings=settings)

    service.get_series("AAPL", date(2024, 1, 2), date(2024, 1, 4))
    written_again = repository.write_bars("AAPL", bars, source="recording")

    assert written_again == 0, "bars are immutable: a re-write is a no-op"


def test_provider_failure_on_a_cached_range_still_answers(settings):
    bars = make_bars([100.0, 110.0, 99.0])
    provider, repository, service = build(bars, settings=settings)
    service.get_series("AAPL", date(2024, 1, 2), date(2024, 1, 4))

    provider.fail = True
    result = service.get_series("AAPL", date(2024, 1, 1), date(2024, 1, 5))

    assert result.total_return == pytest.approx(-0.01)
    assert any("provider is down" in w for w in result.warnings)


def test_provider_failure_with_no_cache_is_fatal(settings):
    _, _, service = build(make_bars([100.0]), fail=True, settings=settings)

    with pytest.raises(NoDataAvailable):
        service.get_series("AAPL", date(2024, 1, 2), date(2024, 1, 4))


def test_short_history_is_reported_not_silently_shortened(settings):
    """No silent shortening. Stage 1c generalises this to the shortest common
    history across several tickers; the reporting channel is the same."""
    bars = make_bars([100.0, 110.0], start=date(2024, 3, 1))
    _, _, service = build(bars, settings=settings)

    result = service.get_series("AAPL", date(2024, 1, 1), date(2024, 3, 31))

    assert result.requested == DateRange(date(2024, 1, 1), date(2024, 3, 31))
    assert result.effective.start == date(2024, 3, 1)
    assert any("ran from 2024-03-01" in w for w in result.warnings)


def test_curve_is_indexed_to_base_at_the_effective_start(settings):
    bars = make_bars([100.0, 110.0, 99.0])
    _, _, service = build(bars, settings=settings)

    result = service.get_series("AAPL", date(2024, 1, 2), date(2024, 1, 4))

    assert result.points[0].value == pytest.approx(100.0)
    assert result.points[0].bar_date == result.effective.start
    assert result.basis == "price_return"


def test_out_of_universe_ticker_is_rejected_before_any_fetch(settings):
    provider, _, service = build(make_bars([100.0]), settings=settings)

    with pytest.raises(TickerRejected):
        service.get_series("BTCUSD", date(2024, 1, 2), date(2024, 1, 4))
    assert provider.calls == []


def test_inverted_window_is_rejected(settings):
    _, _, service = build(make_bars([100.0]), settings=settings)

    with pytest.raises(ValueError):
        service.get_series("AAPL", date(2024, 3, 1), date(2024, 1, 1))


def test_empty_result_explains_why_rather_than_guessing(settings):
    """'No API key' and 'this range precedes the listing' must not produce the
    same message."""
    _, _, service = build(make_bars([100.0]), fail=True, settings=settings)

    with pytest.raises(NoDataAvailable, match="provider is down"):
        service.get_series("AAPL", date(2024, 1, 2), date(2024, 1, 4))
