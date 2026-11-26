"""Tier two: quarantine and report.

Tier one (sienner.schemas) hard-rejects at the boundary - those rows never
reach storage. This module handles the rules that need context a schema does
not have: neighbouring bars, the corporate actions on file, the trading
calendar.

A quarantined row is stored, flagged, and surfaced in the API response. It is
never silently dropped and never silently propagates into a backtest.

Rationale for two tiers, restated because it drives every threshold below: with
the adjustment done correctly, a very large single-day move is either bad data
or a real event. Hard-failing on the real ones would block legitimate
backtests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

import pandas as pd

from sienner.config import Settings, get_settings

RULE_ABS_LOG_RETURN = "abs_log_return"
RULE_SPLIT_SHAPED_MOVE = "split_shaped_move"
RULE_CALENDAR_GAP = "calendar_gap"
RULE_ACTION_COINCIDENCE = "action_coincidence"


@dataclass(frozen=True)
class QuarantineFlag:
    ticker: str
    bar_date: date
    rule: str
    detail: str
    observed_value: float | None = None
    threshold: float | None = None


@lru_cache(maxsize=4)
def _sessions(calendar_name: str, start: date, end: date) -> tuple[date, ...]:
    """Trading sessions in [start, end] for the named calendar."""
    import pandas_market_calendars as mcal

    calendar = mcal.get_calendar(calendar_name)
    days = calendar.valid_days(start_date=start.isoformat(), end_date=end.isoformat())
    return tuple(day.date() for day in days)


def detect(
    returns: pd.DataFrame,
    *,
    ticker: str,
    actions: pd.DataFrame | None = None,
    settings: Settings | None = None,
) -> list[QuarantineFlag]:
    """Run every soft rule over a return series. Order of rules is not
    significant - a bar can trip more than one, and each is recorded."""
    settings = settings or get_settings()
    flags: list[QuarantineFlag] = []
    if returns.empty:
        return flags

    flags.extend(_abs_log_return(returns, ticker=ticker, settings=settings))
    flags.extend(
        _split_shaped_move(returns, ticker=ticker, actions=actions, settings=settings)
    )
    flags.extend(_calendar_gaps(returns, ticker=ticker, settings=settings))
    flags.extend(_action_coincidence(returns, ticker=ticker, actions=actions))
    return flags


def _abs_log_return(
    returns: pd.DataFrame, *, ticker: str, settings: Settings
) -> list[QuarantineFlag]:
    """The generic outlier rule. Catches what nothing else names."""
    threshold = settings.quarantine_abs_log_return
    flags: list[QuarantineFlag] = []

    for row in returns.to_dict("records"):
        ret = float(row["ret"])
        if ret <= -1.0:
            continue  # hard-rejected upstream by RETURN_SERIES_SCHEMA
        log_return = math.log1p(ret)
        if abs(log_return) > threshold:
            flags.append(
                QuarantineFlag(
                    ticker=ticker,
                    bar_date=row["bar_date"],
                    rule=RULE_ABS_LOG_RETURN,
                    detail=(
                        f"single-day log return {log_return:+.4f} "
                        f"({ret:+.2%}) exceeds {threshold}"
                    ),
                    observed_value=log_return,
                    threshold=threshold,
                )
            )
    return flags


def _split_shaped_move(
    returns: pd.DataFrame,
    *,
    ticker: str,
    actions: pd.DataFrame | None,
    settings: Settings,
) -> list[QuarantineFlag]:
    """A move whose SIZE matches a common split ratio, on a date with no
    corporate action on file.

    This rule exists because the generic threshold cannot catch the failure mode
    it is closest to. At the default 0.5, a missed 2:1 split (-0.693 log) trips
    the generic rule, but a missed 3:2 (-0.405) passes clean - and a missed
    split is the single most likely way for a return series to be wrong.

    The "no action on file" condition is what keeps the false-positive rate
    usable: a real split is in corporate_actions, so a real split date does not
    fire this. What fires is a split-shaped move the actions feed did not
    mention.
    """
    known_action_dates = _action_dates(actions)
    tolerance = settings.split_shaped_move_tolerance
    floor = settings.split_shaped_move_min_abs_return
    flags: list[QuarantineFlag] = []

    for row in returns.to_dict("records"):
        ret = float(row["ret"])
        if abs(ret) < floor:
            continue
        if row["bar_date"] in known_action_dates:
            continue

        gross = 1.0 + ret
        match = _matching_split_ratio(gross, settings.split_shaped_move_ratios, tolerance)
        if match is None:
            continue

        ratio, direction = match
        shape = f"{ratio:g}-for-1" if direction == "forward" else f"1-for-{ratio:g}"
        flags.append(
            QuarantineFlag(
                ticker=ticker,
                bar_date=row["bar_date"],
                rule=RULE_SPLIT_SHAPED_MOVE,
                detail=(
                    f"move of {ret:+.2%} matches an unrecorded {shape} split "
                    "and no corporate action is on file for this date"
                ),
                observed_value=ret,
                threshold=ratio,
            )
        )
    return flags


def _matching_split_ratio(
    gross: float, ratios: tuple[float, ...], tolerance: float
) -> tuple[float, str] | None:
    """Does `gross` (1 + r) look like an unadjusted split?

    A missed k-for-1 forward split shows the price at 1/k of the prior close.
    A missed 1-for-k reverse split shows it at k times the prior close.
    """
    for ratio in ratios:
        forward = 1.0 / ratio
        if abs(gross - forward) <= tolerance * forward:
            return ratio, "forward"
        if abs(gross - ratio) <= tolerance * ratio:
            return ratio, "reverse"
    return None


def _action_dates(actions: pd.DataFrame | None) -> set[date]:
    if actions is None or actions.empty or "ex_date" not in actions.columns:
        return set()
    return set(actions["ex_date"].tolist())


def _action_coincidence(
    returns: pd.DataFrame,
    *,
    ticker: str,
    actions: pd.DataFrame | None,
) -> list[QuarantineFlag]:
    """A split and a cash dividend sharing an ex-date.

    This is the one case the field-semantics spike could not test: no AAPL date
    carries both, so the per-share units of the dividend on a split date were
    never observed. It is also the ONLY case where the two dividend-unit
    conventions produce different numbers - everywhere else k = 1 and the two
    forms are algebraically identical.

    So the return for such a date is computed under an assumption that has not
    been checked against data. Rather than let that pass silently on a number
    someone might act on, it is flagged. If this ever fires, the fix is to look
    up that specific dividend's declaration and confirm which share count it
    was declared against - not to trust the default.
    """
    if actions is None or actions.empty or "action_type" not in actions.columns:
        return []

    splits = set(actions.loc[actions["action_type"] == "split", "ex_date"])
    dividends = set(actions.loc[actions["action_type"] == "cash_dividend", "ex_date"])
    both = splits & dividends
    if not both:
        return []

    in_series = set(returns["bar_date"])
    return [
        QuarantineFlag(
            ticker=ticker,
            bar_date=day,
            rule=RULE_ACTION_COINCIDENCE,
            detail=(
                "a split and a cash dividend share this ex-date, which is the "
                "one case the dividend-unit spike could not test; the return "
                "for this day rests on an unverified assumption about which "
                "share count the dividend was declared against"
            ),
        )
        for day in sorted(both & in_series)
    ]


def _calendar_gaps(
    returns: pd.DataFrame, *, ticker: str, settings: Settings
) -> list[QuarantineFlag]:
    """Gaps longer than one trading day that the market calendar does not explain.

    A three-day gap across a weekend is the calendar doing its job. A three-day
    gap on a Tuesday is missing data. Only the calendar can tell them apart,
    which is why the universe is one exchange and one currency: widening it
    means a calendar per exchange, not a looser rule here.
    """
    dates = list(returns["bar_date"])
    if len(dates) < 2:
        return []

    try:
        sessions = _sessions(settings.market_calendar, dates[0], dates[-1])
    except Exception as exc:  # noqa: BLE001 - a calendar failure must not be fatal
        return [
            QuarantineFlag(
                ticker=ticker,
                bar_date=dates[-1],
                rule=RULE_CALENDAR_GAP,
                detail=f"trading calendar unavailable, gap check skipped: {exc}",
            )
        ]

    session_index = {day: position for position, day in enumerate(sessions)}
    allowed = settings.quarantine_gap_sessions
    flags: list[QuarantineFlag] = []

    for previous, current in zip(dates, dates[1:]):
        left, right = session_index.get(previous), session_index.get(current)
        if left is None or right is None:
            # A bar on a non-session date is itself the anomaly.
            missing = current if right is None else previous
            flags.append(
                QuarantineFlag(
                    ticker=ticker,
                    bar_date=current,
                    rule=RULE_CALENDAR_GAP,
                    detail=(
                        f"bar dated {missing} is not a session on the "
                        f"{settings.market_calendar} calendar"
                    ),
                )
            )
            continue

        missed = right - left - 1
        if missed > allowed:
            flags.append(
                QuarantineFlag(
                    ticker=ticker,
                    bar_date=current,
                    rule=RULE_CALENDAR_GAP,
                    detail=(
                        f"{missed} trading session(s) missing between {previous} "
                        f"and {current}; the calendar does not explain the gap"
                    ),
                    observed_value=float(missed),
                    threshold=float(allowed),
                )
            )
    return flags


def to_frame(flags: list[QuarantineFlag]) -> pd.DataFrame:
    """Flags as a frame, for persistence and for the API response."""
    if not flags:
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
    return pd.DataFrame([flag.__dict__ for flag in flags])
