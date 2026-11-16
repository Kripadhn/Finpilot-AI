# Build from the repository root: docker build -f mcp-server/Dockerfile .
FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.13.7

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY shared ./shared
COPY mcp-server ./mcp-server
COPY agent/pyproject.toml agent/pyproject.toml
COPY eval/harness/pyproject.toml eval/harness/pyproject.toml
RUN uv sync --locked --no-dev --package mcp-bank-server
COPY apps ./apps

ENV PATH="/app/.venv/bin:$PATH" MCP_HOST=0.0.0.0
EXPOSE 8002

HEALTHCHECK --interval=10s --timeout=3s --retries=6 \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8002), 2)"

CMD ["python", "-m", "mcp_bank_server"]
