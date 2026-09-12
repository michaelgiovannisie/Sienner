"""API response models.

`basis` is a field rather than prose so the frontend cannot accidentally
present a price return as a total return. Stage 1b changes the value, not the
contract.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class RangeOut(BaseModel):
    start: date
    end: date


class PointOut(BaseModel):
    date: date
    value: float


class SeriesOut(BaseModel):
    ticker: str
    requested: RangeOut
    effective: RangeOut
    basis: str = Field(description="price_return in Stage 1a, total_return from 1b")
    disclaimer: str
    base: float = Field(description="Index level at the effective start date")
    total_return: float = Field(description="Fraction, so 0.43 means +43%")
    points: list[PointOut]
    warnings: list[str] = []
    fetched_ranges: list[RangeOut] = []


class HealthOut(BaseModel):
    status: str
    version: str
    provider: str
    storage: str
