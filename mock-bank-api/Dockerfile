# Build from the repository root: docker build -f mock-bank-api/Dockerfile .
FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.13.7

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
# uv reads every workspace member's pyproject.toml, even for a --no-dev sync of the root only.
COPY shared/pyproject.toml shared/pyproject.toml
COPY mcp-server/pyproject.toml mcp-server/pyproject.toml
COPY agent/pyproject.toml agent/pyproject.toml
COPY eval/harness/pyproject.toml eval/harness/pyproject.toml
RUN uv sync --locked --no-dev
COPY mock-bank-api ./mock-bank-api

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8001

HEALTHCHECK --interval=10s --timeout=3s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/health')"

CMD ["sh", "-c", "[ -f data/bank.db ] || python mock-bank-api/seed/generate.py; exec uvicorn --app-dir mock-bank-api app.main:app --host 0.0.0.0 --port 8001"]
