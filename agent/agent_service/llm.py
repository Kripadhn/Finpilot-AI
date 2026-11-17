"""LLM providers (AG-3): the provider/model comes from .env, never from code.

- `ollama`: native /api/chat with tool calling.
- `replay`: deterministic recorded responses for CI (EN-4); fixtures are keyed by a SHA-256
  of (model, messages, tools). With LLM_PROVIDER=ollama and REPLAY_RECORD=true, real
  responses are recorded so CI fixtures are refreshed with one command.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from bank_shared.config import Settings
from bank_shared.errors import UpstreamUnavailableError


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMReply:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    model: str = ""


def request_key(model: str, messages: list[dict], tools: list[dict]) -> str:
    canonical = json.dumps(
        {"model": model, "messages": messages, "tools": tools},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class OllamaProvider:
    def __init__(self, config: Settings) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.ollama_base_url, timeout=config.llm_timeout_s
        )

    async def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        started = time.perf_counter()
        try:
            body: dict = {
                "model": self._config.llm_model,
                "messages": messages,
                "tools": tools,
                "stream": False,
                "options": {"temperature": 0},
            }
            if self._config.llm_think.lower() in ("true", "false"):
                body["think"] = self._config.llm_think.lower() == "true"
            response = await self._client.post("/api/chat", json=body)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise UpstreamUnavailableError(
                "The language model is not reachable right now."
            ) from error
        payload = response.json()
        message = payload.get("message") or {}
        calls = [
            ToolCall(
                name=call["function"]["name"],
                arguments=call["function"].get("arguments") or {},
            )
            for call in message.get("tool_calls") or []
        ]
        reply = LLMReply(
            content=(message.get("content") or "").strip(),
            tool_calls=calls,
            input_tokens=payload.get("prompt_eval_count") or 0,
            output_tokens=payload.get("eval_count") or 0,
            latency_ms=round((time.perf_counter() - started) * 1000),
            model=self._config.llm_model,
        )
        if self._config.replay_record:
            _write_fixture(self._config, messages, tools, reply)
        return reply

    async def aclose(self) -> None:
        await self._client.aclose()


class ReplayProvider:
    """Deterministic CI provider: raises loudly when a fixture is missing."""

    def __init__(self, config: Settings) -> None:
        self._config = config

    async def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        key = request_key(self._config.llm_model, messages, tools)
        path = Path(self._config.replay_dir) / f"{key}.json"
        if not path.exists():
            raise UpstreamUnavailableError(
                "No recorded model response for this request (replay mode)."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return LLMReply(
            content=data.get("content", ""),
            tool_calls=[ToolCall(**call) for call in data.get("tool_calls", [])],
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            latency_ms=0,
            model=f"replay:{self._config.llm_model}",
        )

    async def aclose(self) -> None:  # interface parity
        return None


def _write_fixture(
    config: Settings, messages: list[dict], tools: list[dict], reply: LLMReply
) -> None:
    directory = Path(config.replay_dir)
    directory.mkdir(parents=True, exist_ok=True)
    key = request_key(config.llm_model, messages, tools)
    fixture = {
        "content": reply.content,
        "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in reply.tool_calls],
        "input_tokens": reply.input_tokens,
        "output_tokens": reply.output_tokens,
        # for human review of fixtures only:
        "_request": {"messages": messages, "tools": [t["function"]["name"] for t in tools]},
    }
    (directory / f"{key}.json").write_text(
        json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


class NullProvider:
    """No model: the agent plans with its deterministic router only (documented CI mode)."""

    async def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        raise UpstreamUnavailableError("No language model is configured (LLM_PROVIDER=none).")

    async def aclose(self) -> None:
        return None


def make_provider(config: Settings):
    if config.llm_provider == "none":
        return NullProvider()
    if config.llm_provider == "replay":
        return ReplayProvider(config)
    if config.llm_provider == "ollama":
        return OllamaProvider(config)
    raise ValueError(f"Unknown LLM_PROVIDER: {config.llm_provider} (use ollama, replay or none)")
