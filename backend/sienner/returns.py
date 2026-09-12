"""The return series - the unit every downstream module consumes.

        r_t = (C_t * k_t + D_t) / C_{t-1} - 1

    C_t  raw close on day t
    k_t  split ratio effective on day t (1.0 if no split)
    D_t  cash dividend with ex-date t (0 if none)

STAGE 1A STATUS: this is a stub. `actions` is always empty, so k_t = 1 and
D_t = 0 and the expression reduces to a price return. The signature, the
return shape, and every consumer above this module are already the Stage 1b
ones. Filling in the actions join changes nothing downstream - that is the
condition that makes the 1a/1b split cheap, and it is decided here rather
than in 1b.

    !!! KNOWN-OPEN, BLOCKS 1B !!!
    The formula above scales C_t by k_t but not D_t. That is only correct if
    D_t is quoted in PRE-split per-share units. If the provider quotes the
    dividend post-split - the likelier convention - the correct form is
    (C_t + D_t) * k_t, and applying it the wrong way is quietly wrong on
    exactly the tickers that split. Resolve with the README's spike before
    implementing apply_actions(); do not guess.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

RETURN_COLUMNS: tuple[str, ...] = ("bar_date", "ret")
ACTION_COLUMNS: tuple[str, ...] = ("bar_date", "split_ratio", "cash_dividend")

#: Basis labels. The API echoes one of these so the UI can never imply that a
#: price return includes dividends. Stage 1b flips the value; the field, the
#: consumer, and the UI element all already exist.
BASIS_PRICE_RETURN = "price_return"
BASIS_TOTAL_RETURN = "total_return"

BASIS_DISCLAIMER = {
    BASIS_PRICE_RETURN: (
        "Price return, dividends not included. Stage 1a does not apply "
        "corporate actions."
    ),
    BASIS_TOTAL_RETURN: (
        "Total return. Dividends are assumed reinvested at the close on the "
        "ex-date, with no tax and no cash drag - the standard convention, and "
        "wrong for a taxable account."
    ),
}


def empty_actions() -> pd.DataFrame:
    """A correctly-typed zero-row actions frame - what Stage 1a always passes."""
    return pd.DataFrame(
        {
            "bar_date": pd.Series(dtype="object"),
            "split_ratio": pd.Series(dtype="float64"),
            "cash_dividend": pd.Series(dtype="float64"),
        }
    )


def to_daily_returns(
    bars: pd.DataFrame,
    actions: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Compute the daily return series from raw bars and corporate actions.

    Returns columns RETURN_COLUMNS, ascending by date, with one row fewer than
    `bars`: the first bar has no prior close and therefore no return. It is not
    zero-filled, because a zero return is a claim about a day that we cannot
    make.
    """
    if bars.empty:
        return pd.DataFrame({"bar_date": pd.Series(dtype="object"), "ret": pd.Series(dtype="float64")})

    frame = bars.loc[:, ["bar_date", "close"]].sort_values("bar_date", kind="stable")
    frame = frame.reset_index(drop=True)

    close = frame["close"].astype("float64")
    k, cash = _action_vectors(frame["bar_date"], actions)

    prior_close = close.shift(1)
    ret = (close * k + cash) / prior_close - 1.0

    out = pd.DataFrame({"bar_date": frame["bar_date"], "ret": ret})
    return out.iloc[1:].reset_index(drop=True)


def _action_vectors(
    bar_dates: pd.Series,
    actions: pd.DataFrame | None,
) -> tuple[pd.Series, pd.Series]:
    """k_t and D_t aligned to bar_dates.

    Stage 1a: `actions` is empty or None, so this returns the identity vectors
    and the caller computes a price return. The alignment is written out rather
    than short-circuited so that Stage 1b changes the caller, not the shape.
    """
    k = pd.Series(1.0, index=bar_dates.index, dtype="float64")
    cash = pd.Series(0.0, index=bar_dates.index, dtype="float64")

    if actions is None or actions.empty:
        return k, cash

    raise NotImplementedError(
        "Corporate actions are Stage 1b. Resolve the dividend/split unit "
        "question in the module docstring before implementing this."
    )


def equity_curve(
    returns: pd.DataFrame,
    *,
    start_date: date,
    base: float = 100.0,
) -> pd.DataFrame:
    """Cumulative product of returns, indexed to `base` at `start_date`.

    Computed per request rather than stored: it is a cumprod over a series that
    is already cached, and storing it would reintroduce exactly the invalidation
    problem that storing returns instead of adjusted prices avoids.

    No dollar amounts. The tool does not know your balance.
    """
    points = pd.DataFrame(
        {
            "bar_date": [start_date],
            "value": [float(base)],
        }
    )
    if returns.empty:
        return points

    values = float(base) * (1.0 + returns["ret"].astype("float64")).cumprod()
    tail = pd.DataFrame({"bar_date": returns["bar_date"], "value": values})
    return pd.concat([points, tail], ignore_index=True)


def total_return(curve: pd.DataFrame, *, base: float = 100.0) -> float:
    """Total return over the curve, as a fraction (0.43 == +43%)."""
    if curve.empty:
        return 0.0
    return float(curve["value"].iloc[-1]) / float(base) - 1.0
