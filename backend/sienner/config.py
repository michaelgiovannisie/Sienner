"""Configuration.

Every assumption the README names is a field here, not a literal buried in a
function. Several are unused in Stage 1a and exist so that the stage that needs
them changes a default rather than introducing a concept.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SIENNER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Provider ---
    fmp_api_key: str = ""
    fmp_base_url: str = "https://financialmodelingprep.com"
    fmp_historical_path: str = "/api/v3/historical-price-full"
    provider_timeout_seconds: float = 30.0

    # --- Database ---
    database_url: str = "postgresql://sienner:sienner@localhost:5432/sienner"
    db_pool_min_size: int = 1
    db_pool_max_size: int = 4

    # --- Universe (README: rejected at ingestion, with a reason) ---
    allowed_exchanges: tuple[str, ...] = ("NYSE", "NASDAQ", "AMEX")
    base_currency: str = "USD"

    # --- Return series ---
    # Stage 1a computes a price return: the stub passes k=1, D=0. Flipping this
    # to True in Stage 1b is what turns on the corporate-actions join.
    dividends_reinvested_at_ex_date_close: bool = False

    # --- Presentation ---
    equity_curve_base: float = 100.0

    # --- Not yet consumed. Declared here so Stage 1b/1c wire them up rather
    # --- than inventing a threshold at the call site.
    trading_days_per_year: int = 252
    risk_free_rate: float = 0.0
    max_tickers: int = 20
    weight_sum_tolerance: float = 1e-6
    forward_fill_limit_trading_days: int = 5
    quarantine_abs_log_return: float = 0.5

    # --- API ---
    cors_allow_origins: tuple[str, ...] = ("http://localhost:5173",)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
