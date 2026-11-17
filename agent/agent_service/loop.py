"""The agent loop (AG-1..AG-8): deterministic guards and computation around model planning.

Turn pipeline: session gate -> conversation binding -> deterministic refusals -> follow-up
fast path -> clarification -> model tool planning (sanitised) -> MCP execution within the
tool budget -> code-composed answer with exact tool numbers -> output scrub.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from bank_shared.config import Settings
from bank_shared.errors import SafeError, SessionInvalidError
from bank_shared.money import fmt_aed, to_number
from bank_shared.tracing import current_trace_id, tracer

from . import guards, intents
from .llm import LLMReply
from .mcp_client import McpTurnClient, ToolOutcome
from .memory import Conversation, ConversationStore

logger = logging.getLogger("agent_service.loop")
_tracer = tracer("agent_service")

PROMPT_VERSION = "v1"
_PROMPT_PATH = Path(__file__).parent / "prompts" / f"system_{PROMPT_VERSION}.md"

CLARIFY_TEXT = "You have more than one account: {options}. Which one do you mean?"
BUDGET_TEXT = (
    "I stopped because this request needed more data lookups than I allow in one turn "
    "({budget}). Please narrow the question, for example to one account or one period."
)


@dataclass
class TurnResponse:
    conversation_id: str
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    ui_resources: list[dict[str, Any]] = field(default_factory=list)
    structured: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""


class AgentLoop:
    def __init__(self, config: Settings, provider: Any, store: ConversationStore) -> None:
        self._config = config
        self._provider = provider
        self._store = store
        self._prompt_template = _PROMPT_PATH.read_text(encoding="utf-8")

    # ---------- public entry ----------

    async def run_turn(
        self, session_token: str, conversation_id: str | None, message: str
    ) -> TurnResponse:
        started = time.perf_counter()
        with _tracer.start_as_current_span("chat.turn") as span:
            span.set_attribute("agent.prompt_version", PROMPT_VERSION)
            response = await self._run_turn_inner(session_token, conversation_id, message)
            response.trace_id = current_trace_id()
            response.usage.setdefault("model", "none")
            response.usage.setdefault("model_calls", 0)
            response.usage["latency_ms"] = round((time.perf_counter() - started) * 1000)
            span.set_attribute("agent.outcome", response.structured.get("outcome", ""))
        return response

    async def _run_turn_inner(
        self, session_token: str, conversation_id: str | None, message: str
    ) -> TurnResponse:
        usage = {"input_tokens": 0, "output_tokens": 0}
        try:
            async with McpTurnClient(self._config, session_token or "") as mcp:
                return await self._authenticated_turn(mcp, conversation_id, message, usage)
        except SafeError as error:
            return TurnResponse(
                conversation_id=conversation_id or "",
                text=error.message,
                structured={"outcome": "error", "error": error.code},
                usage=usage,
            )

    # ---------- the pipeline ----------

    async def _authenticated_turn(
        self,
        mcp: McpTurnClient,
        conversation_id: str | None,
        message: str,
        usage: dict[str, Any],
    ) -> TurnResponse:
        # 1. Session gate + account facts. list_accounts introspects server-side (SE-1);
        #    an expired/unknown token fails here with no data.
        gate = await mcp.call("list_accounts", {})
        tool_calls = [_record(gate)]
        if not gate.ok or not gate.structured:
            unavailable = "temporarily unavailable" in gate.text
            return TurnResponse(
                conversation_id=conversation_id or "",
                text=gate.text or "The session is not valid. Please authenticate again.",
                tool_calls=tool_calls,
                structured={
                    "outcome": "error",
                    "error": "upstream_unavailable" if unavailable else "session_invalid",
                },
                usage=usage,
            )
        accounts = gate.structured["accounts"]
        owned_ids = {account["account_id"] for account in accounts}

        # 2. Conversation binding (AG-2): a different customer cannot continue this id.
        try:
            conversation = self._store.open(conversation_id, accounts)
        except SessionInvalidError as error:
            return TurnResponse(
                conversation_id="",
                text=error.message,
                tool_calls=tool_calls,
                structured={"outcome": "error", "error": "conversation_conflict"},
                usage=usage,
            )

        # 3. Deterministic refusals before any model call (SE-2, SE-4).
        verdict = guards.check_message(message, owned_ids)
        if not verdict.allowed:
            logger.info("guard refusal", extra={"status": verdict.reason})
            self._remember(conversation, message, verdict.refusal_text)
            return TurnResponse(
                conversation_id=conversation.conversation_id,
                text=verdict.refusal_text,
                tool_calls=tool_calls,
                structured={"outcome": "refusal", "reason": verdict.reason},
                usage=usage,
            )

        # 4. Follow-up fast path: "how much of that was fuel?" computes from the exact rows
        #    already shown, never by re-querying the period (AG-2, PUB-04).
        if conversation.last_displayed and intents.is_follow_up(message):
            category = intents.mentioned_category(message)
            if category:
                response = self._follow_up_from_displayed(conversation, message, category)
                response.tool_calls = tool_calls
                response.usage = usage
                return response

        # 5. Clarification before guessing an account (AG-6): a bare "balance" or "my account"
        #    with several accounts is ambiguous; a generic "my transactions" aggregates them.
        effective_message = self._merge_clarification(conversation, message)
        intent = intents.classify(effective_message)
        named = intents.resolve_account(effective_message, accounts)
        ambiguous = (
            named is None
            and len(accounts) > 1
            and not intents.wants_all_accounts(effective_message)
            and (
                intent.kind == "balance"
                or (
                    intent.wants_single_account
                    and intents.explicit_singular_account(effective_message)
                )
            )
        )
        if ambiguous:
            options = " or ".join(
                f"{account['type']} ({account['account_number_masked']})" for account in accounts
            )
            text = CLARIFY_TEXT.format(options=options)
            conversation.pending_clarification = effective_message
            self._remember(conversation, message, text)
            return TurnResponse(
                conversation_id=conversation.conversation_id,
                text=text,
                tool_calls=tool_calls,
                structured={"outcome": "clarification"},
                usage=usage,
            )
        conversation.pending_clarification = None

        # 6. Model plans the tools; code sanitises every argument (AG-5, SE-2).
        plan = await self._plan(mcp, conversation, effective_message, accounts, usage)

        # 7. Execute within the per-turn budget (AG-8).
        budget = self._config.max_tool_calls_per_turn
        outcomes: list[ToolOutcome] = []
        over_budget = len(plan) + len(tool_calls) > budget
        for name, arguments in plan[: max(0, budget - len(tool_calls))]:
            outcome = await mcp.call(name, arguments)
            outcomes.append(outcome)
            tool_calls.append(_record(outcome))
            if not outcome.ok and "session is not valid" in outcome.text:
                return TurnResponse(
                    conversation_id=conversation.conversation_id,
                    text=outcome.text,
                    tool_calls=tool_calls,
                    structured={"outcome": "error", "error": "session_invalid"},
                    usage=usage,
                )
        if over_budget:
            text = BUDGET_TEXT.format(budget=budget)
            self._remember(conversation, message, text)
            return TurnResponse(
                conversation_id=conversation.conversation_id,
                text=text,
                tool_calls=tool_calls,
                structured={"outcome": "error", "error": "tool_budget_exceeded"},
                usage=usage,
            )

        # 8. Compose the answer in code from exact tool numbers (MC-9), scrub, remember.
        response = await self._compose(
            mcp, conversation, effective_message, intent, outcomes, usage
        )
        response.tool_calls = tool_calls
        response.text = guards.scrub_output(response.text, owned_ids)
        self._remember(conversation, message, response.text)
        return response

    # ---------- planning ----------

    async def _plan(
        self,
        mcp: McpTurnClient,
        conversation: Conversation,
        message: str,
        accounts: list[dict[str, Any]],
        usage: dict[str, Any],
    ) -> list[tuple[str, dict[str, Any]]]:
        today = self._config.fixed_today
        reply = await self._ask_model(mcp, conversation, message, accounts, usage)
        plan: list[tuple[str, dict[str, Any]]] = []
        if reply:
            for call in reply.tool_calls:
                safe_args = intents.sanitize_tool_call(
                    call.name, call.arguments, message, accounts, today
                )
                if safe_args is None:
                    logger.info("dropped unsafe tool call", extra={"tool": call.name})
                    continue
                if call.name == "list_accounts":
                    continue  # already called as the session gate
                plan.append((call.name, safe_args))
        expected = intents.expected_tools(message)
        if plan and expected and not any(name in expected for name, _ in plan):
            # The model picked tools that cannot answer this request (for example a spending
            # summary for "show my dining transactions"): code decides, not the model.
            logger.info("model tool does not fit the request", extra={"status": "tool_mismatch"})
            plan = []
        if not plan:
            plan = intents.fallback_plan(message, accounts, today)
            if plan:
                logger.info("deterministic fallback plan", extra={"status": "fallback"})
        # the same tool with the same arguments never needs to run twice
        seen: set[str] = set()
        unique: list[tuple[str, dict[str, Any]]] = []
        for name, arguments in plan:
            key = f"{name}:{sorted(arguments.items())}"
            if key not in seen:
                seen.add(key)
                unique.append((name, arguments))
        return unique

    async def _ask_model(
        self,
        mcp: McpTurnClient,
        conversation: Conversation,
        message: str,
        accounts: list[dict[str, Any]],
        usage: dict[str, Any],
    ) -> LLMReply | None:
        today = self._config.fixed_today
        period = intents.period_args(message, today)
        period_hint = (
            f"The customer's period resolves to {period.get('from_date')}..{period.get('to_date')}."
            if period
            else "No explicit period was named."
        )
        account_lines = ", ".join(
            f"{account['type']} {account['account_id']} ({account['account_number_masked']})"
            for account in accounts
        )
        system = self._prompt_template.format(
            today=today.isoformat(), accounts=account_lines, period_hint=period_hint
        )
        messages = [{"role": "system", "content": system}]
        messages.extend(conversation.history[-2 * 6 :])
        messages.append({"role": "user", "content": message})
        try:
            with _tracer.start_as_current_span("llm.request") as span:
                reply = await self._provider.chat(messages, mcp.ollama_tools())
                span.set_attribute("llm.model", reply.model)
                span.set_attribute("llm.input_tokens", reply.input_tokens)
                span.set_attribute("llm.output_tokens", reply.output_tokens)
        except SafeError as error:
            logger.warning("model unavailable, using fallback", extra={"status": error.code})
            return None
        usage["input_tokens"] += reply.input_tokens
        usage["output_tokens"] += reply.output_tokens
        usage["model"] = reply.model
        usage["model_calls"] = usage.get("model_calls", 0) + 1
        return reply

    # ---------- composing answers ----------

    async def _compose(
        self,
        mcp: McpTurnClient,
        conversation: Conversation,
        message: str,
        intent: intents.Intent,
        outcomes: list[ToolOutcome],
        usage: dict[str, Any],
    ) -> TurnResponse:
        conversation_id = conversation.conversation_id
        ok_outcomes = [outcome for outcome in outcomes if outcome.ok and outcome.structured]
        failed = [outcome for outcome in outcomes if not outcome.ok]
        if not outcomes:
            text = (
                "I can help with balances, transactions and spending summaries for your own "
                "accounts. Could you rephrase what you would like to see?"
            )
            return TurnResponse(conversation_id, text, structured={"outcome": "no_action"})
        if not ok_outcomes:
            text = failed[0].text or "The banking service is temporarily unavailable."
            return TurnResponse(
                conversation_id, text, structured={"outcome": "error", "error": "tool_failed"}
            )

        lines: list[str] = []
        structured: dict[str, Any] = {"outcome": "ok"}
        ui_resources: list[dict[str, Any]] = []

        balances: dict[str, float] = {}
        transaction_outcomes: list[ToolOutcome] = []
        for outcome in ok_outcomes:
            data = outcome.structured
            if outcome.name == "get_balance":
                balances[data["account_id"]] = data["balance"]
                lines.append(
                    f"Your {data['type']} account ({data['account_number_masked']}) balance is "
                    f"{fmt_aed(Decimal(str(data['balance'])))}."
                )
            elif outcome.name == "get_transactions":
                transaction_outcomes.append(outcome)
            elif outcome.name == "spending_summary":
                lines.append(_spending_text(message, intent, data))
                structured.update(
                    {
                        "amount": data.get("amount"),
                        "total": data.get("total"),
                        "by_category": data.get("by_category"),
                        "count": data.get("count"),
                        "currency": data.get("currency", "AED"),
                        "largest_expense": data.get("largest_expense"),
                        "period": data.get("period"),
                    }
                )
                if intent.kind == "largest" and data.get("largest_expense"):
                    structured["amount"] = data["largest_expense"]["amount"]
                if outcome.ui_resource:
                    ui_resources.append(await _ui_entry(mcp, outcome))

        if transaction_outcomes:
            merged = _merge_transactions(transaction_outcomes, message)
            lines.append(merged["text"])
            lines.extend(_transaction_lines(merged["items"]))
            structured["count"] = merged["count"]
            structured["period"] = merged["period"]
            structured["category"] = merged["category"]
            conversation.last_displayed = {
                "tool": "get_transactions",
                "arguments": dict(transaction_outcomes[0].arguments),
                "items": merged["items"],
            }
            first = transaction_outcomes[0]
            if first.ui_resource:
                entry = await _ui_entry(mcp, first)
                entry["result"]["structuredContent"] = merged["view"]
                ui_resources.append(entry)

        if balances:
            structured["balances"] = balances
            if len(balances) == 1:
                structured["amount"] = next(iter(balances.values()))
        if failed:
            lines.append(failed[0].text)

        return TurnResponse(
            conversation_id=conversation_id,
            text="\n".join(line for line in lines if line),
            ui_resources=ui_resources,
            structured=structured,
            usage=usage,
        )

    def _follow_up_from_displayed(
        self, conversation: Conversation, message: str, category: str
    ) -> TurnResponse:
        """Decimal computation over the exact displayed rows (PUB-04)."""
        displayed = conversation.last_displayed or {}
        items = displayed.get("items", [])
        category_of = {
            row["transaction_id"]: row["category"] for row in items if row.get("transaction_id")
        }
        total = Decimal(0)
        count = 0
        for row in items:
            if row.get("status") != "posted":
                continue
            amount = Decimal(str(row["amount"]))
            if amount < 0 and row.get("category") == category:
                total -= amount
                count += 1
            elif (
                row.get("category") == "refund"
                and amount > 0
                and category_of.get(row.get("reference")) == category
            ):
                total -= amount
        amount_number = to_number(total)
        text = (
            f"Of the {len(items)} transactions shown, {fmt_aed(total)} was {category} "
            f"({count} transaction{'s' if count != 1 else ''})."
        )
        if count == 0 and amount_number == 0:
            text = f"None of the {len(items)} transactions shown were {category}."
        response = TurnResponse(
            conversation_id=conversation.conversation_id,
            text=text,
            structured={
                "outcome": "ok",
                "intent": "follow_up",
                "category": category,
                "amount": amount_number,
                "count": count,
                "currency": "AED",
            },
        )
        self._remember(conversation, message, text)
        return response

    def _merge_clarification(self, conversation: Conversation, message: str) -> str:
        pending = conversation.pending_clarification
        if pending and re.search(r"\b(savings|current|both|all|ACC-\d+)\b", message, re.IGNORECASE):
            return f"{pending} ({message})"
        return message

    def _remember(self, conversation: Conversation, user_message: str, reply: str) -> None:
        self._store.remember(conversation, "user", user_message)
        self._store.remember(conversation, "assistant", reply)


# ---------- helpers ----------


def _record(outcome: ToolOutcome) -> dict[str, Any]:
    return {
        "name": outcome.name,
        "arguments": outcome.arguments,
        "duration_ms": outcome.duration_ms,
        "ok": outcome.ok,
    }


def _merge_transactions(outcomes: list[ToolOutcome], message: str) -> dict[str, Any]:
    """One logical result set from one or several per-account get_transactions calls."""
    items: list[dict[str, Any]] = []
    for outcome in outcomes:
        items.extend(outcome.structured.get("items", []))
    items.sort(
        key=lambda row: (row.get("booked_at", ""), row.get("transaction_id", "")), reverse=True
    )
    overall_limit = intents.mentioned_limit(message)
    if overall_limit:
        items = items[:overall_limit]

    first = outcomes[0].structured
    period = first.get("period")
    category = first.get("category")
    if len(outcomes) == 1:
        text = outcomes[0].text
        account = first.get("account")
    else:
        account = None
        scope_bits = [f"{len(outcomes)} accounts"]
        if category:
            scope_bits.append(f"category {category}")
        if period and (period.get("from") or period.get("to")):
            scope_bits.append(f"period {period.get('from') or '…'} to {period.get('to') or '…'}")
        text = (
            f"{len(items)} transaction(s) across {', '.join(scope_bits)} (newest first)."
            if items
            else f"No transactions found across {', '.join(scope_bits)}."
        )
    return {
        "items": items,
        "count": len(items),
        "period": period,
        "category": category,
        "text": text,
        "view": {
            "account": account,
            "period": period,
            "category": category,
            "count": len(items),
            "items": items,
            "next_cursor": None,
        },
    }


def _transaction_lines(items: list[dict[str, Any]], cap: int = 25) -> list[str]:
    lines = []
    for row in items[:cap]:
        amount = fmt_aed(Decimal(str(row["amount"])))
        lines.append(
            f"- {row['booked_at'][:10]} · {row['merchant_name']} · {row['category']} · "
            f"{row['status']} · {amount}"
        )
    if len(items) > cap:
        lines.append(f"… and {len(items) - cap} more (shown in the table view).")
    return lines


def _spending_text(message: str, intent: intents.Intent, data: dict[str, Any]) -> str:
    period = data.get("period") or {}
    period_text = (
        f"{period.get('from')} to {period.get('to')}"
        if period.get("from") or period.get("to")
        else "all history"
    )
    largest = data.get("largest_expense")
    if intent.kind == "largest" and largest:
        return (
            f"Your biggest expense ({period_text}) was {largest['merchant_name']}: "
            f"{fmt_aed(Decimal(str(largest['amount'])))} on {largest['booked_at'][:10]} "
            f"({largest['category']})."
        )
    if data.get("category"):
        return (
            f"You spent {fmt_aed(Decimal(str(data['amount'])))} on {data['category']} "
            f"({period_text})."
        )
    by_category = data.get("by_category") or {}
    top = sorted(((cat, val) for cat, val in by_category.items() if val), key=lambda kv: -kv[1])[:3]
    top_text = ", ".join(f"{cat} {fmt_aed(Decimal(str(val)))}" for cat, val in top)
    text = f"Your total spending ({period_text}) was {fmt_aed(Decimal(str(data['total'])))}."
    if top_text:
        text += f" Largest categories: {top_text}."
    if not by_category or all(not val for val in by_category.values()):
        text = f"No spending found for this period ({period_text})."
    return text


async def _ui_entry(mcp: McpTurnClient, outcome: ToolOutcome) -> dict[str, Any]:
    """A ui_resources entry that also carries what a thin host needs to render the view."""
    mime, html = await mcp.read_view(outcome.ui_resource)
    return {
        "uri": outcome.ui_resource,
        "tool": outcome.name,
        "mimeType": mime,
        "html": html,
        "result": {
            "content": [{"type": "text", "text": outcome.text}],
            "structuredContent": outcome.structured,
        },
    }
