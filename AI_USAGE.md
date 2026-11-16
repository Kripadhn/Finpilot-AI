# AI usage

## Tools used

- **Claude Code** (Anthropic) as the coding agent, driven interactively from a staged brief that I
  wrote. The model changed during the work: early commits are co-authored by Claude Fable 5, later
  ones by Claude Sonnet 5.5 (see the `Co-Authored-By` trailers in `git log`).
- **Ollama** is the runtime model of the product (default `qwen3:8b`), not an authoring tool. At the
  time of the final evaluation run no model was installed on the development machine, so the
  committed report reflects a run without a model (`none`), not model quality.

## Where each tool was used

- `SPEC.md`, `ARCHITECTURE.md`, `docs/voice-auth-design.md` and this file: drafted by Claude Code
  from the assessment brief and the provided code. `SPEC.md` went in as its own first commit.
- `shared/` (settings, errors, redacting logs, fixed-clock dates, Decimal money, tracing, resilient
  API client), `mcp-server/`, `agent/`, `apps/` (two MCP Apps views), `host/` (thin chat host),
  `eval/harness/`, the 15 candidate cases, CI changes, Dockerfiles, compose and the README run
  section: written by Claude Code in stages, with lint, tests and a live run after each stage.
- Tests were designed against the seed generator's own trap list (refunds, pending and reversed
  rows, Arabic names, the injected description, pagination).

## What was changed or rejected

Found by running things, then fixed:

- **Workspace layout broke the provided mock API image.** I first made the new packages dependencies
  of the root project; the provided Dockerfile (`uv sync --no-dev`, only `pyproject.toml` and
  `uv.lock`) would then fail and would also pull every service into that image. Fixed by moving the
  packages into the dev group, copying the member `pyproject.toml` files in the Dockerfile, and
  checking each image's locked install in a scratch directory (Docker itself was not available).
- **The agent reported a model it never used.** The first evaluation report said `qwen3:8b` with
  0 tokens while no model was installed. `usage.model` is now `none` when no model call happened and
  `usage.model_calls` counts real calls; tests cover it.
- **A banking-service outage was labelled `session_invalid`.** It is now `upstream_unavailable`.
- **`SPEC.md` and `ARCHITECTURE.md` described CI replaying recorded model responses.** No fixtures
  exist, CI uses `LLM_PROVIDER=none`; both documents were corrected.
- **MCP SDK differences.** The installed SDK (2.x) renamed `FastMCP` to `MCPServer` and uses
  snake_case result fields; I read the installed code instead of relying on the 1.x API.
- **Harness trust.** A deliberately wrong case must fail all runs and trip the gate; it does.
- Four harness modules (`cases.py`, `checks.py`, `judge.py`, `models.py`) were already in the
  working tree when work resumed. I read them, kept them, and covered them with the harness tests.

Rejected on purpose:

- Putting the session token in tool arguments (simpler, but violates MC-3).
- Letting the model compute totals (violates MC-9); all amounts come from Decimal code.
- Loading a chart library from a CDN in the views (violates UI-8).
- Adopting an agent framework: the hand-rolled loop keeps the guards auditable (ADR-1).

## Verification limits

`make` and Docker were not available on the development machine, so the Makefile targets and the
compose services were not run there; the equivalent `uv run` commands were. No metric in the report
was produced by anything other than a harness run against the running services.
