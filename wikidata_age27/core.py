"""Calendar-aware structured Wikidata date handling shared by both crawlers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Sequence


GREGORIAN = "Q1985727"
JULIAN = "Q1985786"
SUPPORTED_CALENDARS = {GREGORIAN, JULIAN}

CSV_BASE_COLUMNS = [
    "name",
    "wikipedia_url",
    "wikidata_id",
    "birth_date",
    "death_date",
    "cause_of_death",
    "manner_of_death",
    "age_status",
    "minimum_lifespan_days",
    "maximum_lifespan_days",
    "possible_age_range",
]

TIME_RE = re.compile(r"^(?P<sign>[+-]?)(?P<year>\d+)-(?P<month>\d{2})-(?P<day>\d{2})T")


class DataValidationError(RuntimeError):
    """Raised when structured data cannot be handled without guessing."""


@dataclass(frozen=True, order=True)
class CalendarDate:
    year: int
    month: int
    day: int
    calendar: str

    def to_jdn(self) -> int:
        a = (14 - self.month) // 12
        y = self.year + 4800 - a
        m = self.month + 12 * a - 3
        if self.calendar == GREGORIAN:
            return (
                self.day
                + (153 * m + 2) // 5
                + 365 * y
                + y // 4
                - y // 100
                + y // 400
                - 32045
            )
        if self.calendar == JULIAN:
            return self.day + (153 * m + 2) // 5 + 365 * y + y // 4 - 32083
        raise DataValidationError(f"Unsupported calendar model: {self.calendar}")


@dataclass(frozen=True)
class StructuredTime:
    raw: str
    precision: int
    calendar: str

    def components(self) -> tuple[int, int, int]:
        match = TIME_RE.match(self.raw)
        if not match:
            raise DataValidationError(f"Unrecognized Wikidata time value: {self.raw}")
        year = int(match.group("year"))
        if match.group("sign") == "-":
            year = -year
        return year, int(match.group("month")), int(match.group("day"))

    def bounds(self) -> tuple[CalendarDate, CalendarDate]:
        if self.calendar not in SUPPORTED_CALENDARS:
            raise DataValidationError(f"Unsupported calendar model: {self.calendar}")
        year, month, day = self.components()
        if self.precision == 11:
            _validate_date(year, month, day, self.calendar)
            value = CalendarDate(year, month, day, self.calendar)
            return value, value
        if self.precision == 10:
            _validate_date(year, month, 1, self.calendar)
            return (
                CalendarDate(year, month, 1, self.calendar),
                CalendarDate(year, month, days_in_month(year, month, self.calendar), self.calendar),
            )
        if self.precision == 9:
            return CalendarDate(year, 1, 1, self.calendar), CalendarDate(
                year, 12, 31, self.calendar
            )
        raise DataValidationError(
            f"Date precision {self.precision} is coarser than a year and cannot meet the age rule"
        )

    def display(self) -> str:
        year, month, day = self.components()
        year_text = f"{year:04d}" if year >= 0 else f"-{abs(year):04d}"
        if self.precision == 9:
            return year_text
        if self.precision == 10:
            return f"{year_text}-{month:02d}"
        if self.precision == 11:
            return f"{year_text}-{month:02d}-{day:02d}"
        return self.raw


@dataclass
class RawPerson:
    qid: str
    name: str = ""
    wikipedia_url: str = ""
    births: set[StructuredTime] = field(default_factory=set)
    deaths: set[StructuredTime] = field(default_factory=set)
    occupations: set[str] = field(default_factory=set)
    causes_of_death: set[str] = field(default_factory=set)
    manners_of_death: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class AgeRange:
    status: str
    minimum_days: int
    maximum_days: int
    minimum_age: tuple[int, int]
    maximum_age: tuple[int, int]

    def display(self) -> str:
        low = format_calendar_age(*self.minimum_age)
        high = format_calendar_age(*self.maximum_age)
        return low if low == high else f"{low} to {high}"


def is_leap_year(year: int, calendar: str) -> bool:
    if calendar == GREGORIAN:
        return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    if calendar == JULIAN:
        return year % 4 == 0
    raise DataValidationError(f"Unsupported calendar model: {calendar}")


def days_in_month(year: int, month: int, calendar: str) -> int:
    if month == 2:
        return 29 if is_leap_year(year, calendar) else 28
    if month in {4, 6, 9, 11}:
        return 30
    if month in {1, 3, 5, 7, 8, 10, 12}:
        return 31
    raise DataValidationError(f"Invalid month: {month}")


def _validate_date(year: int, month: int, day: int, calendar: str) -> None:
    if day < 1 or day > days_in_month(year, month, calendar):
        raise DataValidationError(f"Invalid date: {year}-{month:02d}-{day:02d}")


def anniversary(birth: CalendarDate, years: int) -> CalendarDate:
    year = birth.year + years
    day = min(birth.day, days_in_month(year, birth.month, birth.calendar))
    return CalendarDate(year, birth.month, day, birth.calendar)


def calendar_age(birth: CalendarDate, death: CalendarDate) -> tuple[int, int]:
    birth_jdn = birth.to_jdn()
    death_jdn = death.to_jdn()
    if death_jdn < birth_jdn:
        raise DataValidationError("Death precedes birth")
    estimate = max(0, death.year - birth.year)
    while estimate > 0 and anniversary(birth, estimate).to_jdn() > death_jdn:
        estimate -= 1
    while anniversary(birth, estimate + 1).to_jdn() <= death_jdn:
        estimate += 1
    return estimate, death_jdn - anniversary(birth, estimate).to_jdn()


def format_calendar_age(years: int, days: int) -> str:
    return (
        f"{years} {'year' if years == 1 else 'years'}, "
        f"{days} {'day' if days == 1 else 'days'}"
    )


def calculate_age_range(
    births: Iterable[StructuredTime], deaths: Iterable[StructuredTime]
) -> AgeRange | None:
    birth_bounds = [value.bounds() for value in births]
    death_bounds = [value.bounds() for value in deaths]
    if not birth_bounds or not death_bounds:
        raise DataValidationError("A person must have both birth and death statements")

    earliest_birth = min((start for start, _ in birth_bounds), key=CalendarDate.to_jdn)
    latest_birth = max((end for _, end in birth_bounds), key=CalendarDate.to_jdn)
    earliest_death = min((start for start, _ in death_bounds), key=CalendarDate.to_jdn)
    latest_death = max((end for _, end in death_bounds), key=CalendarDate.to_jdn)
    minimum_days = earliest_death.to_jdn() - latest_birth.to_jdn()
    maximum_days = latest_death.to_jdn() - earliest_birth.to_jdn()
    if minimum_days < 0 or maximum_days < minimum_days:
        raise DataValidationError("Birth/death uncertainty permits an invalid chronology")

    minimum_age = calendar_age(latest_birth, earliest_death)
    maximum_age = calendar_age(earliest_birth, latest_death)
    min_years, max_years = minimum_age[0], maximum_age[0]
    if min_years == max_years == 27:
        status = "confirmed"
    elif min_years >= 26 and max_years <= 28 and min_years <= 27 <= max_years:
        status = "possible"
    else:
        return None
    return AgeRange(status, minimum_days, maximum_days, minimum_age, maximum_age)


def qid_from_uri(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def batched(values: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def time_sort_key(value: StructuredTime) -> tuple[int, int, str]:
    start, _ = value.bounds()
    return start.to_jdn(), value.precision, value.raw
