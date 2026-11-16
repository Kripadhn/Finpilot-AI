# Build from the repository root: docker build -f agent/Dockerfile .
FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.13.7

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY shared ./shared
COPY agent ./agent
COPY mcp-server/pyproject.toml mcp-server/pyproject.toml
COPY eval/harness/pyproject.toml eval/harness/pyproject.toml
RUN uv sync --locked --no-dev --package bank-agent

ENV PATH="/app/.venv/bin:$PATH" AGENT_HOST=0.0.0.0
EXPOSE 8003

HEALTHCHECK --interval=10s --timeout=3s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8003/health')"

CMD ["python", "-m", "agent_service"]
