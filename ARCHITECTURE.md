# Architecture

## Diagram

```
                 ┌────────────────────────────── trace_id (W3C traceparent) ─────────────────────────────┐
                 │                                                                                        │
 Browser ── :5173 Thin host (Vite + @mcp-ui/client)                                                       │
    │  POST /chat {session_token, conversation_id, message}                                               │
    ▼                                                                                                     ▼
 :8003 Agent service (FastAPI)                                :8002 MCP server (FastMCP,        :8001 Mock core banking API
 ┌──────────────────────────────┐   MCP Streamable HTTP        Streamable HTTP /mcp)            (provided, trusted, no isolation)
 │ 1 session gate               │   Authorization: Bearer ───► ┌─────────────────────────┐      ┌──────────────────────────┐
 │ 2 conversation binding      │   <session_token>            │ introspect EVERY call   │ ───► │ POST /sessions/introspect│
 │ 3 deterministic guards      │                              │ ownership check         │ ───► │ GET /customers/../accounts│
 │ 4 LLM plan (Ollama :11434)  │                              │ validate args           │ ───► │ GET /accounts/../transactions
 │ 5 MCP tool execution        │ ◄── structured + text ────── │ Decimal maths, paging   │      │ X-Service-Key, chaos      │
 │ 6 Decimal post-compute      │     + ui:// metadata         │ ui:// resources, prompt │      └──────────────────────────┘
 │ 7 output guard / redaction  │                              └─────────────────────────┘
 └──────────────────────────────┘
    │ ui_resources (ui://bank/…)             eval/harness ──► POST /chat (any case folder) ──► reports/
    ▼
 MCP Apps views (self-contained HTML, host bridge only)
```

## Key decisions (short ADRs)

**ADR-1: No agent framework.**
Context: guards, token binding, replay determinism and a tool-call budget need tight control.
Decision: hand-rolled loop (~small, readable) over Strands/LangGraph.
Consequence: we own the loop code, but nothing is hidden; AG-1/SE-* are auditable line by line.

**ADR-2: Identity lives only in the MCP server.**
Context: the mock API trusts its caller; the model is gullible.
Decision: session token rides the MCP HTTP `Authorization` header; every tool call introspects;
`customer_id` is derived server-side; no tool accepts it.
Consequence: a compromised/confused model still cannot cross customers (MC-3, SE-1, SE-2).

**ADR-3: Money is computed in code with `Decimal`.**
Context: LLM arithmetic is unreliable; refunds/pending/reversed rules are exact.
Decision: MCP server computes balances/summaries/largest-expense; agent reuses those numbers and
overrides `structured` with computed values.
Consequence: numeric accuracy depends on tested code, not sampling temperature (MC-9).

**ADR-4: Ollama-native client behind one interface, with replay and none providers.**
Context: reviewers run Ollama; CI cannot.
Decision: `LLM_PROVIDER=ollama|replay|none` behind one interface. `replay` keys recorded responses
by hash of (model, messages, tools) and `none` uses the deterministic router only. CI uses `none`
because no fixtures are recorded yet.
Consequence: deterministic CI (EN-4) that tests the pipeline and safety gates but not the model;
`usage.model` is `none` when no model call happened, so reports state what really ran.

**ADR-5: Self-contained HTML views, plain SVG chart.**
Context: UI-8 forbids undeclared network calls; reviewers must run offline.
Decision: one HTML file per view, no CDN, host bridge for data and tool calls.
Consequence: no build complexity in the demo path; chart is simpler than a chart library.

**ADR-6: Harness is plain Python + pytest-style checks, no eval framework.**
Context: the case schema is fixed and small; hidden cases must run unmodified.
Decision: implement the 14 check types directly; LLM judge via Ollama only for `llm_judge`.
Consequence: zero framework impedance with the provided schema (EV-1, EV-2).

## Trade-offs

- **Determinism over fluency**: pre-model guards refuse some phrasings a model might handle
  gracefully; chosen because SE-* failures are disqualifying while terse refusals are not.
- **Introspection on every tool call** costs one API round-trip per tool; correct per SE-1 and
  cheap against a local mock. A production system would cache with short TTL + revocation.
- **In-memory conversation state**: simple and bounded; loses history on restart and pins a
  conversation to one process. Acceptable for the assessment; Azure mapping addresses it.
- **Clarify instead of guessing accounts**: costs a turn, prevents wrong-account answers; PUB-02
  accepts either and hidden ambiguity cases are safer with clarification.
- **Model-free CI** validates the pipeline and safety gates, not model quality; evidence of model
  quality needs a harness run against real Ollama. `reports/report.md` is such a run (`qwen3:8b`,
  CPU only); `reports/report-deterministic.md` is the model-free run. Reports name the model used.

## Known limits

- Single-process services; no HA, no TLS, no rate limiting, no persistent sessions.
- Conversation memory is per-process; a restart mid-conversation loses follow-up context.
- The output guard masks identifier-shaped leaks (`C0xx`, `ACC-xxxx`, token-shaped strings); it
  cannot recognise free-text facts about other customers that carry no identifier — isolation
  therefore rests on the MCP server never returning such data in the first place.
- Date phrases outside the seeded window resolve correctly but return empty data.
- Arabic is preserved and rendered RTL; the agent does not guarantee conversing in Arabic.
- The LLM judge (only where a case demands it) inherits local-model variance; rubric and model
  are pinned and reported.

## Non-listed tools and why they were chosen

None. Every dependency is from the approved stack (uv, FastAPI, official `mcp` SDK, httpx,
Pydantic, OpenTelemetry, pytest, Ruff, Vite, `@mcp-ui/client`, Chart-free SVG).

## Future payment tool: explicit customer confirmation (human in the loop)

A hypothetical `create_payment` tool would never execute on model say-so:

1. **Propose**: the tool call only creates a *draft* payment server-side (amount, payee, source
   account, idempotency key) and returns a confirmation challenge rendered as an MCP Apps view
   showing the exact parsed details.
2. **Confirm out-of-band of the model**: the customer confirms in the UI; the confirmation goes
   host → agent → MCP server as a signed, single-use confirmation token tied to the draft's
   idempotency key and the session. Model output can neither create nor replay it; MCP
   elicitation is the transport. Drafts expire in minutes.
3. **Step-up auth** for amounts over a threshold or anomalous payees: re-verification (voice
   re-check or OTP) raises the session to a `payments:write` scope before execution.
4. **Execute once**: the server validates scope, ownership, limits, idempotency, then calls the
   core API; the result is written to an audit trail with the trace id. Every step is traced and
   the draft/confirm/execute are separate audited events.

## Azure mapping

### Agent hosting
Agent service and MCP server as containers on **Azure Container Apps** (scale-to-zero, revisions,
sidecar OTel collector), or the agent rebuilt on **Microsoft Foundry Agent Service** with
**Microsoft Agent Framework** for the loop; the local Ollama endpoint swaps for **Azure OpenAI**
/ Foundry Models via the same `LLM_PROVIDER` configuration seam.

### MCP gateway
**Azure API Management** fronts `https://…/mcp`: validates the customer's Entra-issued token,
enforces rate limits/quotas and IP policy, strips/injects headers, and forwards a verified
identity claim to the MCP server — introspection becomes JWT validation + an entitlement check.

### Agent identity
The agent and MCP server each run with a **Microsoft Entra Agent ID** (workload identity); the
MCP server calls the core banking API with managed-identity-issued tokens instead of
`X-Service-Key`. Customer identity stays a separate, delegated token — agent identity never
substitutes for customer identity.

### Secrets
**Azure Key Vault** with Container Apps secret references / CSI driver; no secrets in images or
env files; rotation by Key Vault versioning.

### Monitoring
OTLP → **Azure Monitor / Application Insights** (distributed traces keep the one-trace-per-turn
model), Log Analytics for redacted structured logs, alerts on isolation-guard rejections, error
rate and p95 latency; Langfuse-equivalent LLM analytics via App Insights custom events.

### Evaluation
The harness runs in **GitHub Actions / Azure DevOps** against a deployed staging revision
(deterministic mode for gating, scheduled real-model runs), publishing `report.json` to a dashboard;
**Azure AI Foundry evaluations** can host the LLM-judge rubrics. Session/conversation state moves
to **Azure Cosmos DB** or **Redis (Azure Cache)** to lift the single-process limit.

## AWS mapping (optional)

Amazon Bedrock AgentCore: Runtime hosts the agent, Gateway fronts the MCP server, Identity
replaces the service key with SigV4/workload identity, Memory replaces in-process conversation
state, Observability feeds CloudWatch/X-Ray; Strands Agents for the loop, Lambda for the
harness, Bedrock evaluation jobs for judged metrics.
