"""Endpoint shape. Runs against a fake provider and in-memory storage, so the
suite needs neither an API key nor Postgres."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from sienner.config import Settings
from sienner.deps import get_series_service
from sienner.main import create_app
from sienner.repository import InMemoryRepository
from sienner.service import SeriesService
from tests.conftest import make_bars
from tests.test_service import RecordingProvider


@pytest.fixture
def client():
    app = create_app()
    provider = RecordingProvider(make_bars([100.0, 110.0, 99.0]))
    service = SeriesService(provider, InMemoryRepository(), Settings(fmp_api_key="k"))
    app.dependency_overrides[get_series_service] = lambda: service
    with TestClient(app) as test_client:
        yield test_client


def test_health_reports_version_storage_and_basis(client):
    body = client.get("/api/health").json()

    assert body["status"] == "ok"
    assert body["version"]
    assert body["storage"]
    # While the spike is outstanding, health says so rather than implying the
    # deployed tool is computing total returns.
    assert "unresolved" in body["basis"]


def test_series_returns_a_curve_indexed_to_100(client):
    response = client.get(
        "/api/series",
        params={"ticker": "aapl", "start": "2024-01-02", "end": "2024-01-04"},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["ticker"] == "AAPL"
    assert body["base"] == 100.0
    assert body["points"][0]["value"] == pytest.approx(100.0)
    assert body["points"][0]["date"] == "2024-01-02"
    assert body["total_return"] == pytest.approx(-0.01)


def test_series_labels_its_basis_and_disclaims_dividends(client):
    body = client.get(
        "/api/series",
        params={"ticker": "AAPL", "start": "2024-01-02", "end": "2024-01-04"},
    ).json()

    assert body["basis"] == "price_return"
    assert "dividends not included" in body["disclaimer"]


def test_out_of_universe_ticker_is_422_with_the_reason(client):
    response = client.get(
        "/api/series",
        params={"ticker": "BTCUSD", "start": "2024-01-02", "end": "2024-01-04"},
    )
    assert response.status_code == 422
    assert "24/7" in response.json()["detail"]


def test_window_with_no_bars_is_404(client):
    response = client.get(
        "/api/series",
        params={"ticker": "AAPL", "start": "2030-01-02", "end": "2030-01-04"},
    )
    assert response.status_code == 404


def test_inverted_window_is_400(client):
    response = client.get(
        "/api/series",
        params={"ticker": "AAPL", "start": "2024-03-01", "end": "2024-01-01"},
    )
    assert response.status_code == 400
