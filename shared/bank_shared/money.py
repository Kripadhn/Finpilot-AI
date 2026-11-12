"""Decimal money helpers. Floats never touch money (MC-9)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .errors import InvalidInputError

TWO_PLACES = Decimal("0.01")


def amount(value: str | int | Decimal) -> Decimal:
    """Parse an upstream decimal string strictly."""
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise InvalidInputError(f"Not a valid amount: {value!r}") from error


def q2(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def to_number(value: Decimal) -> float:
    """JSON-facing number, quantized to 2 places (matches scripts/compute_truth.py)."""
    return float(q2(value))


def fmt_aed(value: Decimal) -> str:
    return f"AED {q2(value):,.2f}"
