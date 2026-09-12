"""Range algebra. The property that matters: a second identical request must
produce no provider calls."""

from __future__ import annotations

from datetime import date

import pytest

from sienner.coverage import DateRange, merge_ranges, missing_ranges

D = date


def test_range_rejects_inverted_bounds():
    with pytest.raises(ValueError):
        DateRange(D(2024, 3, 1), D(2024, 1, 1))


def test_nothing_covered_means_the_whole_request_is_missing():
    requested = DateRange(D(2024, 1, 1), D(2024, 1, 31))
    assert missing_ranges(requested, []) == [requested]


def test_fully_covered_means_no_fetch():
    requested = DateRange(D(2024, 1, 10), D(2024, 1, 20))
    covered = [DateRange(D(2024, 1, 1), D(2024, 1, 31))]
    assert missing_ranges(requested, covered) == []


def test_exactly_covered_means_no_fetch():
    requested = DateRange(D(2024, 1, 1), D(2024, 1, 31))
    assert missing_ranges(requested, [requested]) == []


def test_gap_before_coverage():
    requested = DateRange(D(2024, 1, 1), D(2024, 1, 31))
    covered = [DateRange(D(2024, 1, 15), D(2024, 2, 28))]
    assert missing_ranges(requested, covered) == [DateRange(D(2024, 1, 1), D(2024, 1, 14))]


def test_gap_after_coverage():
    requested = DateRange(D(2024, 1, 1), D(2024, 1, 31))
    covered = [DateRange(D(2023, 12, 1), D(2024, 1, 15))]
    assert missing_ranges(requested, covered) == [DateRange(D(2024, 1, 16), D(2024, 1, 31))]


def test_hole_in_the_middle():
    requested = DateRange(D(2024, 1, 1), D(2024, 1, 31))
    covered = [
        DateRange(D(2024, 1, 1), D(2024, 1, 10)),
        DateRange(D(2024, 1, 20), D(2024, 1, 31)),
    ]
    assert missing_ranges(requested, covered) == [DateRange(D(2024, 1, 11), D(2024, 1, 19))]


def test_coverage_entirely_outside_the_request_is_ignored():
    requested = DateRange(D(2024, 6, 1), D(2024, 6, 30))
    covered = [
        DateRange(D(2020, 1, 1), D(2020, 12, 31)),
        DateRange(D(2030, 1, 1), D(2030, 12, 31)),
    ]
    assert missing_ranges(requested, covered) == [requested]


def test_adjacent_ranges_merge():
    """Without this, repeated incremental fetches grow the ledger forever and
    the gap computation gets slower every day."""
    merged = merge_ranges(
        [
            DateRange(D(2024, 1, 1), D(2024, 1, 5)),
            DateRange(D(2024, 1, 6), D(2024, 1, 9)),
        ]
    )
    assert merged == [DateRange(D(2024, 1, 1), D(2024, 1, 9))]


def test_overlapping_and_nested_ranges_merge():
    merged = merge_ranges(
        [
            DateRange(D(2024, 1, 1), D(2024, 1, 20)),
            DateRange(D(2024, 1, 5), D(2024, 1, 10)),
            DateRange(D(2024, 1, 18), D(2024, 2, 2)),
        ]
    )
    assert merged == [DateRange(D(2024, 1, 1), D(2024, 2, 2))]


def test_disjoint_ranges_do_not_merge():
    ranges = [
        DateRange(D(2024, 1, 1), D(2024, 1, 5)),
        DateRange(D(2024, 1, 8), D(2024, 1, 9)),
    ]
    assert merge_ranges(ranges) == ranges
