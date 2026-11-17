"""usage must describe what really happened: no model call means model "none"."""

from __future__ import annotations

import pytest
from agent_service.llm import NullProvider, OllamaProvider, ReplayProvider, make_provider
from bank_shared.config import Settings
from bank_shared.errors import UpstreamUnavailableError

AISHA = "tok_aisha_valid"


async def test_usage_reports_model_calls_when_a_model_planned(agent, provider):
    provider.script(("get_transactions", {"account_id": "ACC-1001", "limit": 5}))
    response = await agent.run_turn(AISHA, None, "Show my last 5 transactions on my savings.")
    assert response.usage["model"] == "fake"
    assert response.usage["model_calls"] == 1
    assert response.usage["input_tokens"] == 10


async def test_usage_says_none_when_no_model_was_used(agent):
    agent._provider = NullProvider()
    response = await agent.run_turn(AISHA, None, "Show my last 5 transactions on my savings.")
    assert response.structured["outcome"] == "ok"  # deterministic routing still answers
    assert response.usage["model"] == "none"
    assert response.usage["model_calls"] == 0


async def test_refusals_never_claim_a_model(agent, provider):
    response = await agent.run_turn(AISHA, None, "Transfer 500 AED to Omar.")
    assert response.usage["model"] == "none"
    assert response.usage["model_calls"] == 0


async def test_null_provider_raises_a_safe_error():
    with pytest.raises(UpstreamUnavailableError):
        await NullProvider().chat([], [])


def test_make_provider_selects_by_configuration():
    assert isinstance(make_provider(Settings(llm_provider="none")), NullProvider)
    assert isinstance(make_provider(Settings(llm_provider="replay")), ReplayProvider)
    assert isinstance(make_provider(Settings(llm_provider="ollama")), OllamaProvider)
    with pytest.raises(ValueError):
        make_provider(Settings(llm_provider="mystery"))


async def test_model_tool_that_cannot_answer_is_replaced_by_the_deterministic_plan(agent, provider):
    # The model proposes a spending summary for a request that asks for a transaction list.
    provider.script(("spending_summary", {"from_date": "2026-10-01", "to_date": "2026-10-31"}))
    response = await agent.run_turn(AISHA, None, "Show my dining transactions in October.")
    names = [call["name"] for call in response.tool_calls if call["name"] != "list_accounts"]
    assert names and set(names) == {"get_transactions"}
    assert any(u["uri"] == "ui://bank/transactions" for u in response.ui_resources)
    assert response.usage["model_calls"] == 1  # the model was asked, then overruled


async def test_model_tool_that_fits_is_kept(agent, provider):
    provider.script(("spending_summary", {"from_date": "2026-09-01", "to_date": "2026-09-30"}))
    response = await agent.run_turn(AISHA, None, "How much did I spend last month?")
    names = [call["name"] for call in response.tool_calls if call["name"] != "list_accounts"]
    assert names == ["spending_summary"]


async def test_warm_up_never_stops_the_agent_when_ollama_is_down():
    provider = OllamaProvider(Settings(ollama_base_url="http://127.0.0.1:9", llm_timeout_s=1))
    await provider.warm_up()  # must swallow the connection error
    await provider.aclose()
