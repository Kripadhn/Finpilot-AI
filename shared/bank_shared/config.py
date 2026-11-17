"""Typed settings. Every knob comes from the environment (.env), never from code."""

from __future__ import annotations

from datetime import date
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Upstream mock core banking API
    mock_api_url: str = "http://localhost:8001"
    mock_service_key: str = ""
    mock_admin_key: str = ""

    # Fixed clock (AG-5). All relative dates resolve against this, never the wall clock.
    fixed_today: date = date(2026, 10, 31)

    # LLM
    llm_provider: str = "ollama"  # ollama | replay
    llm_model: str = "qwen3:8b"
    ollama_base_url: str = "http://localhost:11434"
    llm_timeout_s: float = 180.0
    # qwen3-style thinking: false (default) is far faster on CPU; true; or auto to omit the flag.
    llm_think: str = "false"
    replay_dir: str = "eval/replay"
    replay_record: bool = False  # with provider=ollama, also record fixtures for replay

    # Service endpoints
    mcp_url: str = "http://localhost:8002/mcp"
    mcp_port: int = 8002
    agent_port: int = 8003

    # HTTP client resilience (MC-6)
    http_timeout_s: float = 10.0
    http_connect_timeout_s: float = 5.0
    http_retries: int = 3  # retries after the first attempt, transient failures only
    http_backoff_base_s: float = 0.25

    # Agent loop
    max_tool_calls_per_turn: int = 6  # AG-8
    max_history_turns: int = 12
    max_conversations: int = 500
    conversation_ttl_s: int = 3600

    # Eval support: expose per-conversation safe logs so the harness can run no_leak on them.
    expose_safe_logs: bool = False

    # Observability. Standard OTEL_EXPORTER_OTLP_ENDPOINT is read by the SDK itself.
    otel_service_name: str = ""


@lru_cache
def settings() -> Settings:
    return Settings()
