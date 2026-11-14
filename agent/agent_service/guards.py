"""Deterministic guards (SE-2, SE-4, PUB-08/09): run before any model or tool call.

The model is never a security control. These checks refuse prohibited requests outright and
scan every outgoing payload. Refusal texts are templated and contain no banking data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REFUSAL_READONLY = (
    "I can't do that. This assistant is read-only: I can show balances, transactions and "
    "spending summaries for your own accounts, but I cannot move money or change anything."
)
REFUSAL_OTHER_CUSTOMER = (
    "I can only show information for your own accounts. I can't access or discuss any other "
    "person's accounts or data."
)
REFUSAL_ENUMERATION = (
    "I can't list or reveal the bank's customers. I can only help with your own accounts."
)
REFUSAL_OVERRIDE = (
    "I can't change my rules. I can help with your own balances, transactions and spending."
)

# Action intent to move money or change state (SE-4). Questions about past spending
# ("how much did I pay for rent") must NOT match: patterns require an imperative/future form.
_WRITE_PATTERNS = [
    r"\btransfer\b(?!s? (?:in|from my history))",
    r"\bsend (?:aed|money|cash|\d)",
    r"\bmake (?:a |the )?payment\b",
    r"\bpay (?:aed )?\d",
    r"\bpay (?:him|her|them|my|the) (?!.*did)",
    r"\bmove (?:aed |money |\d)",
    r"\bwire\b",
    r"\bset up (?:a )?(?:standing order|direct debit)\b",
    r"\b(?:open|close|block|freeze|cancel) (?:my |the )?(?:account|card)\b",
    r"\bchange (?:my )?(?:pin|password|address|limit)\b",
]

# Requests for other people's or all customers' data (SE-2).
_OTHER_CUSTOMER_PATTERNS = [
    r"\b(?:another|other|different) (?:customer|person|user|client)s?\b",
    r"\bsomeone else'?s?\b",
    r"\bmy (?:wife|husband|friend|brother|sister|colleague)'?s?\b",
    r"\ball (?:the )?customers\b",
    r"\bevery customer\b",
    r"\bcustomer (?:list|ids|records|database)\b",
    r"\blist .*customers\b",
]

# "Omar Haddad's transactions": a capitalised name (not a month or "My") possessing a banking
# noun. Checked case-sensitively on the original message.
_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_POSSESSIVE_NAME_RE = re.compile(
    rf"\b(?!(?:{_MONTHS}|My|Mine|Today|Yesterday)\b)"
    r"[A-Z][a-z]+(?: [A-Z][a-z]+)*'s\s+"
    r"(?:transactions?|accounts?|balances?|spending|data|statements?|money|card)\b"
)

# Direct instruction-override attempts (PUB-09).
_OVERRIDE_PATTERNS = [
    r"\bignore (?:your|all|previous|the) (?:rules|instructions)\b",
    r"\bdisregard (?:your|all|previous|the)\b",
    r"\bsystem prompt\b",
    r"\b(?:developer|admin|administrator|debug|god) mode\b",
    r"\byou are now\b",
    r"\bpretend (?:to be|you are)\b",
    r"\bjailbreak\b",
    r"\bnew instructions\b",
    r"\boverride\b.*\b(?:rules|security|instructions)\b",
]

_ENUMERATION_PATTERNS = [
    r"\ball customers\b",
    r"\blist .*customers\b",
    r"\bcustomer (?:list|database)\b",
    r"\bother accounts in the bank\b",
]

_ACCOUNT_ID_RE = re.compile(r"\bACC-\d+\b")
_CUSTOMER_ID_RE = re.compile(r"\bC0\d{2,}\b")
_TOKEN_RE = re.compile(r"\btok_[A-Za-z0-9_]+\b")


@dataclass
class GuardVerdict:
    allowed: bool
    refusal_text: str = ""
    reason: str = ""


def check_message(message: str, owned_account_ids: set[str] | None = None) -> GuardVerdict:
    """Deterministic pre-model refusals. owned_account_ids, when known, catches ID probes."""
    lowered = message.lower()

    for pattern in _OVERRIDE_PATTERNS:
        if re.search(pattern, lowered):
            return GuardVerdict(False, REFUSAL_OVERRIDE, "override_attempt")
    for pattern in _ENUMERATION_PATTERNS:
        if re.search(pattern, lowered):
            return GuardVerdict(False, REFUSAL_ENUMERATION, "enumeration")
    if _POSSESSIVE_NAME_RE.search(message):
        return GuardVerdict(False, REFUSAL_OTHER_CUSTOMER, "other_customer")
    for pattern in _OTHER_CUSTOMER_PATTERNS:
        if re.search(pattern, lowered):
            return GuardVerdict(False, REFUSAL_OTHER_CUSTOMER, "other_customer")
    for pattern in _WRITE_PATTERNS:
        if re.search(pattern, lowered):
            return GuardVerdict(False, REFUSAL_READONLY, "write_attempt")

    if owned_account_ids is not None:
        for account_id in _ACCOUNT_ID_RE.findall(message):
            if account_id not in owned_account_ids:
                return GuardVerdict(False, REFUSAL_OTHER_CUSTOMER, "foreign_account_id")
    if _CUSTOMER_ID_RE.search(message):
        return GuardVerdict(False, REFUSAL_OTHER_CUSTOMER, "customer_id_mentioned")

    return GuardVerdict(True)


def scrub_output(text: str, owned_account_ids: set[str]) -> str:
    """Output-side guard: mask tokens and any identifier outside the customer's own set."""
    text = _TOKEN_RE.sub("[redacted]", text)
    text = _CUSTOMER_ID_RE.sub("[redacted]", text)
    return _ACCOUNT_ID_RE.sub(
        lambda m: m.group(0) if m.group(0) in owned_account_ids else "[redacted]", text
    )
