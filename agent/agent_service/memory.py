"""Bounded server-side conversation state (AG-2).

A conversation is bound to the customer behind its session: the fingerprint is the sorted set
of owned account ids (derived server-side from introspection via list_accounts), so a
conversation can never be continued under a token that introspects to a different customer.
State is bounded: LRU-evicted, TTL-expired, history truncated.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from bank_shared.errors import SessionInvalidError


@dataclass
class Conversation:
    conversation_id: str
    fingerprint: str
    accounts: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, str]] = field(default_factory=list)  # {"role", "content"}
    last_displayed: dict[str, Any] | None = None  # exact result set shown to the customer
    pending_clarification: str | None = None  # what we asked the customer to disambiguate
    updated_at: float = field(default_factory=time.time)


def fingerprint_of(accounts: list[dict[str, Any]]) -> str:
    return ",".join(sorted(account["account_id"] for account in accounts))


class ConversationStore:
    def __init__(self, max_conversations: int, ttl_s: int, max_history_turns: int) -> None:
        self._store: OrderedDict[str, Conversation] = OrderedDict()
        self._max = max_conversations
        self._ttl = ttl_s
        self._max_history = max_history_turns

    def open(self, conversation_id: str | None, accounts: list[dict[str, Any]]) -> Conversation:
        """Get-or-create, enforcing the identity binding."""
        now = time.time()
        fingerprint = fingerprint_of(accounts)
        if conversation_id and conversation_id in self._store:
            conversation = self._store[conversation_id]
            if now - conversation.updated_at > self._ttl:
                del self._store[conversation_id]
            elif conversation.fingerprint != fingerprint:
                raise SessionInvalidError(
                    "This conversation belongs to a different session. Start a new conversation."
                )
            else:
                conversation.updated_at = now
                conversation.accounts = accounts
                self._store.move_to_end(conversation_id)
                return conversation
        conversation = Conversation(
            conversation_id=conversation_id or uuid.uuid4().hex,
            fingerprint=fingerprint,
            accounts=accounts,
        )
        self._store[conversation.conversation_id] = conversation
        while len(self._store) > self._max:
            self._store.popitem(last=False)
        return conversation

    def remember(self, conversation: Conversation, role: str, content: str) -> None:
        conversation.history.append({"role": role, "content": content})
        if len(conversation.history) > 2 * self._max_history:
            del conversation.history[: len(conversation.history) - 2 * self._max_history]
        conversation.updated_at = time.time()
