from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from sienner import __version__
from sienner.api.schemas import HealthOut, PointOut, RangeOut, SeriesOut
from sienner.config import get_settings
from sienner.deps import get_series_service, storage_kind
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
    )
