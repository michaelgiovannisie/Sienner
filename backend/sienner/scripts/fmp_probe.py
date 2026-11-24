"""Find out which FMP route shape this API key actually answers.

The spike assumes it can fetch bars, splits and dividends. When all three
tickers come back empty, the question is not about field semantics yet - it is
which URL this plan serves. FMP has shipped more than one API generation and
retired older routes, so this probe tries the known shapes and reports what
each one says.

    python -m sienner.scripts.fmp_probe

It prints a table of status codes and a short body sample, then names the
SIENNER_FMP_* settings to put in .env. It never prints the API key.
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from sienner.config import get_settings

# (label, path template, query params besides the key)
# {s} is the symbol. Some generations put the symbol in the path, others in a
# `symbol` query parameter - both shapes are here.
BAR_ROUTES = [
    ("v3 historical-price-full", "/api/v3/historical-price-full/{s}", {}),
    ("stable historical-price-eod/full", "/stable/historical-price-eod/full", {"symbol": "{s}"}),
    ("stable historical-price-eod/light", "/stable/historical-price-eod/light", {"symbol": "{s}"}),
    ("v3 historical-chart 1day", "/api/v3/historical-chart/1day/{s}", {}),
]

SPLIT_ROUTES = [
    ("v3 stock_split", "/api/v3/historical-price-full/stock_split/{s}", {}),
    ("stable splits", "/stable/splits", {"symbol": "{s}"}),
    ("v4 stock_split", "/api/v4/stock_split", {"symbol": "{s}"}),
]

DIVIDEND_ROUTES = [
    ("v3 stock_dividend", "/api/v3/historical-price-full/stock_dividend/{s}", {}),
    ("stable dividends", "/stable/dividends", {"symbol": "{s}"}),
    ("v4 stock_dividend", "/api/v4/stock_dividend", {"symbol": "{s}"}),
]


def describe(payload) -> str:
    """A one-line summary of what came back, without dumping the whole body."""
    if isinstance(payload, dict):
        for key in ("Error Message", "error", "message"):
            if key in payload:
                return f"error: {str(payload[key])[:90]}"
        if "historical" in payload and isinstance(payload["historical"], list):
            rows = payload["historical"]
            if not rows:
                return "ok, but 0 rows"
            return f"{len(rows)} rows, fields: {sorted(rows[0])}"
        return f"object, keys: {sorted(payload)}"
    if isinstance(payload, list):
        if not payload:
            return "ok, but 0 rows"
        first = payload[0]
        if isinstance(first, dict):
            return f"{len(payload)} rows, fields: {sorted(first)}"
        return f"{len(payload)} rows (not objects)"
    return f"{type(payload).__name__}"


def probe(client, base, key, symbol, routes, heading):
    print(f"\n{heading}")
    print("-" * 78)
    winners = []
    for label, path, extra in routes:
        url = base + path.format(s=symbol)
        params = {k: v.format(s=symbol) for k, v in extra.items()}
        params["apikey"] = key
        try:
            response = client.get(url, params=params, timeout=25.0)
        except httpx.HTTPError as exc:
            print(f"  {label:<36} UNREACHABLE  {type(exc).__name__}")
            continue

        try:
            summary = describe(response.json())
        except ValueError:
            summary = f"non-JSON: {response.text[:60]!r}"

        print(f"  {label:<36} {response.status_code:<5} {summary}")
        if response.status_code == 200 and "rows" in summary and "0 rows" not in summary:
            winners.append((label, path, extra))
    return winners


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbol", default="AAPL")
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.fmp_api_key:
        print("SIENNER_FMP_API_KEY is not set in .env", file=sys.stderr)
        return 1

    base = settings.fmp_base_url.rstrip("/")
    print(f"base URL: {base}")
    print(f"symbol:   {args.symbol}")
    print(f"key:      set, {len(settings.fmp_api_key)} chars, ends {settings.fmp_api_key[-4:]}")

    with httpx.Client() as client:
        bars = probe(client, base, settings.fmp_api_key, args.symbol, BAR_ROUTES, "BARS")
        splits = probe(client, base, settings.fmp_api_key, args.symbol, SPLIT_ROUTES, "SPLITS")
        divs = probe(client, base, settings.fmp_api_key, args.symbol, DIVIDEND_ROUTES, "DIVIDENDS")

    print("\n" + "=" * 78)
    print("WHAT TO PUT IN .env")
    print("=" * 78)
    if not (bars and splits and divs):
        print("Not everything answered. Notes:")
        print("  401/403 everywhere -> the key is wrong, expired, or not yet active")
        print("  403 on some routes -> those endpoints are not in your plan")
        print("  404 everywhere     -> the route generation is wrong; check FMP's docs")
        print("                        for the current path and re-run with it")
    for label, path, extra in (bars[:1] + splits[:1] + divs[:1]):
        setting = {
            "BARS": "SIENNER_FMP_HISTORICAL_PATH",
            "SPLITS": "SIENNER_FMP_SPLITS_PATH",
            "DIVIDENDS": "SIENNER_FMP_DIVIDENDS_PATH",
        }
        kind = (
            "SIENNER_FMP_HISTORICAL_PATH" if (label, path, extra) in bars
            else "SIENNER_FMP_SPLITS_PATH" if (label, path, extra) in splits
            else "SIENNER_FMP_DIVIDENDS_PATH"
        )
        # The adapter appends /{symbol}; a query-parameter route needs an
        # adapter change, so say which it is.
        if extra:
            print(f"  {kind}={path}   # NOTE: symbol goes in the query string,")
            print("       the adapter currently appends /{symbol} to the path - tell me and I'll change it")
        else:
            print(f"  {kind}={path.replace('/{s}', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
