# Showcasing and packaging the product

What you need to run it, from least to most effort. Nothing here needs the Makefile.

## 1. Packaged run (no source code, prebuilt images)

Needs Docker Desktop (or Docker Engine with Compose) and, for the full demo, Ollama.

```bash
# once: a file with settings, and the model
cp .env.example .env
ollama pull qwen3:8b

# start the four services and wait until each is healthy
docker compose -f docker-compose.release.yml up -d --wait
# open http://localhost:5173

# stop (add -v to also delete the seeded bank data)
docker compose -f docker-compose.release.yml down
```

The images are published by `.github/workflows/release.yml` when a version tag is pushed
(`git tag v1.0.0 && git push origin v1.0.0`). Use `TAG=v1.0.0` and `IMAGE_REGISTRY=...` to pick a
version and registry. No Docker account is needed to pull or build; the base images are public.
From a source checkout, `docker compose up -d --build --wait` (or `make up`) builds the same
images locally.

Not tested yet: the release workflow and both compose files have not been run, because Docker was
not installed on the development machine. Do one clean run on a machine with Docker before a real
showcase.

## 2. The four services

| Service | Port | Role |
|---|---|---|
| mock-bank-api | 8001 | synthetic core banking system (replace with a real adapter for a client) |
| mcp-server | 8002 | the only path to banking data; enforces who the customer is |
| agent | 8003 | `POST /chat`; plans tool calls, refuses unsafe requests |
| host | 5173 | chat page; proxies `/chat` to the agent, so the browser only talks to this port |

Compose starts them in that order and restarts them if they crash. You do not run them in separate
terminals. Without Docker, use `make api`, `make mcp`, `make agent`, `make host`.

## 3. Showing it from another machine

By default every port is bound to 127.0.0.1, so only the presenter's own computer can reach it.

1. Set `BIND_ADDRESS=0.0.0.0` in `.env` to publish the host port on the network.
2. Set `DEMO_PASSWORD=<something>` (and optionally `DEMO_USER`). The page then asks for HTTP Basic
   credentials. The session menu exposes the test customers, so never publish it without a password.
3. Put HTTPS in front of port 5173 (a reverse proxy such as Caddy or nginx, or a tunnel). Example
   Caddyfile (untested here): `demo.example.com { reverse_proxy 127.0.0.1:5173 }`.

The agent, MCP server and mock API stay on the private network; only `/chat` and `/ui/tool` are
reachable, through the host.

## 4. Running it in an automated job

```bash
docker compose -f docker-compose.release.yml up -d --wait   # waits for the health checks
docker compose -f docker-compose.release.yml exec -T agent python -c "print('up')"
# ... run a task, e.g. the evaluation from a source checkout:  make eval CASES=eval/cases/public
docker compose -f docker-compose.release.yml down
```

A scheduler can use the exit codes: `up --wait` fails if a service never becomes healthy, and the
evaluation harness exits non-zero when isolation or injection resistance is below 100 %.
`scripts/ci_eval.sh` does the same without Docker and is what GitHub Actions runs.

## 5. Model and hardware

- The agent talks to Ollama (`LLM_PROVIDER=ollama`, `LLM_MODEL=qwen3:8b`, `OLLAMA_BASE_URL`). With
  Docker, the agent reaches Ollama on the host through `host.docker.internal:11434`.
- Measured on the development machine (CPU only, no GPU): one tool-selection call with
  `qwen3:8b` took about 44 seconds with thinking off and more than 300 seconds with thinking on.
  `LLM_THINK=false` is therefore the default. For a live demo use a machine with a GPU or a smaller
  model, or expect slow answers.
- `LLM_PROVIDER=none` runs without a model: a deterministic router picks the tools. It answers the
  common questions instantly and every number is still computed in code, but it is not the AI
  planning path and should not be presented as one.

## 6. Before a real client

- Replace the mock core banking API with an adapter to the client's system. The MCP server is the
  only component that calls it, so the change stays in `shared/bank_shared/bank_client.py` and
  `mcp-server/`.
- Replace the test session tokens with real tokens from the authentication step
  (`docs/voice-auth-design.md`); the MCP server already introspects the token on every call.
- Add TLS, authentication in front of the host, rate limiting and persistent conversation storage
  (see "Known limits" in `ARCHITECTURE.md`).
