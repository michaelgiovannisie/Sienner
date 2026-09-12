"""Financial Modeling Prep adapter.

Two things about FMP are deliberately not hardcoded:

1. The endpoint path. FMP has shipped more than one API generation and the
   historical-price route differs between them. Base URL and path are settings,
   so moving generations is a .env change, not a code change.

2. The response envelope. Some FMP routes answer
   {"symbol": "AAPL", "historical": [...]} and others answer a bare list.
   This adapter accepts either rather than guessing which one your plan serves.

What this adapter does NOT do is read adjClose. Stage 1a computes a price
return from the raw close; Stage 1b computes a total return from raw closes
plus corporate actions, and uses adjClose only as a reconciliation check. The
field semantics questions in the README's spike section are therefore open
questions about Stage 1b, not blockers for this module.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pandas as pd

from sienner.config import Settings, get_settings
from sienner.providers.base import (
    MalformedProviderResponse,
    ProviderError,
    UnknownSymbolError,
    empty_raw_bars,
    normalise_raw_bars,
)

# FMP field name -> our column name.
_FIELD_MAP = {
    "date": "bar_date",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
}


class FMPProvider:
    """Implements MarketDataProvider against Financial Modeling Prep."""

    name = "fmp"

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client

    # -- MarketDataProvider ------------------------------------------------

    def fetch_raw_bars(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        if not self._settings.fmp_api_key:
            raise ProviderError(
                "SIENNER_FMP_API_KEY is not set; cannot reach the provider. "
                "Cached ranges still work offline."
            )
        payload = self._get(
            f"{self._settings.fmp_historical_path}/{ticker.upper()}",
            params={
                "from": start.isoformat(),
                "to": end.isoformat(),
                "apikey": self._settings.fmp_api_key,
            },
            ticker=ticker,
        )
        rows = self._unwrap(payload, ticker=ticker)
        if not rows:
            # An empty window is not an error: it may be a holiday stretch or a
            # range entirely before listing. The coverage ledger records that we
            # asked. Only a symbol FMP does not know is an error, and FMP
            # signals that with an error payload, handled in _unwrap.
            return empty_raw_bars()
        return normalise_raw_bars(self._to_frame(rows, ticker=ticker), ticker=ticker)

    # -- internals ---------------------------------------------------------

    def _get(self, path: str, *, params: dict[str, Any], ticker: str) -> Any:
        url = f"{self._settings.fmp_base_url.rstrip('/')}{path}"
        timeout = self._settings.provider_timeout_seconds
        try:
            if self._client is not None:
                response = self._client.get(url, params=params, timeout=timeout)
            else:
                with httpx.Client(timeout=timeout) as client:
                    response = client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{ticker}: transport error talking to FMP: {exc}") from exc

        if response.status_code == 404:
            raise UnknownSymbolError(f"{ticker}: FMP returned 404")
        if response.status_code == 401 or response.status_code == 403:
            raise ProviderError(
                f"{ticker}: FMP rejected the API key (HTTP {response.status_code})"
            )
        if response.status_code == 429:
            raise ProviderError(
                f"{ticker}: FMP rate limit hit (HTTP 429). "
                "Open question 1 in the README - plan limit and batch endpoints - "
                "decides whether this needs a queue."
            )
        if response.status_code >= 400:
            raise ProviderError(f"{ticker}: FMP returned HTTP {response.status_code}")

        try:
            return response.json()
        except ValueError as exc:
            raise MalformedProviderResponse(f"{ticker}: FMP response was not JSON") from exc

    @staticmethod
    def _unwrap(payload: Any, *, ticker: str) -> list[dict[str, Any]]:
        """Accept either response envelope; surface FMP's own error messages."""
        if isinstance(payload, dict):
            for error_key in ("Error Message", "error", "message"):
                if error_key in payload and "historical" not in payload:
                    raise ProviderError(f"{ticker}: FMP said: {payload[error_key]}")
            if "historical" in payload:
                historical = payload["historical"]
                if not isinstance(historical, list):
                    raise MalformedProviderResponse(
                        f"{ticker}: 'historical' was {type(historical).__name__}, expected list"
                    )
                return historical
            raise MalformedProviderResponse(
                f"{ticker}: unrecognised FMP object response with keys {sorted(payload)[:8]}"
            )
        if isinstance(payload, list):
            return payload
        raise MalformedProviderResponse(
            f"{ticker}: unrecognised FMP response type {type(payload).__name__}"
        )

    @staticmethod
    def _to_frame(rows: list[dict[str, Any]], *, ticker: str) -> pd.DataFrame:
        first = rows[0]
        if not isinstance(first, dict):
            raise MalformedProviderResponse(
                f"{ticker}: expected objects in the series, got {type(first).__name__}"
            )
        missing = [field for field in _FIELD_MAP if field not in first]
        if missing:
            raise MalformedProviderResponse(
                f"{ticker}: FMP rows missing fields {missing}; "
                f"got {sorted(first)[:10]}"
            )
        frame = pd.DataFrame(rows)
        return frame.loc[:, list(_FIELD_MAP)].rename(columns=_FIELD_MAP)
