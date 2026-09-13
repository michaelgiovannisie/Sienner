"""Persistence for raw bars, the coverage ledger, corporate actions and
quarantine.

Plain SQL over psycopg. No ORM: the queries are the interesting part, and an
ORM would hide both the ON CONFLICT DO NOTHING that makes ingestion idempotent
and the supersede dance that makes corrections auditable.

The Protocols let the service run against the in-memory implementation in tests
without a live Postgres.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

import pandas as pd

from sienner.coverage import DateRange, merge_ranges
from sienner.providers.base import (
    CORPORATE_ACTION_COLUMNS,
    RAW_BAR_COLUMNS,
    empty_corporate_actions,
    empty_raw_bars,
)

QUARANTINE_COLUMNS: tuple[str, ...] = (
    "ticker",
    "bar_date",
    "rule",
    "detail",
    "observed_value",
    "threshold",
)


def empty_quarantine() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": pd.Series(dtype="object"),
            "bar_date": pd.Series(dtype="object"),
            "rule": pd.Series(dtype="object"),
            "detail": pd.Series(dtype="object"),
            "observed_value": pd.Series(dtype="float64"),
            "threshold": pd.Series(dtype="float64"),
        }
    )


def _action_value(row: dict) -> float | None:
    """The value that decides whether an incoming action is a correction."""
    if row.get("action_type") == "split":
        return row.get("split_ratio")
    return row.get("cash_dividend")


def _same_value(left, right) -> bool:
    if left is None or right is None:
        return False
    return float(left) == float(right)


class Repository(Protocol):
    # --- bars and coverage (Stage 1a) ---
    def covered_ranges(self, ticker: str) -> list[DateRange]: ...

    def record_coverage(
        self, ticker: str, span: DateRange, *, source: str, bars_written: int
    ) -> None: ...

    def write_bars(self, ticker: str, bars: pd.DataFrame, *, source: str) -> int:
        """Insert bars, ignoring any that already exist. Returns rows written."""
        ...

    def read_bars(self, ticker: str, span: DateRange) -> pd.DataFrame: ...

    # --- corporate actions and quarantine (Stage 1b) ---
    def write_actions(self, ticker: str, actions: pd.DataFrame, *, source: str) -> int:
        """Insert actions not already on file. Returns rows written.

        A row whose (ticker, ex_date, action_type) already has an ACTIVE entry
        with a DIFFERENT value is a correction: insert the new row and mark the
        old one superseded. Never edit in place.
        """
        ...

    def read_actions(self, ticker: str, span: DateRange) -> pd.DataFrame:
        """Active actions with ex-date in the closed interval, ascending."""
        ...

    def write_quarantine(self, flags: pd.DataFrame) -> int:
        """Record soft-validation failures. Idempotent per (ticker, date, rule)."""
        ...

    def read_quarantine(self, ticker: str, span: DateRange) -> pd.DataFrame: ...


# Kept as an alias: Stage 1a code and tests refer to this name.
BarRepository = Repository


class InMemoryRepository:
    """Reference implementation. Used by tests, and the fallback when Postgres
    is unreachable so the API degrades instead of refusing to start."""

    def __init__(self) -> None:
        self._bars: dict[str, dict[date, dict]] = {}
        self._coverage: dict[str, list[DateRange]] = {}
        self._actions: dict[str, list[dict]] = {}
        self._quarantine: dict[tuple, dict] = {}

    # --- bars and coverage ---

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
        rows = [row for key, row in sorted(store.items()) if span.start <= key <= span.end]
        if not rows:
            return empty_raw_bars()
        return pd.DataFrame(rows).loc[:, list(RAW_BAR_COLUMNS)].reset_index(drop=True)

    # --- corporate actions ---

    def write_actions(self, ticker: str, actions: pd.DataFrame, *, source: str) -> int:
        if actions.empty:
            return 0
        store = self._actions.setdefault(ticker, [])
        written = 0
        for row in actions.to_dict("records"):
            key = (row["ex_date"], row["action_type"])
            active = next(
                (
                    existing
                    for existing in store
                    if (existing["ex_date"], existing["action_type"]) == key
                    and existing["superseded_by"] is None
                ),
                None,
            )
            if active is not None:
                if _same_value(_action_value(active), _action_value(row)):
                    continue  # already on file, unchanged
                active["superseded_by"] = len(store)
            store.append({**row, "source": source, "superseded_by": None})
            written += 1
        return written

    def read_actions(self, ticker: str, span: DateRange) -> pd.DataFrame:
        rows = [
            row
            for row in self._actions.get(ticker, [])
            if row["superseded_by"] is None and span.start <= row["ex_date"] <= span.end
        ]
        if not rows:
            return empty_corporate_actions()
        frame = pd.DataFrame(rows).loc[:, list(CORPORATE_ACTION_COLUMNS)]
        return frame.sort_values(["ex_date", "action_type"]).reset_index(drop=True)

    # --- quarantine ---

    def write_quarantine(self, flags: pd.DataFrame) -> int:
        if flags.empty:
            return 0
        written = 0
        for row in flags.to_dict("records"):
            key = (row["ticker"], row["bar_date"], row["rule"])
            if key in self._quarantine:
                continue
            self._quarantine[key] = row
            written += 1
        return written

    def read_quarantine(self, ticker: str, span: DateRange) -> pd.DataFrame:
        rows = [
            row
            for key, row in sorted(self._quarantine.items())
            if key[0] == ticker and span.start <= key[1] <= span.end
        ]
        if not rows:
            return empty_quarantine()
        return pd.DataFrame(rows).loc[:, list(QUARANTINE_COLUMNS)].reset_index(drop=True)


class PostgresRepository:
    """psycopg-backed implementation against migrations 001 and 002."""

    def __init__(self, pool) -> None:
        self._pool = pool

    # --- bars and coverage ---

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
                return max(cur.rowcount or 0, 0)

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

    # --- corporate actions ---

    def write_actions(self, ticker: str, actions: pd.DataFrame, *, source: str) -> int:
        if actions.empty:
            return 0
        written = 0
        with self._pool.connection() as conn:
            for row in actions.to_dict("records"):
                existing = conn.execute(
                    """
                    SELECT id, split_ratio, cash_dividend
                      FROM corporate_actions
                     WHERE ticker = %s AND ex_date = %s AND action_type = %s
                       AND superseded_at IS NULL
                    """,
                    (ticker, row["ex_date"], row["action_type"]),
                ).fetchone()

                if existing is not None:
                    current = existing[1] if row["action_type"] == "split" else existing[2]
                    if _same_value(current, _action_value(row)):
                        continue

                # The correction path, in the only order the schema allows.
                # The partial unique index on ACTIVE rows cannot be deferred, so
                # the old row has to stop being active BEFORE the replacement is
                # inserted; superseded_by is then backfilled once the new id
                # exists. All three statements are one transaction, so the
                # intermediate state is never visible.
                if existing is not None:
                    conn.execute(
                        "UPDATE corporate_actions SET superseded_at = now() WHERE id = %s",
                        (existing[0],),
                    )

                inserted = conn.execute(
                    """
                    INSERT INTO corporate_actions
                           (ticker, ex_date, action_type, split_ratio,
                            cash_dividend, provider_adj_dividend, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        ticker,
                        row["ex_date"],
                        row["action_type"],
                        row.get("split_ratio"),
                        row.get("cash_dividend"),
                        row.get("provider_adj_dividend"),
                        source,
                    ),
                ).fetchone()[0]

                if existing is not None:
                    conn.execute(
                        "UPDATE corporate_actions SET superseded_by = %s WHERE id = %s",
                        (inserted, existing[0]),
                    )
                written += 1
        return written

    def read_actions(self, ticker: str, span: DateRange) -> pd.DataFrame:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT ex_date, action_type, split_ratio, cash_dividend,
                       provider_adj_dividend
                  FROM corporate_actions_active
                 WHERE ticker = %s AND ex_date BETWEEN %s AND %s
                 ORDER BY ex_date, action_type
                """,
                (ticker, span.start, span.end),
            ).fetchall()
        if not rows:
            return empty_corporate_actions()
        frame = pd.DataFrame(rows, columns=list(CORPORATE_ACTION_COLUMNS))
        for column in ("split_ratio", "cash_dividend", "provider_adj_dividend"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame

    # --- quarantine ---

    def write_quarantine(self, flags: pd.DataFrame) -> int:
        if flags.empty:
            return 0
        records = [
            (
                row["ticker"],
                row["bar_date"],
                row["rule"],
                row["detail"],
                row.get("observed_value"),
                row.get("threshold"),
            )
            for row in flags.to_dict("records")
        ]
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO quarantine
                           (ticker, bar_date, rule, detail, observed_value, threshold)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ticker, bar_date, rule) DO NOTHING
                    """,
                    records,
                )
                return max(cur.rowcount or 0, 0)

    def read_quarantine(self, ticker: str, span: DateRange) -> pd.DataFrame:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT ticker, bar_date, rule, detail, observed_value, threshold
                  FROM quarantine
                 WHERE ticker = %s AND bar_date BETWEEN %s AND %s
                 ORDER BY bar_date, rule
                """,
                (ticker, span.start, span.end),
            ).fetchall()
        if not rows:
            return empty_quarantine()
        return pd.DataFrame(rows, columns=list(QUARANTINE_COLUMNS))


# Names Stage 1a used, kept so nothing above this module had to change.
InMemoryBarRepository = InMemoryRepository
PostgresBarRepository = PostgresRepository
