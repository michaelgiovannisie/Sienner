"""Orchestration: cache check, conditional fetch, return series, curve.

The only module that knows both a provider and a repository exist. Deliberately
free of FastAPI imports - this is the seam a Prefect or Airflow wrapper attaches
to later, per the README's orchestration note.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sienner.config import Settings, get_settings
from sienner.coverage import DateRange, missing_ranges
from sienner.providers.base import MarketDataProvider
from sienner.repository import BarRepository
from sienner.returns import (
    BASIS_DISCLAIMER,
    BASIS_PRICE_RETURN,
    equity_curve,
    to_daily_returns,
    total_return,
)
from sienner.universe import validate_ticker


class NoDataAvailable(ValueError):
    """The provider and cache between them have no bars in the window."""


@dataclass(frozen=True)
class SeriesPoint:
    bar_date: date
    value: float


@dataclass(frozen=True)
class SeriesResult:
    ticker: str
    requested: DateRange
    effective: DateRange
    basis: str
    disclaimer: str
    points: list[SeriesPoint]
    total_return: float
    fetched_ranges: list[DateRange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class SeriesService:
    def __init__(
        self,
        provider: MarketDataProvider,
        repository: BarRepository,
        settings: Settings | None = None,
    ) -> None:
        self._provider = provider
        self._repository = repository
        self._settings = settings or get_settings()

    def get_series(self, raw_ticker: str, start: date, end: date) -> SeriesResult:
        ticker = validate_ticker(raw_ticker)
        if end < start:
            raise ValueError(f"end {end} precedes start {start}")
        requested = DateRange(start, end)

        warnings: list[str] = []
        fetched = self._ensure_cached(ticker, requested, warnings)

        bars = self._repository.read_bars(ticker, requested)
        if bars.empty:
            # Report why. Without the warnings, "no API key" and "this range
            # precedes the listing" produce the same message, and the first one
            # is a five-second fix presented as a data problem.
            detail = " ".join(warnings) if warnings else "The range may precede the listing."
            raise NoDataAvailable(f"{ticker}: no bars between {start} and {end}. {detail}")

        effective = DateRange(bars["bar_date"].iloc[0], bars["bar_date"].iloc[-1])
        if effective.start > requested.start:
            # No silent shortening. Stage 1c generalises this to the shortest
            # common history across several tickers; the reporting channel is
            # the same field.
            warnings.append(
                f"Requested {requested.start}, ran from {effective.start} "
                f"(earliest bar available for {ticker})."
            )

        returns = to_daily_returns(bars)  # actions omitted: Stage 1a stub
        curve = equity_curve(
            returns,
            start_date=effective.start,
            base=self._settings.equity_curve_base,
        )

        return SeriesResult(
            ticker=ticker,
            requested=requested,
            effective=effective,
            basis=BASIS_PRICE_RETURN,
            disclaimer=BASIS_DISCLAIMER[BASIS_PRICE_RETURN],
            points=[
                SeriesPoint(bar_date=row["bar_date"], value=float(row["value"]))
                for row in curve.to_dict("records")
            ],
            total_return=total_return(curve, base=self._settings.equity_curve_base),
            fetched_ranges=fetched,
            warnings=warnings,
        )

    def _ensure_cached(
        self, ticker: str, requested: DateRange, warnings: list[str]
    ) -> list[DateRange]:
        """Fetch only what the coverage ledger says we have never asked for.

        A fully-cached range issues zero provider calls, which is what makes the
        tool work offline. A provider failure on a partially-cached range is a
        warning, not a fatal error: the cached part is still answerable.
        """
        gaps = missing_ranges(requested, self._repository.covered_ranges(ticker))
        fetched: list[DateRange] = []

        for gap in gaps:
            try:
                bars = self._provider.fetch_raw_bars(ticker, gap.start, gap.end)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                warnings.append(
                    f"Provider failed for {gap.start}..{gap.end}: {exc}. "
                    "Answering from cache only."
                )
                continue

            written = self._repository.write_bars(
                ticker, bars, source=self._provider.name
            )
            # Coverage is recorded even when the fetch returned no bars. That is
            # the whole point of the ledger: "fetched, no bar exists" (holiday,
            # or pre-listing) must not look like "never asked".
            self._repository.record_coverage(
                ticker, gap, source=self._provider.name, bars_written=written
            )
            fetched.append(gap)

        return fetched
