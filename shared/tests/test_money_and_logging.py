"""Unit tests for money helpers and log redaction."""

from __future__ import annotations

from decimal import Decimal

import pytest
from bank_shared.errors import InvalidInputError
from bank_shared.logging import mask_account_number, redact
from bank_shared.money import amount, fmt_aed, q2, to_number


def test_amount_parses_decimal_strings():
    assert amount("-410.00") == Decimal("-410.00")


def test_amount_rejects_garbage():
    with pytest.raises(InvalidInputError):
        amount("not-a-number")


def test_q2_rounds_half_up():
    assert q2(Decimal("1.005")) == Decimal("1.01")
    assert to_number(Decimal("2.675")) == 2.68


def test_fmt_aed():
    assert fmt_aed(Decimal("1234.5")) == "AED 1,234.50"


def test_redact_masks_tokens_and_ibans():
    line = redact("token tok_aisha_valid for AE123456789012345678901")
    assert "tok_aisha_valid" not in line
    assert "tok_***" in line
    assert line.endswith("AE…8901")


def test_redact_masks_configured_secrets():
    assert "service-key" not in redact("key=local-dev-service-key", ["local-dev-service-key"])


def test_mask_account_number_last_four():
    assert mask_account_number("AE123456789012345678901") == "…8901"
