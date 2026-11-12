# AI usage

## Tools used

- **Claude Code** (Anthropic, model Claude Fable 5) as the coding agent, driven interactively
  from a staged instruction brief written by me.
- **Ollama** locally (qwen3:8b) as the runtime model of the system itself and for evaluation
  runs (this is the product's model, not an authoring tool, but recorded for completeness).

## Where each tool was used

- `SPEC.md`, `ARCHITECTURE.md`, `docs/voice-auth-design.md`, this file: drafted by Claude Code
  from the assessment brief and the provided code, reviewed and edited by me.
- Shared modules, MCP server, agent service, MCP Apps views, thin host, evaluation harness,
  candidate cases, CI changes, Docker/Make changes: implemented by Claude Code under a staged
  plan (spec first, then foundations, MCP server, agent, UI, harness, compose), with validation
  (`make lint`, `make test`, `make check-spec`, `make eval`) run after each stage.
- Test design (ownership isolation, expired sessions, pagination, refunds, pending/reversed
  exclusion, retries, Arabic payloads, injection payloads): co-designed; the trap list in the
  provided seed generator was used as the checklist.

## What was changed or rejected

- This file is kept honest incrementally: entries below are updated as stages land.
- Constraint kept against convenience: no agent framework was adopted even though one would have
  been faster to scaffold — the hand-rolled loop was required for auditable guards (ADR-1).
- Rejected: putting the session token into tool arguments (simpler to wire, violates MC-3);
  letting the model compute totals (violates MC-9); fetching chart libraries from a CDN in the
  views (violates UI-8).
- All AI-generated code was run and verified locally before commit; no test results or metrics
  are claimed that were not produced by actual runs.
