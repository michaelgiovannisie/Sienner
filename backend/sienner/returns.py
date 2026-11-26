"""The return series - the unit every downstream module consumes.

        r_t = (C_t * k_t + D_t) / C_{t-1} - 1

    C_t  raw close on day t
    k_t  split ratio effective on day t (1.0 if no split)
    D_t  cash dividend with ex-date t (0 if none)

STAGE 1B STATUS: implemented. The spike (sienner.scripts.fmp_spike, run
against a live key on AAPL over 8 years, which spans the Aug 2020 4:1 split)
settled the two questions this module depended on:

  * `dividend` is the AS-DECLARED historical per-share amount; `adjDividend`
    is the same payment restated into present-day share terms. Evidence:
    dividend / adjDividend = 4.0 for every dividend preceding the 4:1 split.
    Since daily_bars holds RAW closes that are never back-adjusted, a 2019 bar
    is denominated in 2019 shares - so D_t must be `dividend`. Reading
    adjDividend here would divide the dividend by the cumulative split factor
    on exactly the tickers that split.

  * `date` on the dividend route is the EX-date. The evidence is structural:
    the payload carries recordDate and paymentDate as separate fields that
    differ from `date`. (The spike also ran a price test - mean return on
    dividend dates vs all days - but with n=32 that gap sits inside one
    standard error, so it corroborates and does not establish.)

    !!! WHAT THE SPIKE DID NOT SETTLE !!!
    No AAPL date carries both a split and a dividend, so the units of `D_t`
    when an ex-date COINCIDES with a split ex-date were never observed. That
    is the only case where the two conventions below differ. Rather than
    assume, validation.detect() flags any such date
    (RULE_ACTION_COINCIDENCE) so it surfaces for review instead of being
    computed silently under an untested assumption.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from sienner.config import Settings, get_settings

RETURN_COLUMNS: tuple[str, ...] = ("bar_date", "ret")
ACTION_COLUMNS: tuple[str, ...] = (
    "ex_date",
    "action_type",
    "split_ratio",
    "cash_dividend",
)

#: Basis labels. The API echoes one of these so the UI can never imply that a
#: price return includes dividends. Stage 1b flips the value; the field, the
#: consumer, and the UI element all already exist.
BASIS_PRICE_RETURN = "price_return"
BASIS_TOTAL_RETURN = "total_return"

#: Which per-share units the provider quotes a cash dividend in. See
#: apply_actions: these select between two forms of one equation.
CONVENTION_PRE_SPLIT = "pre_split"
CONVENTION_POST_SPLIT = "post_split"
CONVENTION_UNRESOLVED = "unresolved"

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
    """A correctly-typed zero-row actions frame.

    Re-exported from providers.base so callers that only import returns do not
    need to reach past it for the empty case.
    """
    from sienner.providers.base import empty_corporate_actions

    return empty_corporate_actions()


def to_daily_returns(
    bars: pd.DataFrame,
    actions: pd.DataFrame | None = None,
    settings: "Settings | None" = None,
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
    prior_close = close.shift(1)

    if actions is None or actions.empty:
        # No actions to apply: k = 1, D = 0, and the expression reduces to a
        # price return. Both candidate conventions agree here, so this path is
        # not blocked on the spike.
        ret = close / prior_close - 1.0
    else:
        k, cash = align_actions(frame["bar_date"], actions)
        ret = apply_actions(
            close,
            prior_close,
            k,
            cash,
            convention=(settings or get_settings()).dividend_unit_convention,
        )

    out = pd.DataFrame({"bar_date": frame["bar_date"], "ret": ret})
    return out.iloc[1:].reset_index(drop=True)


def align_actions(
    bar_dates: pd.Series,
    actions: pd.DataFrame | None,
) -> tuple[pd.Series, pd.Series]:
    """k_t and D_t aligned to bar_dates.

    Mechanical and convention-free: this only says WHICH split ratio and WHICH
    dividend land on each bar date. How they combine with the close is the open
    question, and that lives in apply_actions().

    k_t is the split ratio effective on day t, 1.0 if none. It is NOT a
    cumulative product: r_t compares C_t to C_{t-1}, so only a split effective
    on t itself is needed to put the two closes on one scale.

    D_t is the total cash dividend with ex-date t, 0.0 if none. Multiple
    dividends on one ex-date are summed - rare, but a special alongside a
    regular does happen.
    """
    k = pd.Series(1.0, index=bar_dates.index, dtype="float64")
    cash = pd.Series(0.0, index=bar_dates.index, dtype="float64")

    if actions is None or actions.empty:
        return k, cash

    position = {day: index for index, day in bar_dates.items()}

    for row in actions.to_dict("records"):
        index = position.get(row["ex_date"])
        if index is None:
            # An action dated outside the bar window, or on a non-session day.
            # Not this function's problem to report: validation covers it.
            continue
        if row.get("action_type") == "split" and pd.notna(row.get("split_ratio")):
            k.at[index] *= float(row["split_ratio"])
        elif row.get("action_type") == "cash_dividend" and pd.notna(
            row.get("cash_dividend")
        ):
            cash.at[index] += float(row["cash_dividend"])

    return k, cash


def apply_actions(
    close: pd.Series,
    prior_close: pd.Series,
    k: pd.Series,
    cash: pd.Series,
    *,
    convention: str,
) -> pd.Series:
    """Combine closes, split ratios and dividends into a daily return.

    The two conventions are ONE equation written in two dividend units, not two
    competing theories. Derive it and that becomes obvious.

    Hold one pre-split share into day t, where a k-for-1 split takes effect.
    The share becomes k post-split shares, each closing at C_t, so the position
    is worth k * C_t. If the dividend is quoted per POST-split share you
    receive k * D_post in cash:

        r_t = (k*C_t + k*D_post) / C_{t-1} - 1 = (C_t + D_post) * k_t / C_{t-1} - 1

    If it is quoted per PRE-split share you receive D_pre once, and since
    D_pre = k * D_post:

        r_t = (k*C_t + D_pre) / C_{t-1} - 1 = (C_t * k_t + D_t) / C_{t-1} - 1

    Both are implemented because the setting exists and the distinction is
    real. The default is pre_split: FMP's `dividend` is the as-declared
    historical amount, and the stored closes are raw, so both sides of the
    ratio are in the same era's share units.

    On every date with no split (k = 1) the two forms are algebraically
    identical, which is every date in the AAPL window the spike examined. The
    coincidence case is flagged by validation rather than assumed - see the
    module docstring.
    """
    if convention == CONVENTION_PRE_SPLIT:
        return (close * k + cash) / prior_close - 1.0
    if convention == CONVENTION_POST_SPLIT:
        return (close + cash) * k / prior_close - 1.0

    raise NotImplementedError(
        "The dividend unit convention is unresolved, so a total return cannot "
        f"be computed (convention={convention!r}). Run "
        "`python -m sienner.scripts.fmp_spike --ticker AAPL --years 8` against "
        "a live key and set SIENNER_DIVIDEND_UNIT_CONVENTION to pre_split or "
        "post_split."
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
