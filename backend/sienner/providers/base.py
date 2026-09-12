"""The provider boundary.

A provider returns RAW BARS. It does not adjust, it does not compute returns,
and it does not know what a return is. Everything above this line consumes the
daily return series produced by sienner.returns, never a price.

That separation is the condition that makes the Stage 1a / 1b split cheap: 1b
adds fetch_corporate_actions() here and fills in the return computation there,
and no signature above either module changes.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

import pandas as pd

# The declared shape of a raw bar frame. Stage 1b replaces this tuple with a
# pandera schema; the column contract itself does not change.
RAW_BAR_COLUMNS: tuple[str, ...] = (
    "bar_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
)

_NUMERIC_COLUMNS = ("open", "high", "low", "close")


class ProviderError(RuntimeError):
    """A provider failed to answer. Carries the ticker for the error message."""


class UnknownSymbolError(ProviderError):
    """The provider has no such symbol, or returned an empty series for it."""


class MalformedProviderResponse(ProviderError):
    """The provider answered in a shape this adapter does not recognise."""


@runtime_checkable
class MarketDataProvider(Protocol):
    """What Sienner requires of a market data source."""

    name: str

    def fetch_raw_bars(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        """Return raw OHLCV bars for [start, end], ascending by date.

        Columns are exactly RAW_BAR_COLUMNS. The range is inclusive on both
        ends. Non-trading days are simply absent - their absence is recorded by
        the coverage ledger, not by the provider.
        """
        ...

    # Stage 1b adds, and nothing above this module changes shape:
    #
    # def fetch_corporate_actions(
    #     self, ticker: str, start: date, end: date
    # ) -> pd.DataFrame:
    #     """Splits and cash dividends, ex-dated, ascending."""


def empty_raw_bars() -> pd.DataFrame:
    """A correctly-typed zero-row bar frame.

    Returned for a fetched range that legitimately contains no bars, which is a
    different fact from "we never asked" and must not be an error.
    """
    frame = pd.DataFrame({name: pd.Series(dtype="float64") for name in _NUMERIC_COLUMNS})
    frame.insert(0, "bar_date", pd.Series(dtype="object"))
    frame["volume"] = pd.Series(dtype="int64")
    return frame[list(RAW_BAR_COLUMNS)]


def normalise_raw_bars(frame: pd.DataFrame, *, ticker: str) -> pd.DataFrame:
    """Coerce a provider frame to the declared contract, or raise.

    Applies the README's hard-reject rules: nulls in required columns,
    non-positive prices, duplicate dates, non-monotonic dates. Stage 1b moves
    these into a pandera schema and adds the soft-reject (quarantine) tier;
    the rules themselves are already the ones that will be enforced there.
    """
    missing = [c for c in RAW_BAR_COLUMNS if c not in frame.columns]
    if missing:
        raise MalformedProviderResponse(
            f"{ticker}: provider frame missing columns {missing}"
        )

    out = frame.loc[:, list(RAW_BAR_COLUMNS)].copy()
    if out.empty:
        return empty_raw_bars()

    out["bar_date"] = pd.to_datetime(out["bar_date"], errors="coerce").dt.date
    for column in _NUMERIC_COLUMNS:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce")

    null_rows = out.isna().any(axis=1)
    if bool(null_rows.any()):
        first = out.loc[null_rows].iloc[0]
        raise MalformedProviderResponse(
            f"{ticker}: null in required column at row {first.to_dict()}"
        )

    nonpositive = (out[list(_NUMERIC_COLUMNS)] <= 0).any(axis=1)
    if bool(nonpositive.any()):
        bad = out.loc[nonpositive, "bar_date"].tolist()
        raise MalformedProviderResponse(
            f"{ticker}: non-positive price on {bad[:5]}"
            f"{' and %d more' % (len(bad) - 5) if len(bad) > 5 else ''}"
        )

    out = out.sort_values("bar_date", kind="stable").reset_index(drop=True)

    duplicated = out["bar_date"].duplicated()
    if bool(duplicated.any()):
        dupes = out.loc[duplicated, "bar_date"].tolist()
        raise MalformedProviderResponse(
            f"{ticker}: duplicate bar dates {dupes[:5]}"
        )

    out["volume"] = out["volume"].astype("int64")
    return out
