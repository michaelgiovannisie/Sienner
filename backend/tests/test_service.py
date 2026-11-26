"""Cache behaviour. The README's claim is that the tool works fully offline for
any ticker-range already cached, and that a backfill is idempotent rather than a
rewrite. These are the tests that hold it to that."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from sienner.config import Settings
from sienner.coverage import DateRange
from sienner.providers.base import (
    ProviderError,
    empty_corporate_actions,
    empty_raw_bars,
)
from sienner.repository import InMemoryRepository
from sienner.service import NoDataAvailable, SeriesService
from sienner.universe import TickerRejected
from tests.conftest import make_bars


class RecordingProvider:
    """Counts calls and records the ranges asked for."""

    name = "recording"

    def __init__(
        self,
        bars: pd.DataFrame | None = None,
        fail: bool = False,
        actions: pd.DataFrame | None = None,
    ) -> None:
        self._bars = empty_raw_bars() if bars is None else bars
        self._actions = empty_corporate_actions() if actions is None else actions
        self.calls: list[DateRange] = []
        self.action_calls: list[DateRange] = []
        self.fail = fail

    def fetch_raw_bars(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        self.calls.append(DateRange(start, end))
        if self.fail:
            raise ProviderError("provider is down")
        frame = self._bars
        mask = (frame["bar_date"] >= start) & (frame["bar_date"] <= end)
        return frame.loc[mask].reset_index(drop=True)

    def fetch_corporate_actions(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        self.action_calls.append(DateRange(start, end))
        if self.fail:
            raise ProviderError("provider is down")
        frame = self._actions
        if frame.empty:
            return frame
        mask = (frame["ex_date"] >= start) & (frame["ex_date"] <= end)
        return frame.loc[mask].reset_index(drop=True)


@pytest.fixture
def settings() -> Settings:
    return Settings(fmp_api_key="test-key")


def build(bars: pd.DataFrame | None = None, *, fail: bool = False, settings=None, actions=None):
    provider = RecordingProvider(bars, fail=fail, actions=actions)
    repository = InMemoryRepository()
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
    assert result.basis == "total_return"


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


# --- Stage 1b: actions, quarantine, and the spike gate ---------------------


def _split_action(day: date, ratio: float) -> pd.DataFrame:
    return pd.DataFrame(
        [{"ex_date": day, "action_type": "split", "split_ratio": ratio,
          "cash_dividend": None, "provider_adj_dividend": None}]
    )


def test_actions_are_fetched_and_stored_alongside_bars(settings):
    bars = make_bars([100.0, 110.0, 99.0])
    actions = _split_action(date(2024, 1, 3), 2.0)
    provider, repository, service = build(bars, settings=settings, actions=actions)

    service.get_series("AAPL", date(2024, 1, 2), date(2024, 1, 4))

    assert provider.action_calls, "actions must be fetched in the same pass as bars"
    stored = repository.read_actions("AAPL", DateRange(date(2024, 1, 2), date(2024, 1, 4)))
    assert len(stored) == 1
    assert float(stored["split_ratio"].iloc[0]) == 2.0


def test_unresolved_convention_keeps_the_tool_working_and_says_why():
    """The fallback, which outlives the spike: if the convention is ever reset,
    the deployed tool must keep answering as a price return and must not imply
    it is doing anything else."""
    bars = make_bars([100.0, 110.0, 99.0])
    actions = _split_action(date(2024, 1, 3), 2.0)
    unresolved = Settings(fmp_api_key="test-key", dividend_unit_convention="unresolved")
    _, _, service = build(bars, settings=unresolved, actions=actions)

    result = service.get_series("AAPL", date(2024, 1, 2), date(2024, 1, 4))

    assert result.basis == "price_return"
    assert any("NOT applied" in w for w in result.warnings)
    assert any("spike" in w for w in result.warnings)


def test_resolved_convention_produces_a_total_return(settings):
    """The default now the spike is answered: actions are applied, the basis
    says total_return, and no warning hedges it."""
    bars = make_bars([100.0, 110.0, 99.0])
    actions = _split_action(date(2024, 1, 3), 2.0)
    _, _, service = build(bars, settings=settings, actions=actions)

    result = service.get_series("AAPL", date(2024, 1, 2), date(2024, 1, 4))

    assert result.basis == "total_return"
    assert "ex-date" in result.disclaimer, "the reinvestment assumption is stated"
    assert not any("NOT applied" in w for w in result.warnings)


def test_quarantined_bars_are_surfaced_and_stored(settings):
    """A 3:2-shaped drop with no action on file. Included in the series,
    flagged in the response, recorded in storage."""
    bars = make_bars([300.0, 200.0, 201.0])
    _, repository, service = build(bars, settings=settings)

    result = service.get_series("AAPL", date(2024, 1, 2), date(2024, 1, 4))

    rules = {flag.rule for flag in result.quarantined}
    assert "split_shaped_move" in rules
    assert len(result.points) == 3, "a quarantined bar is flagged, not dropped"

    stored = repository.read_quarantine(
        "AAPL", DateRange(date(2024, 1, 2), date(2024, 1, 4))
    )
    assert len(stored) >= 1


def test_re_running_does_not_duplicate_quarantine_rows(settings):
    bars = make_bars([300.0, 200.0, 201.0])
    _, repository, service = build(bars, settings=settings)
    window = (date(2024, 1, 2), date(2024, 1, 4))

    service.get_series("AAPL", *window)
    first = len(repository.read_quarantine("AAPL", DateRange(*window)))
    service.get_series("AAPL", *window)
    second = len(repository.read_quarantine("AAPL", DateRange(*window)))

    assert first == second
