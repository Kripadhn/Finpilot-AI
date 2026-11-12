"""Error taxonomy. SafeError messages may reach the model, the user and the logs.

They therefore never contain tokens, keys, upstream response bodies, other customers'
identifiers, or hints that a foreign account exists.
"""

from __future__ import annotations


class SafeError(Exception):
    """An error whose message is safe to show to the agent, the user and the logs."""

    code = "error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SessionInvalidError(SafeError):
    """Expired, unknown, inactive or under-scoped session. Deliberately one message for all."""

    code = "session_invalid"

    def __init__(self, message: str = "The session is not valid. Please authenticate again."):
        super().__init__(message)


class NotOwnedError(SafeError):
    """Account missing or owned by someone else: one indistinguishable message (no oracle)."""

    code = "not_found"

    def __init__(self, message: str = "No such account was found for this customer."):
        super().__init__(message)


class InvalidInputError(SafeError):
    code = "invalid_input"


class UpstreamUnavailableError(SafeError):
    code = "upstream_unavailable"

    def __init__(self, message: str = "The banking service is temporarily unavailable."):
        super().__init__(message)


class ToolBudgetExceededError(SafeError):
    code = "tool_budget_exceeded"

    def __init__(self, message: str = "I stopped because this request needed too many lookups."):
        super().__init__(message)
