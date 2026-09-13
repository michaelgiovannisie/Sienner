"""Tier two: quarantine and report.

The rule that matters most here is split_shaped_move, which exists because the
generic threshold provably cannot catch the failure mode it is closest to.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from sienner.config import Settings
from sienner.validation import (
    RULE_ABS_LOG_RETURN,
    RULE_CALENDAR_GAP,
    RULE_SPLIT_SHAPED_MOVE,
    detect,
    to_frame,
)


@pytest.fixture
def settings() -> Settings:
    return Settings()


def returns(rows: list[tuple[date, float]]) -> pd.DataFrame:
    return pd.DataFrame([{"bar_date": day, "ret": ret} for day, ret in rows])


def rules_for(flags, day: date) -> set[str]:
    return {flag.rule for flag in flags if flag.bar_date == day}


# --- generic outlier -------------------------------------------------------


def test_ordinary_days_are_not_flagged(settings):
    series = returns([(date(2024, 6, 10), 0.01), (date(2024, 6, 11), -0.02)])
    assert detect(series, ticker="T", settings=settings) == []


def test_move_above_the_log_threshold_is_flagged(settings):
    series = returns([(date(2024, 6, 10), 0.80)])  # log 0.588 > 0.5
    flags = detect(series, ticker="T", settings=settings)

    assert RULE_ABS_LOG_RETURN in rules_for(flags, date(2024, 6, 10))


def test_the_threshold_is_configurable_not_a_literal(settings):
    series = returns([(date(2024, 6, 10), 0.30)])
    assert detect(series, ticker="T", settings=settings) == []

    loosened = Settings(quarantine_abs_log_return=0.1, split_shaped_move_min_abs_return=0.99)
    assert detect(series, ticker="T", settings=loosened)


# --- split-shaped move -----------------------------------------------------


def test_missed_two_for_one_split_is_caught_by_both_rules(settings):
    """-50% is -0.693 log, so the generic rule fires too. Both are recorded;
    a bar can trip more than one rule."""
    series = returns([(date(2024, 6, 10), -0.50)])
    flags = detect(series, ticker="T", settings=settings)

    assert rules_for(flags, date(2024, 6, 10)) == {
        RULE_ABS_LOG_RETURN,
        RULE_SPLIT_SHAPED_MOVE,
    }


def test_missed_three_for_two_split_is_caught_only_by_the_split_rule(settings):
    """This is the whole reason the rule exists. -33.3% is -0.405 log, which
    passes the 0.5 generic threshold clean."""
    series = returns([(date(2024, 6, 10), -1 / 3)])
    flags = detect(series, ticker="T", settings=settings)

    assert rules_for(flags, date(2024, 6, 10)) == {RULE_SPLIT_SHAPED_MOVE}


def test_missed_ten_for_one_split_is_caught(settings):
    series = returns([(date(2024, 6, 10), -0.90)])
    flags = detect(series, ticker="T", settings=settings)

    assert RULE_SPLIT_SHAPED_MOVE in rules_for(flags, date(2024, 6, 10))


def test_reverse_split_shape_is_caught(settings):
    """A 1-for-10 reverse split multiplies the price by 10."""
    series = returns([(date(2024, 6, 10), 9.0)])
    flags = detect(series, ticker="T", settings=settings)

    assert RULE_SPLIT_SHAPED_MOVE in rules_for(flags, date(2024, 6, 10))


def test_a_recorded_split_does_not_fire_the_rule(settings):
    """The 'no action on file' condition is what keeps false positives usable."""
    series = returns([(date(2024, 6, 10), -0.50)])
    actions = pd.DataFrame(
        [
            {
                "ex_date": date(2024, 6, 10),
                "action_type": "split",
                "split_ratio": 2.0,
                "cash_dividend": None,
                "provider_adj_dividend": None,
            }
        ]
    )
    flags = detect(series, ticker="T", actions=actions, settings=settings)

    assert RULE_SPLIT_SHAPED_MOVE not in rules_for(flags, date(2024, 6, 10))
    # The generic rule still fires - a 50% move is worth a human look either way.
    assert RULE_ABS_LOG_RETURN in rules_for(flags, date(2024, 6, 10))


def test_ordinary_large_move_below_the_floor_is_not_called_a_split(settings):
    """A -20% day is a crash, not a 5:4 split. The floor keeps the rule from
    claiming otherwise."""
    series = returns([(date(2024, 6, 10), -0.19)])
    flags = detect(series, ticker="T", settings=settings)

    assert RULE_SPLIT_SHAPED_MOVE not in rules_for(flags, date(2024, 6, 10))


# --- calendar gaps ---------------------------------------------------------


def test_a_weekend_is_not_a_gap(settings):
    """Friday to Monday. The calendar explains it."""
    series = returns([(date(2024, 6, 7), 0.01), (date(2024, 6, 10), 0.01)])
    flags = detect(series, ticker="T", settings=settings)

    assert not [f for f in flags if f.rule == RULE_CALENDAR_GAP]


def test_a_holiday_is_not_a_gap(settings):
    """2024-07-04 is a market holiday, so Jul 3 to Jul 5 is contiguous."""
    series = returns([(date(2024, 7, 3), 0.01), (date(2024, 7, 5), 0.01)])
    flags = detect(series, ticker="T", settings=settings)

    assert not [f for f in flags if f.rule == RULE_CALENDAR_GAP]


def test_missing_midweek_sessions_are_a_gap(settings):
    """Monday to Friday with nothing in between is three missing sessions."""
    series = returns([(date(2024, 6, 10), 0.01), (date(2024, 6, 14), 0.01)])
    flags = detect(series, ticker="T", settings=settings)

    gap = [f for f in flags if f.rule == RULE_CALENDAR_GAP]
    assert gap and "3 trading session(s) missing" in gap[0].detail


def test_a_bar_on_a_non_session_date_is_flagged(settings):
    """A bar dated Christmas Day is itself the anomaly."""
    series = returns([(date(2024, 12, 24), 0.01), (date(2024, 12, 25), 0.01)])
    flags = detect(series, ticker="T", settings=settings)

    gap = [f for f in flags if f.rule == RULE_CALENDAR_GAP]
    assert gap and "not a session" in gap[0].detail


# --- shape -----------------------------------------------------------------


def test_to_frame_is_empty_but_typed_when_nothing_fired():
    frame = to_frame([])
    assert frame.empty
    assert list(frame.columns) == [
        "ticker", "bar_date", "rule", "detail", "observed_value", "threshold",
    ]
