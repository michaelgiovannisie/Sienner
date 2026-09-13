"""Wiring. Kept apart from routes so tests can swap either half.

Storage degrades rather than failing: if Postgres is unreachable at startup the
API serves from an in-memory repository and says so at /api/health. For a
walking skeleton that means a broken database is a visible, diagnosable state
instead of a container that will not boot.
"""

from __future__ import annotations

from sienner.config import get_settings
from sienner.providers.fmp import FMPProvider
from sienner.repository import (
    InMemoryRepository,
    PostgresRepository,
    Repository,
)
from sienner.service import SeriesService

_repository: Repository | None = None
_storage_kind = "uninitialised"


def init_storage() -> None:
    """Called once at startup. Safe to call again."""
    global _repository, _storage_kind
    settings = get_settings()
    try:
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            open=True,
            timeout=10.0,
        )
        pool.wait(timeout=10.0)
        _repository = PostgresRepository(pool)
        _storage_kind = "postgres"
    except Exception as exc:  # noqa: BLE001
        _repository = InMemoryRepository()
        _storage_kind = f"in-memory (postgres unavailable: {type(exc).__name__})"


def storage_kind() -> str:
    return _storage_kind


def get_repository() -> Repository:
    global _repository
    if _repository is None:
        init_storage()
    assert _repository is not None
    return _repository


def get_series_service() -> SeriesService:
    return SeriesService(FMPProvider(), get_repository())
