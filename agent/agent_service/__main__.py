"""Run the agent service: python -m agent_service (port 8003)."""

from __future__ import annotations

import os

import uvicorn
from bank_shared.config import settings


def main() -> None:
    uvicorn.run(
        "agent_service.main:app",
        host=os.getenv("AGENT_HOST", "127.0.0.1"),
        port=settings().agent_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
