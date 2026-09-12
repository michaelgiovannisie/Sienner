"""Universe admission.

US-listed equities and ETFs, USD, one trading calendar. Everything else is
rejected at ingestion with an error naming the ticker and the reason, because
each excluded class breaks an assumption the return series already depends on.

Stage 1a can only check the symbol's shape - the exchange check needs the
securities table, which arrives in Stage 1b. The rejection reasons are written
out now so that the error text does not change when the check gets stronger.
"""

from __future__ import annotations

import re

_TICKER_PATTERN = re.compile(r"^([A-Z]{1,5})(?:[.\-]([A-Z]{1,3}))?$")

# A suffix cannot be classified by shape alone: BRK.B is a US class share and
# VOD.L is a London listing, and both are one letter. So the class suffixes are
# an allowlist and everything else is treated as an exchange code.
#
# This is the weakest check in the module and it is deliberately conservative -
# a wrongly-rejected US ticker is a visible error message, a wrongly-accepted
# foreign one silently mixes currencies. Stage 1b replaces it with a lookup
# against the securities table, which is the real answer.
_US_CLASS_SUFFIXES = frozenset({"A", "B", "C", "D", "U", "W", "WS", "PR"})

# Symbol shapes that are unambiguously outside the universe. Checked before the
# general pattern so the user gets the real reason rather than "malformed".
_EXCLUDED_SHAPES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^[A-Z]{2,10}(USD|USDT|BTC|ETH)$"),
        "crypto trades 24/7, so holiday-gap validation is meaningless",
    ),
    (
        re.compile(r"^[A-Z]{4}X$"),
        "mutual funds are forward-priced NAV, not traded bars",
    ),
)


class TickerRejected(ValueError):
    """A ticker is outside the universe Sienner commits to."""


def validate_ticker(raw: str) -> str:
    """Return the canonical ticker, or raise TickerRejected naming the reason."""
    ticker = (raw or "").strip().upper()
    if not ticker:
        raise TickerRejected("ticker is empty")
    if len(ticker) > 8:
        raise TickerRejected(f"{ticker}: longer than any US listed symbol")

    for pattern, reason in _EXCLUDED_SHAPES:
        if pattern.match(ticker):
            raise TickerRejected(f"{ticker}: outside the universe - {reason}")

    match = _TICKER_PATTERN.match(ticker)
    if not match:
        raise TickerRejected(
            f"{ticker}: not a US-listed equity or ETF symbol "
            "(letters, optionally a class suffix such as BRK.B)"
        )

    suffix = match.group(2)
    if suffix is not None and suffix not in _US_CLASS_SUFFIXES:
        raise TickerRejected(
            f"{ticker}: outside the universe - '{suffix}' is not a US share "
            "class suffix, and non-US listings need a per-exchange calendar "
            "and an FX series"
        )

    return ticker
