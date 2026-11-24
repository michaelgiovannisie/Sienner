"""The spike the README makes a precondition for Stage 1b.

FMP's field semantics are not resolvable by discussion. This script answers the
three questions against a live key and writes a machine-readable verdict.

    python -m sienner.scripts.fmp_spike --ticker NVDA --ticker AAPL --ticker KO

Questions, and how each is decided from data rather than from documentation:

1. Does adjClose adjust for splits only, or splits and dividends?

   Build a split-only adjusted series from the raw closes and the splits on
   file, then compare it to the provider's adjClose. If the two agree to within
   rounding, adjClose is split-only. If they diverge, and the divergence over
   the window is close to the cumulative dividend yield, adjClose includes
   dividends. The dividend yield is the discriminator because it is the only
   quantity of the right magnitude.

2. Are dividends ex-dated or pay-dated?

   Two signals. First, whether the dividend payload carries separate date and
   paymentDate fields and whether they differ - if `date` equals paymentDate on
   every row, `date` is not the ex-date. Second, the price test: on a true
   ex-date the close gaps down by roughly the dividend. Compare the mean return
   on claimed dividend dates against the unconditional mean. An ex-date series
   is visibly negative; a pay-date series is noise.

3. Is adjDividend already split-adjusted?

   Only decidable on a ticker that both split and paid dividends. Take the
   dividends before a known split and compare dividend to adjDividend. If the
   ratio is the split factor, adjDividend is split-adjusted. If it is 1.0,
   it is not.

Nothing here writes to the database. It reads, it reasons, it prints, and it
saves a JSON verdict for the README.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from sienner.config import get_settings

VERDICT_UNRESOLVED = "unresolved"


@dataclass
class Finding:
    question: str
    verdict: str
    confidence: str
    evidence: dict[str, Any] = field(default_factory=dict)


def _get(client: httpx.Client, url: str, params: dict) -> Any:
    response = client.get(url, params=params, timeout=30.0)
    response.raise_for_status()
    return response.json()


def _rows(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        if "historical" in payload:
            return payload["historical"]
        raise SystemExit(f"Unexpected response object with keys {sorted(payload)[:8]}")
    if isinstance(payload, list):
        return payload
    raise SystemExit(f"Unexpected response type {type(payload).__name__}")


def fetch_all(ticker: str, years: int) -> dict[str, pd.DataFrame]:
    settings = get_settings()
    if not settings.fmp_api_key:
        raise SystemExit(
            "SIENNER_FMP_API_KEY is not set. This script needs a live key - "
            "that is the entire point of it."
        )
    base = settings.fmp_base_url.rstrip("/")
    end = date.today()
    start = end - timedelta(days=365 * years)

    def route(path: str) -> tuple[str, dict]:
        """Same generation switch the adapter uses, so the spike probes the
        routes the application will actually call."""
        params = {"apikey": settings.fmp_api_key}
        if settings.fmp_symbol_in_query:
            params["symbol"] = ticker
            return f"{base}{path}", params
        return f"{base}{path}/{ticker}", params

    with httpx.Client() as client:
        bar_url, bar_params = route(settings.fmp_historical_path)
        bars = _rows(
            _get(
                client,
                bar_url,
                {**bar_params, "from": start.isoformat(), "to": end.isoformat()},
            )
        )
        splits = _rows(_get(client, *route(settings.fmp_splits_path)))
        dividends = _rows(_get(client, *route(settings.fmp_dividends_path)))

    def framed(rows: list[dict], date_field: str = "date") -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        frame[date_field] = pd.to_datetime(frame[date_field], errors="coerce").dt.date
        return frame.dropna(subset=[date_field]).sort_values(date_field).reset_index(drop=True)

    return {
        "bars": framed(bars),
        "splits": framed(splits),
        "dividends": framed(dividends),
    }


def question_1(data: dict[str, pd.DataFrame], ticker: str) -> Finding:
    """adjClose: split-only, or split-and-dividend?"""
    bars, splits, dividends = data["bars"], data["splits"], data["dividends"]
    if bars.empty:
        return Finding(
            "Does adjClose adjust for splits only, or splits and dividends?",
            VERDICT_UNRESOLVED,
            "none",
            {"reason": "no bars returned"},
        )
    if "adjClose" not in bars.columns:
        # Not a failure to measure - a finding. The stable EOD route does not
        # expose an adjusted close, so there is nothing to reconcile against
        # and the question is moot. Sienner never consumed adjClose anyway; it
        # was only ever a cross-check. The cross-check has to come from
        # somewhere else, or go.
        return Finding(
            "Does adjClose adjust for splits only, or splits and dividends?",
            "not_applicable_no_adjclose",
            "high",
            {
                "ticker": ticker,
                "bar_fields": sorted(bars.columns),
                "note": (
                    "This route exposes no adjusted close. The reconciliation "
                    "test in the README cannot be written against it."
                ),
            },
        )

    # Split-only adjusted series: divide every close BEFORE a split by the
    # cumulative factor of the splits that follow it.
    close = bars["close"].astype(float).copy()
    factor = pd.Series(1.0, index=bars.index)
    if not splits.empty and {"numerator", "denominator"} <= set(splits.columns):
        for row in splits.to_dict("records"):
            ratio = float(row["numerator"]) / float(row["denominator"])
            before = bars["date"] < row["date"]
            factor.loc[before] = factor.loc[before] / ratio
    split_only = close * factor

    provider_adj = bars["adjClose"].astype(float)
    # Scale both to end at the same point; only the SHAPE is informative.
    scaled = split_only * (provider_adj.iloc[-1] / split_only.iloc[-1])
    relative_gap = float(abs(provider_adj.iloc[0] - scaled.iloc[0]) / scaled.iloc[0])

    dividend_yield = 0.0
    if not dividends.empty and "dividend" in dividends.columns:
        paid = pd.to_numeric(dividends["dividend"], errors="coerce").fillna(0).sum()
        dividend_yield = float(paid / close.iloc[0]) if close.iloc[0] else 0.0

    if relative_gap < 0.005:
        verdict = "splits_only"
        confidence = "high"
    elif dividend_yield and abs(relative_gap - dividend_yield) < 0.5 * max(dividend_yield, 1e-9):
        verdict = "splits_and_dividends"
        confidence = "high"
    elif relative_gap > 0.02:
        verdict = "splits_and_dividends"
        confidence = "medium"
    else:
        verdict = VERDICT_UNRESOLVED
        confidence = "low"

    return Finding(
        "Does adjClose adjust for splits only, or splits and dividends?",
        verdict,
        confidence,
        {
            "ticker": ticker,
            "relative_gap_at_window_start": round(relative_gap, 6),
            "cumulative_dividend_yield": round(dividend_yield, 6),
            "splits_in_window": int(len(splits)),
            "interpretation": (
                "gap ~= 0 means adjClose is split-only; gap ~= dividend yield "
                "means it includes dividends"
            ),
        },
    )


def question_2(data: dict[str, pd.DataFrame], ticker: str) -> Finding:
    """Is the dividend `date` the ex-date or the pay-date?"""
    bars, dividends = data["bars"], data["dividends"]
    question = "Are dividends ex-dated or pay-dated?"
    if dividends.empty or bars.empty:
        return Finding(question, VERDICT_UNRESOLVED, "none", {"reason": "no dividends"})

    fields = sorted(dividends.columns)
    has_payment_date = "paymentDate" in dividends.columns
    distinct_payment_date = False
    if has_payment_date:
        payment = pd.to_datetime(dividends["paymentDate"], errors="coerce").dt.date
        distinct_payment_date = bool((payment != dividends["date"]).any())

    # Price test: on a true ex-date the close gaps down by roughly the dividend.
    bars = bars.sort_values("date").reset_index(drop=True)
    bars["ret"] = bars["close"].astype(float).pct_change()
    returns_by_date = dict(zip(bars["date"], bars["ret"]))

    on_dividend = [
        returns_by_date[day]
        for day in dividends["date"]
        if day in returns_by_date and pd.notna(returns_by_date[day])
    ]
    all_returns = [r for r in bars["ret"].tolist() if pd.notna(r)]

    mean_on = statistics.fmean(on_dividend) if on_dividend else float("nan")
    mean_all = statistics.fmean(all_returns) if all_returns else float("nan")

    if not on_dividend:
        verdict, confidence = VERDICT_UNRESOLVED, "none"
    elif has_payment_date and distinct_payment_date and mean_on < mean_all:
        verdict, confidence = "ex_dated", "high"
    elif mean_on < mean_all - 0.0005:
        verdict, confidence = "ex_dated", "medium"
    else:
        verdict, confidence = "possibly_pay_dated", "low"

    return Finding(
        question,
        verdict,
        confidence,
        {
            "ticker": ticker,
            "dividend_payload_fields": fields,
            "has_separate_paymentDate": has_payment_date,
            "paymentDate_differs_from_date": distinct_payment_date,
            "mean_return_on_dividend_dates": round(mean_on, 6) if on_dividend else None,
            "mean_return_all_days": round(mean_all, 6) if all_returns else None,
            "matched_dividend_dates": len(on_dividend),
            "interpretation": (
                "a genuinely ex-dated series shows a visibly more negative mean "
                "return on those dates; a pay-dated one shows noise"
            ),
        },
    )


def question_3(data: dict[str, pd.DataFrame], ticker: str) -> Finding:
    """Is adjDividend already split-adjusted?"""
    splits, dividends = data["splits"], data["dividends"]
    question = "Is adjDividend already split-adjusted?"
    if splits.empty or dividends.empty or "adjDividend" not in dividends.columns:
        return Finding(
            question,
            VERDICT_UNRESOLVED,
            "none",
            {"reason": "needs a ticker that both split and paid dividends"},
        )

    latest_split = splits.iloc[-1]
    ratio = float(latest_split["numerator"]) / float(latest_split["denominator"])
    before = dividends[dividends["date"] < latest_split["date"]].tail(8)
    if before.empty:
        return Finding(
            question, VERDICT_UNRESOLVED, "none", {"reason": "no dividends before the split"}
        )

    raw = pd.to_numeric(before["dividend"], errors="coerce")
    adjusted = pd.to_numeric(before["adjDividend"], errors="coerce")
    usable = (raw > 0) & (adjusted > 0)
    if not bool(usable.any()):
        return Finding(question, VERDICT_UNRESOLVED, "none", {"reason": "no usable pairs"})

    observed = float((raw[usable] / adjusted[usable]).median())

    if abs(observed - ratio) < 0.05 * ratio:
        verdict, confidence = "already_split_adjusted", "high"
    elif abs(observed - 1.0) < 0.05:
        verdict, confidence = "not_split_adjusted", "high"
    else:
        verdict, confidence = VERDICT_UNRESOLVED, "low"

    return Finding(
        question,
        verdict,
        confidence,
        {
            "ticker": ticker,
            "split_ex_date": str(latest_split["date"]),
            "split_ratio": ratio,
            "median_dividend_over_adjDividend": round(observed, 6),
            "interpretation": (
                "ratio ~= split factor means adjDividend is already split-adjusted; "
                "ratio ~= 1 means it is not"
            ),
        },
    )


def recommend(findings: list[Finding]) -> str:
    """Translate the findings into the setting apply_actions() needs.

    The convention question is: in what per-share units is `dividend` quoted?
    If adjDividend is already split-adjusted, then `dividend` is the raw
    as-declared amount in PRE-split units. If neither field is split-adjusted,
    `dividend` is in the units of its own era, which for a return computed
    against a same-era close is also pre-split.
    """
    by_verdict = {finding.question: finding.verdict for finding in findings}
    adj_dividend = by_verdict.get("Is adjDividend already split-adjusted?")
    ex_dated = by_verdict.get("Are dividends ex-dated or pay-dated?")

    if ex_dated not in ("ex_dated",):
        return (
            "DO NOT SET THE CONVENTION YET. The ex-date question is unresolved, "
            "and a pay-dated dividend applied at the ex-date close is wrong "
            "regardless of which split convention you pick."
        )
    if adj_dividend is None:
        return (
            "PARTIALLY RESOLVED. The ex-date is confirmed, but the "
            "split-adjustment of adjDividend was not measurable - that needs a "
            "ticker with both a split and dividends inside the window. Re-run "
            "with --ticker AAPL --years 8 (4:1 split, Aug 2020)."
        )
    if adj_dividend == "already_split_adjusted":
        return (
            "SIENNER_DIVIDEND_UNIT_CONVENTION=pre_split\n"
            "  adjDividend carries the split correction, so `dividend` is the "
            "as-declared pre-split amount and r_t = (C_t*k_t + D_t)/C_{t-1} - 1 "
            "is correct as written in the README."
        )
    if adj_dividend == "not_split_adjusted":
        return (
            "SIENNER_DIVIDEND_UNIT_CONVENTION=post_split\n"
            "  Neither field carries a split correction, so D_t must be scaled "
            "with the close: r_t = (C_t + D_t)*k_t/C_{t-1} - 1."
        )
    return "UNRESOLVED. Re-run against a ticker with both a split and dividends in the window."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--ticker",
        action="append",
        default=None,
        help="Repeatable. Defaults to NVDA (10:1 in 2024), AAPL, KO.",
    )
    parser.add_argument("--years", type=int, default=12)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("spike_verdict.json"),
        help="Where to write the machine-readable verdict.",
    )
    args = parser.parse_args(argv)
    tickers = args.ticker or ["NVDA", "AAPL", "KO"]

    all_findings: list[dict] = []
    for ticker in tickers:
        print(f"\n{'=' * 70}\n{ticker}\n{'=' * 70}")
        try:
            data = fetch_all(ticker, args.years)
        except httpx.HTTPStatusError as exc:
            reason = f"HTTP {exc.response.status_code}: {exc.response.text[:160]}"
            print(f"  {reason} - skipping")
            # Record it. An empty results list with no reason in the file is
            # useless to anyone reading it later, including me.
            all_findings.append({"ticker": ticker, "error": reason, "findings": []})
            continue
        except httpx.HTTPError as exc:
            # Transport-level: DNS, TLS, a proxy refusing the host, no route.
            # A traceback here tells you nothing you can act on.
            reason = f"unreachable: {type(exc).__name__}: {exc}"
            print(f"  cannot reach the provider: {type(exc).__name__}: {exc}")
            print(
                "  This machine has no route to the FMP API. The spike has to run "
                "somewhere that does."
            )
            all_findings.append({"ticker": ticker, "error": reason, "findings": []})
            continue

        print(
            f"  bars={len(data['bars'])} splits={len(data['splits'])} "
            f"dividends={len(data['dividends'])}"
        )
        findings = [
            question_1(data, ticker),
            question_2(data, ticker),
            question_3(data, ticker),
        ]
        for finding in findings:
            print(f"\n  Q: {finding.question}")
            print(f"  A: {finding.verdict}  (confidence: {finding.confidence})")
            for key, value in finding.evidence.items():
                print(f"       {key}: {value}")
        all_findings.append(
            {"ticker": ticker, "findings": [asdict(f) for f in findings]}
        )

    print(f"\n{'=' * 70}\nRECOMMENDATION\n{'=' * 70}")
    if all(entry.get("error") for entry in all_findings) and all_findings:
        print(
            "NO DATA WAS FETCHED - every ticker failed. The errors are above and\n"
            "now recorded in the verdict file. This is not a finding about FMP's\n"
            "field semantics; it is a connectivity or endpoint problem.\n\n"
            "Run the probe to find which routes your plan answers:\n"
            "    python -m sienner.scripts.fmp_probe"
        )
        args.out.write_text(
            json.dumps(
                {"tickers": tickers, "results": all_findings,
                 "recommendation": "no data fetched; see per-ticker errors"},
                indent=2, default=str,
            )
        )
        print(f"\nWritten to {args.out}")
        return 1
    flattened = [
        Finding(**finding)
        for entry in all_findings
        for finding in entry["findings"]
        if finding["verdict"] != VERDICT_UNRESOLVED
    ]
    recommendation = recommend(flattened)
    print(recommendation)

    args.out.write_text(
        json.dumps(
            {"tickers": tickers, "results": all_findings, "recommendation": recommendation},
            indent=2,
            default=str,
        )
    )
    print(f"\nWritten to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
