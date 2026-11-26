"""Configuration.

Every assumption the README names is a field here, not a literal buried in a
function. Several are unused in Stage 1a and exist so that the stage that needs
them changes a default rather than introducing a concept.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# The repo root, from this file: sienner/ -> backend/ -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # .env lives at the repo root, but commands get run from backend/ as often
    # as from the root. A bare ".env" resolves against the CURRENT WORKING
    # DIRECTORY, so running anything from backend/ silently found no file and
    # reported the key as unset. Both locations are checked, root last so it
    # wins on conflict.
    model_config = SettingsConfigDict(
        env_prefix="SIENNER_",
        env_file=(".env", str(_REPO_ROOT / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Provider ---
    fmp_api_key: str = ""
    fmp_base_url: str = "https://financialmodelingprep.com"

    # The /stable/ generation. The /api/v3 and /api/v4 routes now answer
    # 403 "Legacy Endpoint ... no longer supported", confirmed by
    # sienner.scripts.fmp_probe against a live key.
    fmp_historical_path: str = "/stable/historical-price-eod/full"
    provider_timeout_seconds: float = 30.0

    # The stable generation takes the symbol as a query parameter; the legacy
    # generation appended it to the path. One switch rather than three, because
    # FMP is consistent within a generation. Re-run fmp_probe if routes change
    # again.
    fmp_symbol_in_query: bool = True

    # --- Database ---
    database_url: str = "postgresql://sienner:sienner@localhost:5432/sienner"
    db_pool_min_size: int = 1
    db_pool_max_size: int = 4

    # --- Universe (README: rejected at ingestion, with a reason) ---
    allowed_exchanges: tuple[str, ...] = ("NYSE", "NASDAQ", "AMEX")
    base_currency: str = "USD"

    # --- Provider: corporate action routes ---
    fmp_splits_path: str = "/stable/splits"
    fmp_dividends_path: str = "/stable/dividends"

    # --- Return series ---
    # The standard total-return convention: dividends reinvested at the close on
    # the ex-date, no tax, no cash drag. Wrong for a taxable account, and the UI
    # says so.
    dividends_reinvested_at_ex_date_close: bool = True

    # THE STAGE 1B BLOCKER.
    #
    # r_t = (C_t * k_t + D_t) / C_{t-1} - 1 scales the close by the split ratio
    # but not the dividend. That is correct only if D_t is quoted in PRE-split
    # per-share units. If the provider quotes it post-split, the correct form is
    # (C_t + D_t) * k_t, and getting it backwards is quietly wrong on exactly
    # the tickers that split.
    #
    # RESOLVED by the spike (AAPL, 8 years, spans the Aug 2020 4:1 split):
    # FMP's `dividend` is the as-declared historical per-share amount, and
    # daily_bars holds raw never-back-adjusted closes, so both sides of the
    # return ratio are in the same era's share units. See apply_actions.
    #
    # Still unobserved: a dividend ex-date falling ON a split ex-date, the only
    # case where the two conventions differ. validation flags it rather than
    # trusting this default there.
    dividend_unit_convention: str = "pre_split"  # pre_split | post_split

    # --- Validation, tier 2: quarantine and report ---
    # A single-day |log return| above this is either bad data or a real event.
    # Hard-failing on the real ones would block legitimate backtests, so these
    # rows are flagged and surfaced, never silently dropped or propagated.
    quarantine_abs_log_return: float = 0.5

    # The generic threshold above is a 64.9% move, which sits ABOVE the
    # signature of the failure mode it is best placed to catch: a missed 2:1
    # split is -0.69 log and trips it, but a missed 3:2 is -0.405 and passes
    # clean. So a move whose size matches a common split ratio, on a date with
    # no corresponding corporate action, is its own rule.
    split_shaped_move_ratios: tuple[float, ...] = (
        4 / 3, 1.5, 2.0, 3.0, 4.0, 5.0, 7.0, 10.0, 20.0,
    )
    split_shaped_move_tolerance: float = 0.02  # relative
    # Floor below which a move is too ordinary to attribute to a missed split.
    split_shaped_move_min_abs_return: float = 0.20

    # Gaps longer than this many sessions that the market calendar does not
    # explain are quarantined.
    market_calendar: str = "NYSE"
    quarantine_gap_sessions: int = 1

    # --- Presentation ---
    equity_curve_base: float = 100.0

    # --- Not yet consumed. Declared here so Stage 1c wires them up rather
    # --- than inventing a threshold at the call site.
    trading_days_per_year: int = 252
    risk_free_rate: float = 0.0
    max_tickers: int = 20
    weight_sum_tolerance: float = 1e-6
    forward_fill_limit_trading_days: int = 5

    # --- API ---
    cors_allow_origins: tuple[str, ...] = ("http://localhost:5173",)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
