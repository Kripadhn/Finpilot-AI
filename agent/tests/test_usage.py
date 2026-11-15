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
