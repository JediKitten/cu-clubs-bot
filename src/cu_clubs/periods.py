"""Owner-defined academic reporting windows; June belongs to two windows."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ReportingPeriod:
    name: str
    starts_on: date
    ends_before: date

    def contains(self, day: date) -> bool:
        return self.starts_on <= day < self.ends_before


def academic_periods(start_year: int) -> tuple[ReportingPeriod, ...]:
    """An academic year starts in September; all end bounds are exclusive."""
    next_year = start_year + 1
    return (
        ReportingPeriod("semester_1", date(start_year, 9, 1), date(next_year, 2, 1)),
        ReportingPeriod("semester_2", date(next_year, 2, 1), date(next_year, 7, 1)),
        ReportingPeriod("summer", date(next_year, 6, 1), date(next_year, 9, 1)),
    )


def active_periods(day: date) -> tuple[ReportingPeriod, ...]:
    start_year = day.year if day.month >= 9 else day.year - 1
    return tuple(period for period in academic_periods(start_year) if period.contains(day))
