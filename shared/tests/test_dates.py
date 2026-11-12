"""Unit tests for bank_shared.dates: fixed-clock resolution (AG-5)."""

from __future__ import annotations

from datetime import date

from bank_shared.dates import last_month, month_by_name, resolve_period, this_month

TODAY = date(2026, 10, 31)


def test_last_month_is_september_2026():
    assert last_month(TODAY) == (date(2026, 9, 1), date(2026, 9, 30))


def test_this_month_is_october_2026():
    assert this_month(TODAY) == (date(2026, 10, 1), date(2026, 10, 31))


def test_last_month_across_year_boundary():
    assert last_month(date(2026, 1, 15)) == (date(2025, 12, 1), date(2025, 12, 31))


def test_month_by_name_picks_most_recent_not_after_today():
    assert month_by_name("august", TODAY) == (date(2026, 8, 1), date(2026, 8, 31))
    assert month_by_name("october", TODAY) == (date(2026, 10, 1), date(2026, 10, 31))
    # November 2026 is after the fixed clock, so November means 2025.
    assert month_by_name("november", TODAY) == (date(2025, 11, 1), date(2025, 11, 30))


def test_resolve_period_phrases():
    assert resolve_period("how much last month?", TODAY) == (date(2026, 9, 1), date(2026, 9, 30))
    assert resolve_period("my transactions this month", TODAY) == (
        date(2026, 10, 1),
        date(2026, 10, 31),
    )
    assert resolve_period("spending for October", TODAY) == (date(2026, 10, 1), date(2026, 10, 31))
    assert resolve_period("biggest expense in August", TODAY) == (
        date(2026, 8, 1),
        date(2026, 8, 31),
    )
    assert resolve_period("in May 2026 please", TODAY) == (date(2026, 5, 1), date(2026, 5, 31))


def test_resolve_period_iso_and_ranges():
    assert resolve_period("from 2026-09-01 to 2026-09-15", TODAY) == (
        date(2026, 9, 1),
        date(2026, 9, 15),
    )
    assert resolve_period("on 2026-10-05", TODAY) == (date(2026, 10, 5), date(2026, 10, 5))


def test_resolve_period_none_when_no_period():
    assert resolve_period("what is my balance?", TODAY) is None
