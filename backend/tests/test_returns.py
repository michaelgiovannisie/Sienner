"""The return series is the unit everything downstream consumes, so these are
the tests that would catch a silent change to it."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from sienner.returns import (
    BASIS_PRICE_RETURN,
    BASIS_TOTAL_RETURN,
    BASIS_DISCLAIMER,
    empty_actions,
    equity_curve,
    to_daily_returns,
    total_return,
)


def test_returns_are_close_over_prior_close(bars_factory):
    returns = to_daily_returns(bars_factory([100.0, 110.0, 99.0]))

    assert list(returns.columns) == ["bar_date", "ret"]
    assert returns["ret"].tolist() == pytest.approx([0.10, -0.10])


def test_first_bar_has_no_return_and_is_not_zero_filled(bars_factory):
    bars = bars_factory([100.0, 110.0, 99.0])
    returns = to_daily_returns(bars)

    assert len(returns) == len(bars) - 1
    assert returns["bar_date"].iloc[0] == bars["bar_date"].iloc[1]


def test_single_bar_yields_no_returns(bars_factory):
    assert to_daily_returns(bars_factory([100.0])).empty


def test_empty_bars_yield_empty_returns(bars_factory):
    assert to_daily_returns(bars_factory([])).empty


def test_unsorted_input_is_sorted_before_differencing():
    """FMP answers newest-first. A return computed in that order is the
    negative-ish of the right one, which is the kind of bug that produces a
    plausible chart."""
    rows = [
        {"bar_date": date(2024, 1, 3), "close": 99.0},
        {"bar_date": date(2024, 1, 1), "close": 100.0},
        {"bar_date": date(2024, 1, 2), "close": 110.0},
    ]
    returns = to_daily_returns(pd.DataFrame(rows))

    assert returns["bar_date"].tolist() == [date(2024, 1, 2), date(2024, 1, 3)]
    assert returns["ret"].tolist() == pytest.approx([0.10, -0.10])


def test_equity_curve_is_indexed_to_base_at_effective_start(bars_factory):
    bars = bars_factory([100.0, 110.0, 99.0])
    curve = equity_curve(to_daily_returns(bars), start_date=bars["bar_date"].iloc[0])

    assert curve["value"].iloc[0] == 100.0
    assert curve["value"].tolist() == pytest.approx([100.0, 110.0, 99.0])
    assert len(curve) == len(bars)


def test_equity_curve_respects_a_non_default_base(bars_factory):
    bars = bars_factory([100.0, 110.0])
    curve = equity_curve(
        to_daily_returns(bars), start_date=bars["bar_date"].iloc[0], base=1.0
    )
    assert curve["value"].tolist() == pytest.approx([1.0, 1.1])


def test_total_return_is_a_fraction(bars_factory):
    bars = bars_factory([100.0, 110.0, 99.0])
    curve = equity_curve(to_daily_returns(bars), start_date=bars["bar_date"].iloc[0])

    assert total_return(curve) == pytest.approx(-0.01)


def test_stage_1a_ignores_dividends_and_says_so(bars_factory):
    """A price return is what Stage 1a computes. This test exists to make the
    Stage 1b change visible: when actions are wired in, it must be updated
    deliberately rather than discovered."""
    bars = bars_factory([100.0, 100.0])
    returns = to_daily_returns(bars)

    assert returns["ret"].tolist() == pytest.approx([0.0])
    assert "dividends not included" in BASIS_DISCLAIMER[BASIS_PRICE_RETURN]
    assert "ex-date" in BASIS_DISCLAIMER[BASIS_TOTAL_RETURN]


def test_passing_actions_changes_the_result_rather_than_being_ignored(bars_factory):
    """Silently dropping corporate actions would produce a wrong number that
    looks right. This asserts they are applied.

    A 10:1 split on the third bar: the raw close falls from 110 to 99, which is
    -10% uncorrected. With k = 10 applied it is 99*10/110 - 1 = +800%, because
    these synthetic closes are not split-shaped. The magnitude is not the point
    - the point is that the two numbers differ."""
    bars = bars_factory([100.0, 110.0, 99.0])
    actions = pd.DataFrame(
        [
            {
                "ex_date": bars["bar_date"].iloc[2],
                "action_type": "split",
                "split_ratio": 10.0,
                "cash_dividend": None,
                "provider_adj_dividend": None,
            }
        ]
    )

    without = to_daily_returns(bars)
    with_actions = to_daily_returns(bars, actions)

    assert float(without["ret"].iloc[-1]) == pytest.approx(-0.10)
    assert float(with_actions["ret"].iloc[-1]) == pytest.approx(8.0)


def test_an_unresolved_convention_refuses_rather_than_guessing(bars_factory):
    """If the convention is ever reset to unresolved, the math must refuse. The
    service decides whether to fall back to a price return; this layer does
    not get to guess."""
    from sienner.config import Settings

    bars = bars_factory([100.0, 110.0, 99.0])
    actions = pd.DataFrame(
        [
            {
                "ex_date": bars["bar_date"].iloc[2],
                "action_type": "split",
                "split_ratio": 10.0,
                "cash_dividend": None,
                "provider_adj_dividend": None,
            }
        ]
    )
    with pytest.raises(NotImplementedError, match="unresolved"):
        to_daily_returns(
            bars, actions, settings=Settings(dividend_unit_convention="unresolved")
        )


def test_empty_actions_is_the_stage_1a_path(bars_factory):
    returns = to_daily_returns(bars_factory([100.0, 110.0]), empty_actions())
    assert returns["ret"].tolist() == pytest.approx([0.10])
