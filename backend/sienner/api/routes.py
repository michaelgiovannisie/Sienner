from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from sienner import __version__
from sienner.api.schemas import (
    HealthOut,
    PointOut,
    QuarantineOut,
    RangeOut,
    SeriesOut,
)
from sienner.config import get_settings
from sienner.deps import get_series_service, storage_kind
from sienner.schemas import SchemaViolation
from sienner.service import NoDataAvailable, SeriesService
from sienner.universe import TickerRejected

router = APIRouter(prefix="/api")


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(
        status="ok",
        version=__version__,
        provider="fmp" if get_settings().fmp_api_key else "fmp (no key configured)",
        storage=storage_kind(),
        basis=(
            "price_return (dividend unit convention unresolved - run the FMP spike)"
            if get_settings().dividend_unit_convention == "unresolved"
            else "total_return"
        ),
    )


@router.get("/series", response_model=SeriesOut)
def series(
    ticker: str = Query(min_length=1, max_length=8),
    start: date = Query(description="Inclusive"),
    end: date = Query(description="Inclusive"),
    service: SeriesService = Depends(get_series_service),
) -> SeriesOut:
    try:
        result = service.get_series(ticker, start, end)
    except TickerRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except NoDataAvailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SchemaViolation as exc:
        # Cached data failed its declared schema. That is a server-side data
        # problem, not a bad request - 422 would blame the caller.
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return SeriesOut(
        ticker=result.ticker,
        requested=RangeOut(start=result.requested.start, end=result.requested.end),
        effective=RangeOut(start=result.effective.start, end=result.effective.end),
        basis=result.basis,
        disclaimer=result.disclaimer,
        base=get_settings().equity_curve_base,
        total_return=result.total_return,
        points=[PointOut(date=p.bar_date, value=p.value) for p in result.points],
        warnings=result.warnings,
        fetched_ranges=[
            RangeOut(start=r.start, end=r.end) for r in result.fetched_ranges
        ],
        quarantined=[
            QuarantineOut(date=q.bar_date, rule=q.rule, detail=q.detail)
            for q in result.quarantined
        ],
    )
