"""OpenTelemetry setup and W3C trace propagation helpers (OB-1, OB-2).

One trace_id links /chat -> MCP server -> mock API: the agent starts the turn span and sends
`traceparent` on MCP requests; the MCP server continues that context and the shared BankClient
injects it into mock-API requests. Spans export over OTLP when OTEL_EXPORTER_OTLP_ENDPOINT is
set; otherwise spans still exist (so trace ids flow) but nothing is exported.
"""

from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_CONFIGURED = False


def setup_tracing(service_name: str) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _CONFIGURED = True


def tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


def current_trace_id() -> str:
    context = trace.get_current_span().get_span_context()
    return f"{context.trace_id:032x}" if context.trace_id else ""


def inject_trace_headers(headers: dict[str, str]) -> dict[str, str]:
    inject(headers)
    return headers


def context_from_headers(headers) -> object:
    return extract(dict(headers))
