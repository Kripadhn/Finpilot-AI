"""Fixed-clock date-period resolution (AG-5).

All helpers take `today` explicitly; callers pass settings().fixed_today. Resolution is pure
code and unit-tested; the model never computes dates.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

MONTH_NAMES = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTH_ABBRS = {name.lower(): i for i, name in enumerate(calendar.month_abbr) if name}

_ISO_RANGE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s*(?:to|until|through|–|-)\s*(\d{4}-\d{2}-\d{2})")
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_MONTH_YEAR_RE = re.compile(r"\b([a-z]+)\s+(\d{4})\b")


def month_bounds(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def this_month(today: date) -> tuple[date, date]:
    return month_bounds(today.year, today.month)


def last_month(today: date) -> tuple[date, date]:
    year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return month_bounds(year, month)


def month_by_name(name: str, today: date, year: int | None = None) -> tuple[date, date] | None:
    """The most recent occurrence of the named month not after `today`, or the explicit year."""
    month = MONTH_NAMES.get(name.lower()) or MONTH_ABBRS.get(name.lower())
    if not month:
        return None
    if year is not None:
        return month_bounds(year, month)
    chosen_year = today.year if month <= today.month else today.year - 1
    return month_bounds(chosen_year, month)


def resolve_period(text: str, today: date) -> tuple[date, date] | None:
    """Resolve a relative or explicit period mentioned in `text` against the fixed clock.

    Returns (from_date, to_date) inclusive, or None when the text names no period.
    """
    lowered = text.lower()

    if match := _ISO_RANGE_RE.search(lowered):
        return date.fromisoformat(match.group(1)), date.fromisoformat(match.group(2))
    if "last month" in lowered or "previous month" in lowered:
        return last_month(today)
    if "this month" in lowered or "current month" in lowered:
        return this_month(today)
    if "yesterday" in lowered:
        day = today - timedelta(days=1)
        return day, day
    if "today" in lowered:
        return today, today
    if "this week" in lowered:
        start = today - timedelta(days=today.weekday())
        return start, today
    if "last week" in lowered:
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=6)
    if "this year" in lowered:
        return date(today.year, 1, 1), today
    if "last year" in lowered:
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if match := _MONTH_YEAR_RE.search(lowered):
        if period := month_by_name(match.group(1), today, year=int(match.group(2))):
            return period
    for word in re.findall(r"[a-z]+", lowered):
        if period := month_by_name(word, today):
            return period
    if match := _ISO_DATE_RE.search(lowered):
        day = date.fromisoformat(match.group(0))
        return day, day
    return None
