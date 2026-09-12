"""Coverage range algebra.

Pure functions over closed date intervals. No database, no provider - which is
why the idempotency property the README asks for is testable directly rather
than inferred from an integration test.

The invariant: fetching [a, b] twice must issue provider calls the first time
and none the second.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

ONE_DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class DateRange:
    """A closed interval. Both ends are included."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"range end {self.end} precedes start {self.start}")

    def overlaps_or_touches(self, other: "DateRange") -> bool:
        return self.start <= other.end + ONE_DAY and other.start <= self.end + ONE_DAY


def merge_ranges(ranges: list[DateRange]) -> list[DateRange]:
    """Collapse overlapping and adjacent ranges.

    Adjacent counts as mergeable: [Jan 1, Jan 5] and [Jan 6, Jan 9] describe one
    continuous covered span, and keeping them separate would make the ledger
    grow without bound under repeated incremental fetches.
    """
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda r: (r.start, r.end))
    merged = [ordered[0]]
    for candidate in ordered[1:]:
        last = merged[-1]
        if candidate.start <= last.end + ONE_DAY:
            if candidate.end > last.end:
                merged[-1] = DateRange(last.start, candidate.end)
        else:
            merged.append(candidate)
    return merged


def missing_ranges(requested: DateRange, covered: list[DateRange]) -> list[DateRange]:
    """The sub-ranges of `requested` that no covered range accounts for.

    An empty result means the request is fully served from cache, which is what
    makes the tool work offline for any ticker-range already fetched.
    """
    gaps: list[DateRange] = []
    cursor = requested.start

    for span in merge_ranges(covered):
        if span.end < requested.start:
            continue
        if span.start > requested.end:
            break
        if span.start > cursor:
            gaps.append(DateRange(cursor, min(span.start - ONE_DAY, requested.end)))
        if span.end >= cursor:
            cursor = span.end + ONE_DAY
        if cursor > requested.end:
            return gaps

    if cursor <= requested.end:
        gaps.append(DateRange(cursor, requested.end))
    return gaps
