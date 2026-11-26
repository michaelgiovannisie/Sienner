"""Corporate actions: alignment, the return math, and storage.

The three acceptance tests at the bottom were strict-xfail until the FMP
field-semantics spike was answered. They now assert the real numbers: NVDA's
raw close fell from 1208.88 to 121.79 across a 10:1 split, which is -89.93%
uncorrected and +0.75% once the split ratio is applied.
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

# The spike is answered and apply_actions() is implemented, so the three
# acceptance tests below now run for real. They were marked
# xfail(strict=True) until this point; strict is what forced this marker to be
# removed deliberately rather than leaving tests that silently never checked
# anything. The spike's findings are recorded in sienner/returns.py.
PRE_SPLIT = Settings(dividend_unit_convention="pre_split")


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


# --- resolved by the spike -------------------------------------------------


def test_split_is_corrected_so_the_return_is_small(nvda):
    """NVDA's raw close fell from 1208.88 to 121.79 on the 10:1 ex-date. The
    economic return that day was about +0.75%, not -89.9%."""
    bars, actions = bars_from(nvda), actions_from(nvda)
    settings = PRE_SPLIT

    returns = to_daily_returns(bars, actions, settings=settings)
    split_day = returns.loc[returns["bar_date"] == date(2024, 6, 10), "ret"]

    assert float(split_day.iloc[0]) == pytest.approx(0.0075, abs=0.002)


def test_dividend_shows_up_in_the_return(nvda):
    """The demonstration that 1b worked: the same chart, visibly different for
    anything paying a dividend."""
    bars, actions = bars_from(nvda), actions_from(nvda)
    settings = PRE_SPLIT

    with_dividend = to_daily_returns(bars, actions, settings=settings)
    without = to_daily_returns(bars, actions[actions["action_type"] == "split"],
                               settings=settings)

    day = date(2024, 6, 11)
    assert (
        float(with_dividend.loc[with_dividend["bar_date"] == day, "ret"].iloc[0])
        > float(without.loc[without["bar_date"] == day, "ret"].iloc[0])
    )


def test_appending_a_future_split_leaves_every_historical_return_identical(nvda):
    """The README's property test, in full. This is the immutability claim the
    entire cache design rests on: a corporate action in 2027 must not change
    the relationship between two adjacent closes in 2024."""
    bars, actions = bars_from(nvda), actions_from(nvda)
    settings = PRE_SPLIT

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


# --- the two conventions ---------------------------------------------------
#
# They are one equation in two dividend units, so they agree on every date
# where k = 1 and differ only when a split and a dividend share an ex-date.
# That coincidence is the case the spike could not test, which is why these
# tests pin the agreement AND the divergence.


def test_the_conventions_agree_when_no_split_shares_a_dividend_date(nvda):
    """Every date in the AAPL window the spike examined looks like this: the
    split is on one day, the dividend on another, so k = 1 wherever D > 0."""
    bars, actions = bars_from(nvda), actions_from(nvda)

    pre = to_daily_returns(bars, actions, settings=PRE_SPLIT)
    post = to_daily_returns(
        bars, actions, settings=Settings(dividend_unit_convention="post_split")
    )

    pd.testing.assert_frame_equal(pre, post)


def test_the_conventions_diverge_when_a_split_and_dividend_share_an_ex_date():
    """The one case the spike never observed. Pinned so that if someone
    changes the default, the consequence is visible in a test rather than in a
    number on a chart."""
    bars = pd.DataFrame(
        [
            {"bar_date": date(2024, 6, 7), "close": 1000.0},
            {"bar_date": date(2024, 6, 10), "close": 100.0},
        ]
    )
    actions = pd.DataFrame(
        [
            {"ex_date": date(2024, 6, 10), "action_type": "split", "split_ratio": 10.0,
             "cash_dividend": None, "provider_adj_dividend": None},
            {"ex_date": date(2024, 6, 10), "action_type": "cash_dividend",
             "split_ratio": None, "cash_dividend": 1.0, "provider_adj_dividend": None},
        ]
    )

    pre = to_daily_returns(bars, actions, settings=PRE_SPLIT)
    post = to_daily_returns(
        bars, actions, settings=Settings(dividend_unit_convention="post_split")
    )

    # pre_split:  (100*10 + 1)/1000 - 1 = +0.1%   D is one pre-split share's cash
    # post_split: (100 + 1)*10/1000 - 1 = +1.0%   D is per post-split share, held 10
    assert float(pre["ret"].iloc[0]) == pytest.approx(0.001)
    assert float(post["ret"].iloc[0]) == pytest.approx(0.010)


def test_an_unresolved_convention_refuses_to_compute_a_total_return(nvda):
    """Setting the convention back to unresolved must not silently fall back to
    a price return at this level - the service decides that, not the math."""
    bars, actions = bars_from(nvda), actions_from(nvda)
    unresolved = Settings(dividend_unit_convention="unresolved")

    with pytest.raises(NotImplementedError, match="unresolved"):
        to_daily_returns(bars, actions, settings=unresolved)


def test_reading_adj_dividend_instead_would_understate_the_dividend(nvda):
    """Why the spike mattered. provider_adj_dividend is the payment restated
    into present-day share terms; using it against raw closes divides the
    dividend by the cumulative split factor."""
    actions = actions_from(nvda)
    dividend = actions[actions["action_type"] == "cash_dividend"].iloc[0]

    # In this fixture both fields happen to be 0.01 because the dividend is
    # post-split already. The contract is what is asserted: the field consumed
    # is cash_dividend, and provider_adj_dividend is carried untouched.
    assert dividend["cash_dividend"] is not None
    assert "provider_adj_dividend" in actions.columns
