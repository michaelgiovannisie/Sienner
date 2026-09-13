"""Declared shapes for every DataFrame that crosses a process boundary.

Provider output, cached read, backtest input, metric output. A failure names the
ticker, the date, and the rule, because "SchemaError: column 'close' failed
check greater_than(0)" is not something you can act on at 11pm.

This is tier one of the two-tier validation: HARD REJECT. Nulls in required
columns, non-positive prices, duplicate dates, non-monotonic dates. These never
reach storage.

Tier two - quarantine and report - is sienner.validation, because a soft rule
needs context a schema does not have: the surrounding bars, the corporate
actions on file, the trading calendar.
"""

from __future__ import annotations

import pandas as pd
import pandera.pandas as pa


class SchemaViolation(ValueError):
    """A frame failed its declared schema. Message names ticker, date, rule."""


RAW_BARS_SCHEMA = pa.DataFrameSchema(
    {
        "bar_date": pa.Column(object, nullable=False),
        "open": pa.Column(float, pa.Check.gt(0), nullable=False),
        "high": pa.Column(float, pa.Check.gt(0), nullable=False),
        "low": pa.Column(float, pa.Check.gt(0), nullable=False),
        "close": pa.Column(float, pa.Check.gt(0), nullable=False),
        "volume": pa.Column("int64", pa.Check.ge(0), nullable=False),
    },
    checks=[
        pa.Check(
            lambda df: ~df["bar_date"].duplicated(),
            name="dates_unique",
            error="duplicate bar date",
        ),
        pa.Check(
            lambda df: df["bar_date"].is_monotonic_increasing,
            name="dates_monotonic",
            error="bar dates are not ascending",
        ),
        pa.Check(
            lambda df: (df["high"] >= df["low"])
            & (df["high"] >= df["open"])
            & (df["high"] >= df["close"])
            & (df["low"] <= df["open"])
            & (df["low"] <= df["close"]),
            name="ohlc_consistent",
            error="high/low do not bracket open/close",
        ),
    ],
    strict=True,
    ordered=True,
    name="raw_bars",
)


CORPORATE_ACTIONS_SCHEMA = pa.DataFrameSchema(
    {
        "ex_date": pa.Column(object, nullable=False),
        "action_type": pa.Column(
            str, pa.Check.isin(["split", "cash_dividend"]), nullable=False
        ),
        # Exactly one of these is populated per row; the cross-field rule below
        # is what enforces that, since either column alone is legitimately null.
        "split_ratio": pa.Column(float, pa.Check.gt(0), nullable=True),
        "cash_dividend": pa.Column(float, pa.Check.gt(0), nullable=True),
        "provider_adj_dividend": pa.Column(float, nullable=True),
    },
    checks=[
        pa.Check(
            lambda df: df.apply(
                lambda row: (
                    pd.notna(row["split_ratio"]) and pd.isna(row["cash_dividend"])
                )
                if row["action_type"] == "split"
                else (
                    pd.notna(row["cash_dividend"]) and pd.isna(row["split_ratio"])
                ),
                axis=1,
            ),
            name="action_shape",
            error="split rows need split_ratio only; dividend rows need cash_dividend only",
        ),
        pa.Check(
            lambda df: df["ex_date"].is_monotonic_increasing,
            name="dates_monotonic",
            error="ex-dates are not ascending",
        ),
    ],
    strict=True,
    ordered=True,
    name="corporate_actions",
)


RETURN_SERIES_SCHEMA = pa.DataFrameSchema(
    {
        "bar_date": pa.Column(object, nullable=False),
        # A return of exactly -1.0 means the price went to zero, which for a
        # listed equity means the data is wrong, not that the company vanished
        # overnight. Anything at or below that is a hard reject.
        "ret": pa.Column(float, pa.Check.gt(-1.0), nullable=False),
    },
    checks=[
        pa.Check(
            lambda df: ~df["bar_date"].duplicated(),
            name="dates_unique",
            error="duplicate date in return series",
        ),
        pa.Check(
            lambda df: df["bar_date"].is_monotonic_increasing,
            name="dates_monotonic",
            error="return series is not ascending",
        ),
    ],
    strict=True,
    ordered=True,
    name="return_series",
)


def validate(
    frame: pd.DataFrame,
    schema: pa.DataFrameSchema,
    *,
    ticker: str,
) -> pd.DataFrame:
    """Validate, or raise SchemaViolation naming ticker, date and rule."""
    try:
        return schema.validate(frame, lazy=True)
    except pa.errors.SchemaErrors as exc:
        raise SchemaViolation(_describe(exc, frame, ticker=ticker, schema=schema)) from exc


def _describe(
    exc: "pa.errors.SchemaErrors",
    frame: pd.DataFrame,
    *,
    ticker: str,
    schema: pa.DataFrameSchema,
) -> str:
    """Turn pandera's failure_cases table into something actionable.

    The date matters more than the row index: "AAPL 2024-06-10 failed
    greater_than(0)" is a thing you can go look up. "index 1387" is not.
    """
    cases = exc.failure_cases
    date_column = "bar_date" if "bar_date" in frame.columns else "ex_date"

    def when_for(index) -> str:
        if index is None or date_column not in frame.columns:
            return "-"
        try:
            return str(frame[date_column].iloc[int(index)])
        except (ValueError, TypeError, IndexError):
            return "-"

    # A DataFrame-level check reports one failure per column, so a single bad
    # row arrives as six near-identical cases. Column-level failures name the
    # actual culprit, so they sort first, and (date, check) is deduplicated.
    records = sorted(
        cases.to_dict("records"),
        key=lambda r: 0 if r.get("schema_context") == "Column" else 1,
    )

    seen: set[tuple[str, object]] = set()
    lines: list[str] = []
    for record in records:
        when = when_for(record.get("index"))
        key = (when, record.get("check"))
        if key in seen:
            continue
        seen.add(key)
        if record.get("schema_context") == "Column":
            what = f"{record.get('column')} failed {record.get('check')} (value {record.get('failure_case')!r})"
        else:
            what = str(record.get("check"))
        lines.append(f"{ticker} {when}: {what}")

    shown, lines = lines[:5], lines[:5]
    remaining = len(seen) - len(shown)
    if remaining > 0:
        lines.append(f"... and {remaining} more")

    return f"{schema.name} schema violation - " + "; ".join(lines)
