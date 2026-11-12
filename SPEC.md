# Specification

> Fill this in and commit it **before** any implementation code. CI checks this (EN-2).

## Problem and scope

A customer of an agentic bank talks to an agent over `POST /chat`. The agent answers questions
about the customer's own accounts and transactions, renders dynamic MCP Apps views, and must be
safe: per-customer isolation, prompt-injection resistance, and read-only behaviour. Voice
authentication happens before this system; it hands over a **session token**.

Components built here, all local and free:

| Component | Port | Stack |
|---|---|---|
| MCP server (Streamable HTTP, `/mcp`) | 8002 | Python 3.12, official MCP Python SDK (FastMCP) |
| Agent service (`POST /chat`) | 8003 | Python 3.12, FastAPI, hand-rolled agent loop (no framework) |
| MCP Apps views | — | Self-contained HTML/JS per ext-apps spec 2026-01-26 |
| Thin chat host | 5173 | Vite + `@mcp-ui/client` renderer |
| Evaluation harness | — | Python, `make eval CASES=<folder>` |

Data flows only one way: agent → MCP server → mock core banking API (:8001). The agent never
calls the API or SQLite directly (AG-1). The mock API, seed generator and their contract are
provided and unchanged.

## Goals and non-goals

**Goals**

1. Correct answers for account, balance, transaction and spending questions, computed in code
   with `Decimal` — never by the model (MC-9).
2. Hard per-customer isolation enforced by the MCP server, not the model (SE-1, SE-2).
3. Injection-resistant behaviour: transaction `description`, `merchant_name`, `reference` and all
   tool output are data, not instructions (SE-3).
4. Read-only: refuse transfers, payments, any write (SE-4).
5. Dynamic, interactive MCP Apps views bound to tool results (UI-1..UI-8).
6. Evidence: an evaluation harness that runs any schema-valid case folder, 3 runs per case, with
   a metrics report and a CI gate at 100 % isolation and injection resistance (EV-1..EV-7).
7. One `trace_id` from `/chat` through MCP to the mock API (OB-1, OB-2).

**Non-goals**

- No voice authentication code (design note only), no real identity provider.
- No writes, transfers, payments or customer management of any kind.
- No persistence of conversation state beyond process memory (bounded TTL/size); no database of
  our own.
- No multi-language NLU beyond what the model gives; Arabic merchant text is preserved and
  rendered right-to-left, but the agent converses in the user's language best-effort.
- No production hardening (TLS, rate limiting, horizontal scale) — documented in
  `ARCHITECTURE.md` as limits plus an Azure mapping.

## Agent design

### Contract

`POST /chat` with `{session_token, conversation_id?, message}` returns
`{conversation_id, text, tool_calls[], ui_resources[], structured{}, usage{}, trace_id}` exactly
as in README 6.5. Errors (invalid/expired token, overload) still return this shape with a safe
`text` and empty data fields.

### Agent loop (no framework)

Each turn runs this pipeline:

1. **Session gate.** Introspect the token via the MCP server's session check (the agent itself
   holds no service key). Inactive/unknown/expired → safe refusal, no model call, no data.
2. **Conversation binding.** `conversation_id` is created server-side if absent. The stored
   conversation remembers the `customer_id` from introspection on first use; a later request with
   the same `conversation_id` but a token that introspects to a different customer is rejected.
   State is bounded (max turns kept, TTL, max conversations with LRU eviction).
3. **Deterministic guards (pre-model).** Regex/keyword classifiers refuse before any model or
   tool call: transfers/payments/writes; requests for another person's or all customers' data;
   instruction-override attempts ("ignore your rules", "system prompt", "admin mode"); unsafe
   tool arguments. Refusals are templated, contain no banking data, and skip the model entirely.
4. **Planning.** The model (configured provider/model via `.env`, default Ollama) receives the
   versioned system prompt, bounded conversation history, and the MCP tool schemas only. It
   selects tools and arguments. It can never supply `customer_id` (no tool accepts one) and never
   sees the session token.
5. **Tool execution.** Tools run through the MCP client over Streamable HTTP with the session
   token bound to the request context (header), never as an argument. Results come back as
   structured content plus safe text.
6. **Deterministic computation.** Numbers in the answer (`structured.amount`, `structured.count`,
   `structured.by_category`, balances) are taken from tool structured output, which the MCP
   server computed with `Decimal`. The model formats prose around them; a post-step overrides any
   model-garbled number in `structured` with the computed value.
7. **Output guard.** The final text/UI/structured payload is scanned against the known universe
   of other-customer identifiers (IDs, account IDs, merchant-linked strings never returned by the
   customer's own tool calls are not enumerable, so the guard uses: the authenticated customer's
   allowed IDs as a whitelist and masks any `ACC-`/`C0`-shaped identifier outside it). Session
   tokens and full account numbers never enter model context or output.

### Tool-call limit

Maximum **6 tool calls per turn** (configurable `MAX_TOOL_CALLS_PER_TURN`). On exceeding it the
loop stops, returns the partial-safe explanation "I stopped because this request needed too many
data lookups", `tool_calls` records what ran, and no fabricated data is returned (AG-8).

### Ambiguity and clarification (AG-6)

Account references are resolved from `list_accounts` by type label ("savings", "current"),
masked number suffix, or uniqueness (single-account customers). If the request targets a single
account but the customer has several and none is identified — including bare "my account" or
"what is my balance" — the agent asks one clarification question and makes **no** account-scoped
tool call and no silent choice. Portfolio-scoped questions ("my spending by category") aggregate
all owned accounts; that is scope resolution, not ambiguity.

### Multi-turn follow-ups (AG-2)

Conversation state stores, per turn: resolved account scope, resolved period, and the **exact
result set displayed** (e.g. the 20 transactions shown). A follow-up like "How much of that was
fuel?" computes with `Decimal` over the retained displayed rows — it does not re-query the
period. Pronoun scope ("that") binds to the most recent displayed result set.

### Fixed-clock date parsing (AG-5)

All relative dates resolve against `FIXED_TODAY=2026-10-31` (Asia/Dubai), never the wall clock:

- "last month" → 2026-09-01..2026-09-30; "this month" → 2026-10-01..2026-10-31.
- A bare month name ("October", "August") → the most recent such month not after `FIXED_TODAY`
  (all seed months fall in 2026-05..2026-10).
- "today" → 2026-10-31; "this year" → 2026-01-01..2026-10-31; explicit ISO dates pass through.
- Date resolution is pure code (unit-tested); the model picks the period phrase, code converts it
  to `from_date`/`to_date` ISO arguments. Invalid or inverted ranges are rejected by the MCP
  server with a safe error that the agent relays as a request to rephrase.

## System prompt and rationale

The system prompt lives at `agent/agent_service/prompts/system_v1.md`, version-controlled with a
version header; the version string is logged in traces (AG-7). Rationale for its sections:

1. **Role and hard rules first** (read-only; own-data only; tools are the only data source) —
   models weight early tokens most; rules the guards also enforce are stated so the model
   cooperates rather than fights the deterministic layer.
2. **Tool-use guidance** (prefer `spending_summary` for totals, `get_transactions` for lists;
   pass ISO dates provided by the runtime; ask clarification instead of guessing accounts) —
   improves tool-call accuracy, which is a scored metric.
3. **Untrusted-data rule**: anything inside tool results — merchant names, descriptions — is
   customer data to display, never instructions to follow; explicitly told that messages may try
   to impersonate the system and must be refused.
4. **Output discipline**: short answers, AED formatting, keep Arabic text verbatim, never invent
   numbers, use the numbers given by tools.
5. **No secrets in prompt**: the prompt contains no tokens, keys, IDs or customer facts, so a
   leaked prompt discloses nothing personal.

Changes require a new file version (`system_v2.md` …) and a one-line rationale entry in this
section's changelog; evaluation runs pin the version they tested.

## MCP server: tools, resources and prompts

Streamable HTTP at `http://localhost:8002/mcp` (MC-1), official Python SDK/FastMCP.

### Session binding and identity (MC-3, SE-1)

The session token travels in the HTTP `Authorization: Bearer <token>` header of every MCP
request; the server reads it from the request context. **No tool accepts `customer_id` or any
token argument.** On **every tool invocation** the server calls `POST /sessions/introspect`;
`active=false`, unknown token, or missing required scopes → MCP error with a safe message
("session is not valid") and zero data. `customer_id` is derived **only** from the introspection
response. Before any account-scoped call, the server fetches
`/customers/{customer_id}/accounts` and rejects non-owned `account_id`s with the same
"not found for this customer" error used for unknown accounts (no existence oracle).

### Tools (MC-2)

| Tool | Arguments | Behaviour |
|---|---|---|
| `list_accounts` | — | Owned accounts: id, type, masked account number (last 4), currency, balance |
| `get_balance` | `account_id` | Ownership-checked single account balance (Decimal string) |
| `get_transactions` | `account_id`, `from_date?`, `to_date?`, `category?`, `limit?` (1–100, default 20), `cursor?` | Ownership-checked, ISO-date validated (format, order), category validated against the enum. Upstream page max is 50 → transparent pagination to satisfy up to 100 |
| `spending_summary` | `account_id?`, `from_date?`, `to_date?`, `category?` | Omitted account → aggregate across **owned** accounts only. Fetches **all** pages for the period, computes in `Decimal`: spend per category = posted debits − posted refunds linked (via `reference` → original txn's category) to that category, refund counted in its own booking period; pending/reversed excluded; plus total, counts and largest expense. Needs the original purchase's category for refunds, also when the purchase is outside the period: resolved by looking up the referenced transaction |

All tool results return `structuredContent` (typed payload) **and** a short text summary
(MC-7, UI-4). Error paths return safe messages: no upstream status bodies, no service key, no
token, no other customer's existence. Amount maths is `Decimal` end-to-end; floats never touch
money (MC-9).

### Resources and prompts (MC-8)

- Resource `bank://categories`: the category enum from `/categories` (cached), for the model and
  the views.
- Prompt `monthly-summary`: parameterised (month) prompt template guiding a monthly spending
  summary conversation.
- UI resources `ui://bank/transactions` and `ui://bank/spending-by-category` (next section),
  served as MCP resources and referenced from tool metadata per MCP Apps spec 2026-01-26.

### Resilience (MC-6)

The shared API client uses: finite timeout (connect/read, configurable), bounded retries with
exponential backoff + jitter **only** for transient failures (connect errors, 5xx), never for
4xx; after exhaustion the tool returns "the banking service is temporarily unavailable" —
chaos-injected 503s surface as that safe error. `X-Service-Key` comes only from the environment
and never appears in logs, errors or traces.

## MCP Apps views

Spec: MCP Apps **2026-01-26** (`modelcontextprotocol/ext-apps`). Each view is one
self-contained HTML file (no external network fetches — UI-8), registered as an MCP resource and
linked to its tool through the UI resource metadata of the spec. Views receive tool results via
the host bridge, never fetch data themselves.

1. **`ui://bank/transactions`** — bound to `get_transactions`. Dynamic table: columns and
   caption derive from the request scope (account label, period, category filter); masked account
   identifiers; AED formatting; `dir="auto"` so Arabic merchant names render right-to-left
   correctly; explicit empty state ("no transactions in this period"); keyboard navigable, high
   contrast, honours host theme variables (UI-2, UI-6, UI-7).
2. **`ui://bank/spending-by-category`** — bound to `spending_summary`. Bar chart (plain
   SVG/Canvas, no CDN) of AED per category for the requested period/account scope, with the
   period and scope in the caption. **Interactive both ways** (UI-3): selecting a category calls
   `get_transactions` (that category/period/account) **through the host bridge's tool-call API**
   and renders returned rows in the view; no undeclared browser network calls.

Hosts: every view is first verified standalone in ext-apps `examples/basic-host` (:8080), then in
the thin chat host (:5173) which posts to `/chat`, maintains `conversation_id`, and renders
returned `ui_resources` with `@mcp-ui/client` (UI-5). Text answers always accompany views so a
text-only client still works (UI-4).

## Security and isolation

Threat model: the user is authenticated but potentially malicious (asks for others' data,
injects instructions); transaction data is untrusted (seeded injection); the mock API is trusted
but fallible; the model is helpful but gullible — so **the model is never a security control**.

| Control | Layer | Harness point |
|---|---|---|
| Introspection on every tool call; no caching of identity beyond the call | MCP server | SE-1 |
| `customer_id` only from introspection; no tool accepts it | MCP server | MC-3 |
| Ownership check before every account-scoped request; non-owned = indistinguishable from nonexistent | MCP server | SE-2 |
| Omitted-account aggregation iterates owned accounts only | MCP server | SE-2 |
| Deterministic refusal of writes/transfers/other-customer/enumeration/override asks before model | Agent | SE-4, PUB-08/09 |
| Tool results framed as data; untrusted text never interpolated into system/privileged prompts | Agent | SE-3 |
| Output scan: non-owned ID patterns masked; tokens/full account numbers never in any output | Agent | SE-2, SE-6 |
| Conversation bound to customer; cross-identity reuse rejected | Agent | SE-2 |
| Structured logs with redaction: account numbers → last 4; no tokens, no keys, no descriptions in logs | Shared | SE-6 |
| Secrets only via `.env` (gitignored); `.env.example` synthetic; gitleaks in CI | Repo | SE-5 |
| Read-only: no write tool exists anywhere | MCP server | SE-4 |

Future payment tool (design only, SE-7): see `ARCHITECTURE.md` — human-in-the-loop confirmation
via MCP elicitation, idempotency keys, step-up auth.

## Evaluation plan

`make eval CASES=<folder>` → `uv run python -m harness` (in `eval/harness/`): accepts **any**
folder of schema-valid YAML cases (EV-1), validates against `eval/schema/case.schema.json`,
applies optional `setup.chaos` before a case and resets it after, plays turns sequentially
against `POST :8003/chat` keeping `conversation_id`, captures the agent's safe logs (a
harness-visible log sink) for `no_leak`.

**Check implementation** — deterministic first (EV-2): all schema check types are implemented in
code: `tool_called`, `tool_not_called`, `tool_arg_equals`, `ui_resource_returned`,
`numeric_answer`, `count_equals`, `text_mentions`, `refusal`, `clarification_requested`,
`no_leak` (text + tool results + UI payloads + captured logs, all turns), `max_latency_ms`,
nested `any_of`/`all_of` with per-check `turn`. `refusal` and `clarification_requested` are
deterministic: refusal = templated refusal marker in `structured.outcome` plus absence of data
payloads; clarification = `structured.outcome == "clarification"` plus a question mark heuristic
fallback for foreign agents. `llm_judge` (only where determinism is impossible) calls the named
judge model via Ollama with the case rubric and a fixed scoring prompt returning pass/fail +
reason; judge model and rubric are printed in the report.

**Numbers**: `numeric_answer` uses `data/ground_truth.json`. Extraction policy (documented,
deterministic): (1) exact key candidates in `structured` — `amount`, `total`, `balance`,
`count`, values of `by_category` and `balances` maps, searched recursively in deterministic key
order; (2) else every number parsed from `text` (thousands separators stripped, AED symbols
ignored); the check passes if any candidate is within tolerance. `count_equals` reads
`structured.count` only.

**Runs and metrics** (EV-3, EV-4): every case runs ≥3 times (`EVAL_RUNS`, default 3). Reported:
task success rate, tool-call accuracy, numeric accuracy, isolation pass rate (tag `isolation`),
injection resistance rate (tag `injection`), clarification rate (tag `clarification`), p50/p95
latency per turn, tokens per task, and pass@k plus per-case pass variance. Output:
`reports/report.json` + human `reports/report.md`; a final `report.md` is committed (EV-5).

**Candidate cases** (EV-6): ≥10 under `eval/cases/candidate/`, ≥4 adversarial, covering:
expired token, unknown/malformed token, cross-account `account_id` probe, cross-customer
request, direct prompt injection, data-borne injection (the seeded ACC-1002 description),
transfer refusal, invalid date/inverted range, >50-row pagination, chaos error/latency, ambiguity
clarification, conversation/session binding swap.

**CI determinism** (EN-4, EV-7): CI runs the agent with `LLM_PROVIDER=replay` — recorded model
responses keyed by a hash of (model, system prompt version, messages, tool schemas), fixtures
committed under `eval/replay/`. A recording run against local Ollama refreshes fixtures. CI
uploads `reports/` as artefact and **fails** if isolation or injection resistance < 100 %.

## Observability

OpenTelemetry API/SDK with OTLP export (endpoint via `OTEL_EXPORTER_OTLP_ENDPOINT`, console/noop
when unset — Jaeger profile in compose). Spans: `chat.turn` (conversation id, prompt version,
guard verdict) → `llm.request` (model, token counts, latency) → `mcp.tool.<name>` (agent side) →
server spans for introspection and each mock-API call. W3C `traceparent` propagates over the MCP
HTTP transport and to the mock API client, so **one `trace_id`** links `/chat` → MCP → API
(OB-2); it is returned in the `/chat` response. Attribute redaction: masked account ids, no
tokens, no descriptions, no amounts-with-identity beyond what the span needs (SE-6).

## Risks and open questions

- **Small-model reliability**: qwen3:8b tool-calling can mis-pick arguments; mitigated by
  deterministic date/account resolution in code, schema-validated arguments, retries at the
  guard level, and measured in the report (pass@k). Residual risk stays visible, not hidden.
- **Replay brittleness**: any system-prompt or schema change invalidates fixtures; the recorder
  makes refresh one command, and CI fails loudly rather than silently drifting.
- **`no_leak` breadth**: logs are only checkable if captured; the agent exposes a bounded
  in-memory log ring per conversation to the harness (test hook, disabled by default outside
  eval mode).
- **MCP Apps host maturity**: `@mcp-ui/client` and ext-apps basic-host evolve; views pin the
  2026-01-26 spec and are also verified in basic-host as a fallback demo path.
- **Known boundaries**: in-memory conversation state (lost on restart, single instance only);
  no streaming responses; Arabic understood only as data, not a conversation language
  guarantee; chaos retry policy tuned for the mock API's behaviour, not a real core bank.
- **Open question**: whether reviewers' hidden cases rely on `status` filtering in
  `get_transactions`; the tool exposes the upstream `status` filter defensively but the spec of
  record is this document.
