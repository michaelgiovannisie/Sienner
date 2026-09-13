"""Tier one: hard reject. A failure must name the ticker, the date and the rule,
because an error you cannot act on is barely better than no error."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from sienner.schemas import (
    CORPORATE_ACTIONS_SCHEMA,
    RAW_BARS_SCHEMA,
    RETURN_SERIES_SCHEMA,
    SchemaViolation,
    validate,
)


def bars(**overrides) -> pd.DataFrame:
    rows = [
        {"bar_date": date(2024, 1, 2), "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 100},
        {"bar_date": date(2024, 1, 3), "open": 10.5, "high": 12.0, "low": 10.0, "close": 11.5, "volume": 120},
    ]
    frame = pd.DataFrame(rows).astype({"volume": "int64"})
    for column, value in overrides.items():
        frame.loc[1, column] = value
    return frame


def actions(**overrides) -> pd.DataFrame:
    frame = pd.DataFrame(
        [
            {
                "ex_date": date(2024, 6, 10),
                "action_type": "split",
                "split_ratio": 10.0,
                "cash_dividend": None,
                "provider_adj_dividend": None,
            }
        ]
    ).astype(
        {
            "split_ratio": "float64",
            "cash_dividend": "float64",
            "provider_adj_dividend": "float64",
        }
    )
    for column, value in overrides.items():
        frame.loc[0, column] = value
    return frame


def test_clean_bars_validate():
    validate(bars(), RAW_BARS_SCHEMA, ticker="AAPL")


def test_error_names_ticker_date_and_rule():
    with pytest.raises(SchemaViolation) as excinfo:
        validate(bars(close=-1.0), RAW_BARS_SCHEMA, ticker="AAPL")
    message = str(excinfo.value)

    assert "AAPL" in message
    assert "2024-01-03" in message
    assert "greater_than(0)" in message


def test_null_in_required_column_is_rejected():
    frame = bars()
    frame.loc[1, "close"] = None
    with pytest.raises(SchemaViolation):
        validate(frame, RAW_BARS_SCHEMA, ticker="AAPL")


def test_duplicate_dates_are_rejected():
    frame = pd.concat([bars().iloc[[0]], bars().iloc[[0]]], ignore_index=True)
    with pytest.raises(SchemaViolation, match="duplicate bar date"):
        validate(frame, RAW_BARS_SCHEMA, ticker="AAPL")


def test_non_monotonic_dates_are_rejected():
    frame = bars().iloc[::-1].reset_index(drop=True)
    with pytest.raises(SchemaViolation, match="ascending"):
        validate(frame, RAW_BARS_SCHEMA, ticker="AAPL")


def test_ohlc_must_bracket():
    with pytest.raises(SchemaViolation, match="bracket"):
        validate(bars(high=1.0), RAW_BARS_SCHEMA, ticker="AAPL")


def test_unexpected_column_is_rejected():
    """strict=True. A provider quietly adding a field must not slip into storage."""
    frame = bars()
    frame["adjClose"] = 10.0
    with pytest.raises(SchemaViolation):
        validate(frame, RAW_BARS_SCHEMA, ticker="AAPL")


def test_split_action_validates():
    validate(actions(), CORPORATE_ACTIONS_SCHEMA, ticker="NVDA")


def test_action_with_both_value_fields_is_rejected():
    with pytest.raises(SchemaViolation, match="split rows need split_ratio only"):
        validate(actions(cash_dividend=0.5), CORPORATE_ACTIONS_SCHEMA, ticker="NVDA")


def test_action_with_neither_value_field_is_rejected():
    with pytest.raises(SchemaViolation):
        validate(actions(split_ratio=None), CORPORATE_ACTIONS_SCHEMA, ticker="NVDA")


def test_return_of_minus_one_is_a_hard_reject():
    """A listed equity does not go to exactly zero overnight. That is bad data."""
    frame = pd.DataFrame(
        {"bar_date": [date(2024, 1, 3)], "ret": [-1.0]}
    )
    with pytest.raises(SchemaViolation):
        validate(frame, RETURN_SERIES_SCHEMA, ticker="AAPL")
