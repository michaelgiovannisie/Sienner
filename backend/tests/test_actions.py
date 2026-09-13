"""Corporate actions: alignment, storage, and the tests that are waiting on the
spike.

The tests below marked `strict xfail` are the Stage 1b acceptance tests. They
run today and fail with NotImplementedError, which is the honest status. When
apply_actions() is written they start passing - and because the marker is
strict, an unexpected PASS fails the suite, forcing whoever implements it to
remove the marker deliberately rather than discovering later that the tests were
never really checked.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from sienner.config import Settings
from sienner.coverage import DateRange
from sienner.providers.base import CORPORATE_ACTION_COLUMNS
from sienner.repository import InMemoryRepository
from sienner.returns import align_actions, to_daily_returns

FIXTURES = Path(__file__).parent / "fixtures"

pending_spike = pytest.mark.xfail(
    raises=NotImplementedError,
    strict=True,
    reason="apply_actions() is blocked on the FMP dividend-unit spike",
)


@pytest.fixture
def nvda() -> dict:
    return json.loads((FIXTURES / "nvda_split_2024.json").read_text())


def bars_from(fixture: dict) -> pd.DataFrame:
    frame = pd.DataFrame(fixture["bars"])
    frame["bar_date"] = pd.to_datetime(frame["date"]).dt.date
    return frame.loc[:, ["bar_date", "open", "high", "low", "close", "volume"]].astype(
        {"volume": "int64"}
    )


def with_future_split(actions: pd.DataFrame) -> pd.DataFrame:
    """`actions` plus a split dated well after the bar window.

    Built as one frame rather than concatenated, because concatenating an
    all-NA column is deprecated and the dtype it lands on is exactly the sort
    of detail this test is supposed to be insensitive to.
    """
    rows = actions.to_dict("records") + [
        {
            "ex_date": date(2027, 1, 4),
            "action_type": "split",
            "split_ratio": 4.0,
            "cash_dividend": None,
            "provider_adj_dividend": None,
        }
    ]
    return pd.DataFrame(rows).astype(actions.dtypes.to_dict())


def actions_from(fixture: dict) -> pd.DataFrame:
    rows = [
        {
            "ex_date": date.fromisoformat(fixture["split"]["ex_date"]),
            "action_type": "split",
            "split_ratio": fixture["split"]["numerator"] / fixture["split"]["denominator"],
            "cash_dividend": None,
            "provider_adj_dividend": None,
        }
    ]
    for row in fixture.get("dividends", []):
        rows.append(
            {
                "ex_date": date.fromisoformat(row["date"]),
                "action_type": "cash_dividend",
                "split_ratio": None,
                "cash_dividend": row["dividend"],
                "provider_adj_dividend": row.get("adjDividend"),
            }
        )
    return pd.DataFrame(rows).sort_values("ex_date").reset_index(drop=True)


# --- alignment: mechanical, convention-free, testable today ----------------


def test_split_lands_on_its_ex_date_only(nvda):
    bars, actions = bars_from(nvda), actions_from(nvda)
    k, cash = align_actions(bars["bar_date"], actions)

    split_row = bars.index[bars["bar_date"] == date(2024, 6, 10)][0]
    assert k.loc[split_row] == 10.0
    assert (k.drop(index=split_row) == 1.0).all()


def test_dividend_lands_on_its_ex_date_only(nvda):
    bars, actions = bars_from(nvda), actions_from(nvda)
    _, cash = align_actions(bars["bar_date"], actions)

    dividend_row = bars.index[bars["bar_date"] == date(2024, 6, 11)][0]
    assert cash.loc[dividend_row] == pytest.approx(0.01)
    assert (cash.drop(index=dividend_row) == 0.0).all()


def test_k_is_not_cumulative(nvda):
    """r_t compares C_t to C_{t-1}, so only a split effective on t matters.
    A cumulative factor here would corrupt every bar after any split."""
    bars, actions = bars_from(nvda), actions_from(nvda)
    k, _ = align_actions(bars["bar_date"], actions)

    after_split = bars["bar_date"] > date(2024, 6, 10)
    assert (k[after_split.values] == 1.0).all()


def test_multiple_dividends_on_one_ex_date_are_summed():
    bar_dates = pd.Series([date(2024, 3, 1), date(2024, 3, 2)])
    actions = pd.DataFrame(
        [
            {"ex_date": date(2024, 3, 2), "action_type": "cash_dividend",
             "split_ratio": None, "cash_dividend": 0.25, "provider_adj_dividend": None},
            {"ex_date": date(2024, 3, 2), "action_type": "cash_dividend",
             "split_ratio": None, "cash_dividend": 1.00, "provider_adj_dividend": None},
        ]
    )
    _, cash = align_actions(bar_dates, actions)
    assert cash.tolist() == [0.0, 1.25]


def test_action_outside_the_bar_window_is_ignored():
    bar_dates = pd.Series([date(2024, 3, 1), date(2024, 3, 2)])
    actions = pd.DataFrame(
        [{"ex_date": date(2020, 1, 1), "action_type": "split", "split_ratio": 4.0,
          "cash_dividend": None, "provider_adj_dividend": None}]
    )
    k, _ = align_actions(bar_dates, actions)
    assert k.tolist() == [1.0, 1.0]


# --- the immutability property, at the level that is testable today --------


def test_a_future_split_does_not_touch_historical_alignment(nvda):
    """The weaker half of the README's property, and the half that does not
    need apply_actions: appending an action dated after the window changes no
    historical k or D."""
    bars = bars_from(nvda)
    actions = actions_from(nvda)

    before_k, before_cash = align_actions(bars["bar_date"], actions)

    future = with_future_split(actions)
    after_k, after_cash = align_actions(bars["bar_date"], future)

    pd.testing.assert_series_equal(before_k, after_k)
    pd.testing.assert_series_equal(before_cash, after_cash)


def test_price_return_path_is_unaffected_by_the_spike(nvda):
    """With no actions the two candidate conventions agree, so the deployed
    tool keeps working while the spike is outstanding. The -90% is the
    uncorrected split, which is exactly what the quarantine rule catches."""
    returns = to_daily_returns(bars_from(nvda))
    split_day = returns.loc[returns["bar_date"] == date(2024, 6, 10), "ret"]

    assert float(split_day.iloc[0]) == pytest.approx(-0.8993, abs=1e-4)


# --- blocked on the spike --------------------------------------------------


@pending_spike
def test_split_is_corrected_so_the_return_is_small(nvda):
    """NVDA's raw close fell from 1208.88 to 121.79 on the 10:1 ex-date. The
    economic return that day was about +0.75%, not -89.9%."""
    bars, actions = bars_from(nvda), actions_from(nvda)
    settings = Settings(dividend_unit_convention="pre_split")

    returns = to_daily_returns(bars, actions, settings=settings)
    split_day = returns.loc[returns["bar_date"] == date(2024, 6, 10), "ret"]

    assert float(split_day.iloc[0]) == pytest.approx(0.0075, abs=0.002)


@pending_spike
def test_dividend_shows_up_in_the_return(nvda):
    """The demonstration that 1b worked: the same chart, visibly different for
    anything paying a dividend."""
    bars, actions = bars_from(nvda), actions_from(nvda)
    settings = Settings(dividend_unit_convention="pre_split")

    with_dividend = to_daily_returns(bars, actions, settings=settings)
    without = to_daily_returns(bars, actions[actions["action_type"] == "split"],
                               settings=settings)

    day = date(2024, 6, 11)
    assert (
        float(with_dividend.loc[with_dividend["bar_date"] == day, "ret"].iloc[0])
        > float(without.loc[without["bar_date"] == day, "ret"].iloc[0])
    )


@pending_spike
def test_appending_a_future_split_leaves_every_historical_return_identical(nvda):
    """The README's property test, in full. This is the immutability claim the
    entire cache design rests on: a corporate action in 2027 must not change
    the relationship between two adjacent closes in 2024."""
    bars, actions = bars_from(nvda), actions_from(nvda)
    settings = Settings(dividend_unit_convention="pre_split")

    before = to_daily_returns(bars, actions, settings=settings)
    after = to_daily_returns(bars, with_future_split(actions), settings=settings)

    pd.testing.assert_frame_equal(before, after)


# --- storage: supersede semantics ------------------------------------------


def test_actions_are_stored_and_read_back(nvda):
    repo = InMemoryRepository()
    actions = actions_from(nvda)

    assert repo.write_actions("NVDA", actions, source="fmp") == len(actions)

    read = repo.read_actions("NVDA", DateRange(date(2024, 6, 1), date(2024, 6, 30)))
    assert list(read.columns) == list(CORPORATE_ACTION_COLUMNS)
    assert len(read) == len(actions)


def test_rewriting_identical_actions_is_a_no_op(nvda):
    repo = InMemoryRepository()
    actions = actions_from(nvda)
    repo.write_actions("NVDA", actions, source="fmp")

    assert repo.write_actions("NVDA", actions, source="fmp") == 0


def test_a_corrected_ratio_supersedes_rather_than_edits():
    """The gap in 'once computed, r_t never changes': it holds against future
    actions, not against corrections to past ones. A restated ratio must be an
    insert plus a supersede, so the old value stays auditable."""
    repo = InMemoryRepository()
    span = DateRange(date(2024, 6, 1), date(2024, 6, 30))
    original = pd.DataFrame(
        [{"ex_date": date(2024, 6, 10), "action_type": "split", "split_ratio": 10.0,
          "cash_dividend": None, "provider_adj_dividend": None}]
    )
    corrected = original.copy()
    corrected.loc[0, "split_ratio"] = 4.0

    repo.write_actions("NVDA", original, source="fmp")
    assert repo.write_actions("NVDA", corrected, source="fmp") == 1

    active = repo.read_actions("NVDA", span)
    assert len(active) == 1, "only the correction is active"
    assert float(active["split_ratio"].iloc[0]) == 4.0


def test_actions_outside_the_span_are_not_read_back(nvda):
    repo = InMemoryRepository()
    repo.write_actions("NVDA", actions_from(nvda), source="fmp")

    assert repo.read_actions("NVDA", DateRange(date(2023, 1, 1), date(2023, 12, 31))).empty
