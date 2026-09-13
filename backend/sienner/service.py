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
from sienner.providers.base import MarketDataProvider, empty_corporate_actions
from sienner.repository import Repository
from sienner.returns import (
    BASIS_DISCLAIMER,
    BASIS_PRICE_RETURN,
    BASIS_TOTAL_RETURN,
    equity_curve,
    to_daily_returns,
    total_return,
)
from sienner.schemas import RAW_BARS_SCHEMA, RETURN_SERIES_SCHEMA, validate
from sienner.universe import validate_ticker
from sienner import validation


class NoDataAvailable(ValueError):
    """The provider and cache between them have no bars in the window."""


@dataclass(frozen=True)
class SeriesPoint:
    bar_date: date
    value: float


@dataclass(frozen=True)
class QuarantinedBar:
    bar_date: date
    rule: str
    detail: str


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
    quarantined: list[QuarantinedBar] = field(default_factory=list)


class SeriesService:
    def __init__(
        self,
        provider: MarketDataProvider,
        repository: Repository,
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

        # Tier one, on the way out of storage as well as on the way in: a cached
        # read is a process boundary too.
        bars = validate(bars, RAW_BARS_SCHEMA, ticker=ticker)

        effective = DateRange(bars["bar_date"].iloc[0], bars["bar_date"].iloc[-1])
        if effective.start > requested.start:
            # No silent shortening. Stage 1c generalises this to the shortest
            # common history across several tickers; the reporting channel is
            # the same field.
            warnings.append(
                f"Requested {requested.start}, ran from {effective.start} "
                f"(earliest bar available for {ticker})."
            )

        actions = self._repository.read_actions(ticker, effective)
        basis, returns = self._compute_returns(bars, actions, warnings=warnings)
        returns = validate(returns, RETURN_SERIES_SCHEMA, ticker=ticker)

        # Tier two. Quarantined rows are stored, surfaced, and never dropped.
        flags = validation.detect(
            returns, ticker=ticker, actions=actions, settings=self._settings
        )
        if flags:
            self._repository.write_quarantine(validation.to_frame(flags))

        curve = equity_curve(
            returns,
            start_date=effective.start,
            base=self._settings.equity_curve_base,
        )

        return SeriesResult(
            ticker=ticker,
            requested=requested,
            effective=effective,
            basis=basis,
            disclaimer=BASIS_DISCLAIMER[basis],
            points=[
                SeriesPoint(bar_date=row["bar_date"], value=float(row["value"]))
                for row in curve.to_dict("records")
            ],
            total_return=total_return(curve, base=self._settings.equity_curve_base),
            fetched_ranges=fetched,
            warnings=warnings,
            quarantined=[
                QuarantinedBar(bar_date=flag.bar_date, rule=flag.rule, detail=flag.detail)
                for flag in flags
            ],
        )

    def _compute_returns(
        self,
        bars,
        actions,
        *,
        warnings: list[str],
    ):
        """Total return when the dividend unit convention is known, price return
        otherwise.

        This is the gate that keeps the deployed tool working while the spike is
        outstanding. It does not guess: it computes the answer both conventions
        agree on - the price return - and says plainly in the response why the
        total-return path is off.
        """
        if self._settings.dividend_unit_convention == "unresolved":
            if not actions.empty:
                warnings.append(
                    "Dividends and splits are on file but NOT applied: the "
                    "provider's dividend unit convention is unresolved, so this "
                    "is a price return. Run the FMP spike and set "
                    "SIENNER_DIVIDEND_UNIT_CONVENTION to enable total return."
                )
            return BASIS_PRICE_RETURN, to_daily_returns(bars, settings=self._settings)

        return BASIS_TOTAL_RETURN, to_daily_returns(
            bars, actions, settings=self._settings
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

            # Actions are fetched in the same pass and stored ex-dated. They are
            # stored whether or not the convention is resolved: the data is not
            # the blocker, the arithmetic is.
            try:
                actions = self._provider.fetch_corporate_actions(
                    ticker, gap.start, gap.end
                )
            except NotImplementedError:
                actions = empty_corporate_actions()
            except Exception as exc:  # noqa: BLE001
                actions = empty_corporate_actions()
                warnings.append(
                    f"Corporate actions unavailable for {gap.start}..{gap.end}: {exc}"
                )
            if not actions.empty:
                self._repository.write_actions(
                    ticker, actions, source=self._provider.name
                )
            # Coverage is recorded even when the fetch returned no bars. That is
            # the whole point of the ledger: "fetched, no bar exists" (holiday,
            # or pre-listing) must not look like "never asked".
            self._repository.record_coverage(
                ticker, gap, source=self._provider.name, bars_written=written
            )
            fetched.append(gap)

        return fetched
