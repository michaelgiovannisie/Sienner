"""Integration tests against a real Postgres.

Skipped unless SIENNER_TEST_DATABASE_URL points at a database the tests may
create and drop tables in. With Compose running:

    SIENNER_TEST_DATABASE_URL=postgresql://sienner:<pw>@localhost:5432/sienner \
        pytest tests/test_postgres_integration.py

These exist because the in-memory repository cannot enforce what the schema
enforces. The correction path in particular has an ordering constraint that
only a real partial unique index will surface: the replacement row cannot be
inserted while the old row is still active, and the old row cannot reference a
replacement that does not exist yet. That deadlock passed every in-memory test.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from sienner.coverage import DateRange
from sienner.repository import PostgresRepository

DATABASE_URL = os.environ.get("SIENNER_TEST_DATABASE_URL")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set SIENNER_TEST_DATABASE_URL to run integration tests"
)


@pytest.fixture(scope="module")
def pool():
    from psycopg_pool import ConnectionPool

    connection_pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=2, open=True)
    connection_pool.wait(timeout=10.0)
    with connection_pool.connection() as conn:
        conn.execute(
            "DROP TABLE IF EXISTS quarantine, corporate_actions, "
            "daily_bars, fetch_coverage CASCADE"
        )
        conn.execute("DROP VIEW IF EXISTS corporate_actions_active CASCADE")
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            conn.execute(migration.read_text())
    yield connection_pool
    connection_pool.close()


@pytest.fixture
def repo(pool):
    with pool.connection() as conn:
        conn.execute("TRUNCATE quarantine, corporate_actions, daily_bars, fetch_coverage")
    return PostgresRepository(pool)


def bars(closes: list[float], start: date = date(2024, 1, 2)) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "bar_date": date.fromordinal(start.toordinal() + offset),
                "open": close, "high": close, "low": close, "close": close,
                "volume": 1_000,
            }
            for offset, close in enumerate(closes)
        ]
    ).astype({"volume": "int64"})


def split(ratio: float, day: date = date(2024, 6, 10)) -> pd.DataFrame:
    return pd.DataFrame(
        [{"ex_date": day, "action_type": "split", "split_ratio": ratio,
          "cash_dividend": None, "provider_adj_dividend": None}]
    )


# --- bars ------------------------------------------------------------------


def test_bars_round_trip(repo):
    assert repo.write_bars("AAPL", bars([100.0, 110.0]), source="fmp") == 2

    read = repo.read_bars("AAPL", DateRange(date(2024, 1, 1), date(2024, 1, 31)))
    assert read["close"].tolist() == [100.0, 110.0]


def test_re_writing_bars_is_a_no_op(repo):
    frame = bars([100.0, 110.0])
    repo.write_bars("AAPL", frame, source="fmp")

    assert repo.write_bars("AAPL", frame, source="fmp") == 0


def test_the_immutability_trigger_is_real(repo):
    repo.write_bars("AAPL", bars([100.0]), source="fmp")

    with pytest.raises(Exception, match="immutable"):
        with repo._pool.connection() as conn:
            conn.execute("UPDATE daily_bars SET close = 1 WHERE ticker = 'AAPL'")


# --- coverage --------------------------------------------------------------


def test_coverage_round_trips_and_merges(repo):
    repo.record_coverage(
        "AAPL", DateRange(date(2024, 1, 1), date(2024, 1, 5)), source="fmp", bars_written=3
    )
    repo.record_coverage(
        "AAPL", DateRange(date(2024, 1, 6), date(2024, 1, 9)), source="fmp", bars_written=2
    )

    assert repo.covered_ranges("AAPL") == [DateRange(date(2024, 1, 1), date(2024, 1, 9))]


# --- corporate actions: the correction path --------------------------------


def test_actions_round_trip(repo):
    assert repo.write_actions("NVDA", split(10.0), source="fmp") == 1

    read = repo.read_actions("NVDA", DateRange(date(2024, 6, 1), date(2024, 6, 30)))
    assert float(read["split_ratio"].iloc[0]) == 10.0


def test_identical_action_is_not_rewritten(repo):
    repo.write_actions("NVDA", split(10.0), source="fmp")

    assert repo.write_actions("NVDA", split(10.0), source="fmp") == 0


def test_a_corrected_ratio_supersedes_against_a_real_unique_index(repo):
    """The test that the in-memory repository could not have caught."""
    repo.write_actions("NVDA", split(10.0), source="fmp")
    assert repo.write_actions("NVDA", split(4.0), source="fmp") == 1

    active = repo.read_actions("NVDA", DateRange(date(2024, 6, 1), date(2024, 6, 30)))
    assert len(active) == 1
    assert float(active["split_ratio"].iloc[0]) == 4.0

    with repo._pool.connection() as conn:
        rows = conn.execute(
            "SELECT split_ratio, superseded_by IS NOT NULL, superseded_at IS NOT NULL "
            "FROM corporate_actions ORDER BY id"
        ).fetchall()

    assert len(rows) == 2, "the superseded row is kept, not deleted"
    assert float(rows[0][0]) == 10.0 and rows[0][1] and rows[0][2]
    assert float(rows[1][0]) == 4.0 and not rows[1][1]


def test_two_successive_corrections(repo):
    repo.write_actions("NVDA", split(10.0), source="fmp")
    repo.write_actions("NVDA", split(4.0), source="fmp")
    repo.write_actions("NVDA", split(2.0), source="fmp")

    active = repo.read_actions("NVDA", DateRange(date(2024, 6, 1), date(2024, 6, 30)))
    assert len(active) == 1
    assert float(active["split_ratio"].iloc[0]) == 2.0


def test_in_place_edit_is_refused_by_the_trigger(repo):
    repo.write_actions("NVDA", split(10.0), source="fmp")

    with pytest.raises(Exception, match="append-only"):
        with repo._pool.connection() as conn:
            conn.execute("UPDATE corporate_actions SET split_ratio = 4 WHERE ticker='NVDA'")


def test_un_superseding_is_refused(repo):
    """Two guards cover this, and the trigger's supersede-pointer check fires
    first. Either refusal is correct; what matters is that history cannot be
    rewound."""
    repo.write_actions("NVDA", split(10.0), source="fmp")
    repo.write_actions("NVDA", split(4.0), source="fmp")

    with pytest.raises(Exception, match="already superseded|un-superseded"):
        with repo._pool.connection() as conn:
            conn.execute(
                "UPDATE corporate_actions SET superseded_at = NULL, superseded_by = NULL "
                "WHERE split_ratio = 10"
            )


def test_clearing_only_the_superseded_marker_is_refused(repo):
    """Leaving superseded_by set while clearing superseded_at would make a
    superseded row active again, which the ordering CHECK exists to prevent."""
    repo.write_actions("NVDA", split(10.0), source="fmp")
    repo.write_actions("NVDA", split(4.0), source="fmp")

    with pytest.raises(Exception, match="un-superseded|corporate_actions_supersede_order"):
        with repo._pool.connection() as conn:
            conn.execute(
                "UPDATE corporate_actions SET superseded_at = NULL WHERE split_ratio = 10"
            )


# --- quarantine ------------------------------------------------------------


def test_quarantine_is_idempotent(repo):
    flags = pd.DataFrame(
        [{"ticker": "AAPL", "bar_date": date(2024, 1, 3), "rule": "split_shaped_move",
          "detail": "first", "observed_value": -0.33, "threshold": 1.5}]
    )
    assert repo.write_quarantine(flags) == 1

    second = flags.copy()
    second.loc[0, "detail"] = "second"
    repo.write_quarantine(second)

    stored = repo.read_quarantine("AAPL", DateRange(date(2024, 1, 1), date(2024, 1, 31)))
    assert len(stored) == 1
    assert stored["detail"].iloc[0] == "first", "first write wins; no silent overwrite"


def test_unknown_rule_is_refused(repo):
    flags = pd.DataFrame(
        [{"ticker": "AAPL", "bar_date": date(2024, 1, 3), "rule": "vibes",
          "detail": "looked odd", "observed_value": None, "threshold": None}]
    )
    with pytest.raises(Exception):
        repo.write_quarantine(flags)
