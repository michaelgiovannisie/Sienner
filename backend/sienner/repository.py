"""Persistence for raw bars and the coverage ledger.

Plain SQL over psycopg. No ORM: there are two tables in Stage 1a, the queries
are the interesting part, and an ORM would hide the ON CONFLICT DO NOTHING that
makes ingestion idempotent.

BarRepository is a Protocol so the service can be exercised against the
in-memory implementation in tests without a live Postgres.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

import pandas as pd

from sienner.coverage import DateRange, merge_ranges
from sienner.providers.base import RAW_BAR_COLUMNS, empty_raw_bars


class BarRepository(Protocol):
    def covered_ranges(self, ticker: str) -> list[DateRange]:
        """Ranges already fetched for this ticker, merged."""
        ...

    def record_coverage(
        self, ticker: str, span: DateRange, *, source: str, bars_written: int
    ) -> None:
        ...

    def write_bars(self, ticker: str, bars: pd.DataFrame, *, source: str) -> int:
        """Insert bars, ignoring any that already exist. Returns rows written."""
        ...

    def read_bars(self, ticker: str, span: DateRange) -> pd.DataFrame:
        """Bars within the closed interval, ascending by date."""
        ...


class InMemoryBarRepository:
    """Reference implementation. Used by tests; also the fallback when no
    DATABASE_URL is reachable, so the API can be exercised without Postgres."""

    def __init__(self) -> None:
        self._bars: dict[str, dict[date, dict]] = {}
        self._coverage: dict[str, list[DateRange]] = {}

    def covered_ranges(self, ticker: str) -> list[DateRange]:
        return merge_ranges(list(self._coverage.get(ticker, [])))

    def record_coverage(
        self, ticker: str, span: DateRange, *, source: str, bars_written: int
    ) -> None:
        self._coverage.setdefault(ticker, []).append(span)

    def write_bars(self, ticker: str, bars: pd.DataFrame, *, source: str) -> int:
        store = self._bars.setdefault(ticker, {})
        written = 0
        for row in bars.to_dict("records"):
            key = row["bar_date"]
            if key in store:
                continue  # immutable: first write wins
            store[key] = row
            written += 1
        return written

    def read_bars(self, ticker: str, span: DateRange) -> pd.DataFrame:
        store = self._bars.get(ticker, {})
        rows = [
            row
            for key, row in sorted(store.items())
            if span.start <= key <= span.end
        ]
        if not rows:
            return empty_raw_bars()
        return pd.DataFrame(rows).loc[:, list(RAW_BAR_COLUMNS)]


class PostgresBarRepository:
    """psycopg-backed implementation against the Stage 1a schema."""

    def __init__(self, pool) -> None:
        self._pool = pool

    def covered_ranges(self, ticker: str) -> list[DateRange]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT range_start, range_end
                  FROM fetch_coverage
                 WHERE ticker = %s
                 ORDER BY range_start
                """,
                (ticker,),
            ).fetchall()
        return merge_ranges([DateRange(start, end) for start, end in rows])

    def record_coverage(
        self, ticker: str, span: DateRange, *, source: str, bars_written: int
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO fetch_coverage
                       (ticker, range_start, range_end, source, bars_written)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (ticker, span.start, span.end, source, bars_written),
            )

    def write_bars(self, ticker: str, bars: pd.DataFrame, *, source: str) -> int:
        if bars.empty:
            return 0
        records = [
            (
                ticker,
                row["bar_date"],
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                int(row["volume"]),
                source,
            )
            for row in bars.to_dict("records")
        ]
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                # ON CONFLICT DO NOTHING is what makes a re-fetch a no-op rather
                # than a rewrite. daily_bars also has an UPDATE-blocking trigger,
                # so DO UPDATE here would fail loudly - by design.
                cur.executemany(
                    """
                    INSERT INTO daily_bars
                           (ticker, bar_date, open, high, low, close, volume, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ticker, bar_date) DO NOTHING
                    """,
                    records,
                )
                return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def read_bars(self, ticker: str, span: DateRange) -> pd.DataFrame:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT bar_date, open, high, low, close, volume
                  FROM daily_bars
                 WHERE ticker = %s AND bar_date BETWEEN %s AND %s
                 ORDER BY bar_date
                """,
                (ticker, span.start, span.end),
            ).fetchall()
        if not rows:
            return empty_raw_bars()
        frame = pd.DataFrame(rows, columns=list(RAW_BAR_COLUMNS))
        for column in ("open", "high", "low", "close"):
            frame[column] = frame[column].astype("float64")
        frame["volume"] = frame["volume"].astype("int64")
        return frame
