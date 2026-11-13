"""Run the MCP server: python -m mcp_bank_server (Streamable HTTP on :8002/mcp)."""

from __future__ import annotations

import asyncio
import os

from bank_shared.config import settings
from bank_shared.logging import setup_logging
from bank_shared.tracing import setup_tracing

from .server import mcp


def main() -> None:
    config = settings()
    setup_logging("mcp-server", secrets=[config.mock_service_key, config.mock_admin_key])
    setup_tracing("mcp-bank-server")
    asyncio.run(
        mcp.run_streamable_http_async(
            host=os.getenv("MCP_HOST", "127.0.0.1"),
            port=config.mcp_port,
            streamable_http_path="/mcp",
            stateless_http=True,
        )
    )


if __name__ == "__main__":
    main()
