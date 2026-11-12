from datetime import date

import pytest

from cu_clubs.periods import active_periods


@pytest.mark.parametrize(
    ("day", "names"),
    [
        (date(2027, 1, 31), ["semester_1"]),
        (date(2027, 2, 1), ["semester_2"]),
        (date(2027, 5, 31), ["semester_2"]),
        (date(2027, 6, 1), ["semester_2", "summer"]),
        (date(2027, 6, 30), ["semester_2", "summer"]),
        (date(2027, 7, 1), ["summer"]),
        (date(2027, 8, 31), ["summer"]),
        (date(2027, 9, 1), ["semester_1"]),
    ],
)
def test_academic_boundaries_and_explicit_summer_overlap(day, names):
    assert [period.name for period in active_periods(day)] == names
