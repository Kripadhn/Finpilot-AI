"""Structured logging with redaction and an in-memory capture ring.

Redaction (SE-6): session tokens, service/admin keys and IBAN-like account numbers never
appear in log output; account numbers show the last 4 characters only. Transaction payloads
(descriptions, merchant names) are never logged by policy — log counts and ids instead.

The capture ring exists so the evaluation harness can run `no_leak` checks against real log
output. It is per-process, bounded, and only exposed over HTTP when EXPOSE_SAFE_LOGS is on.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Iterable

_TOKEN_RE = re.compile(r"\btok_[A-Za-z0-9_]+")
_IBAN_RE = re.compile(r"\bAE\d{17,21}\b")
_CAPTURE: deque[str] = deque(maxlen=2000)
_CAPTURE_LOCK = threading.Lock()


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Mask tokens, configured secrets and account numbers in free text."""
    text = _TOKEN_RE.sub("tok_***", text)
    text = _IBAN_RE.sub(lambda m: f"AE…{m.group(0)[-4:]}", text)
    for secret in extra_secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def mask_account_number(number: str) -> str:
    """Show the last 4 characters only (SE-6)."""
    return f"…{number[-4:]}" if len(number) > 4 else "…"


class _RedactingJsonFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self._secrets = secrets

    def format(self, record: logging.Record) -> str:
        payload: dict = {
            "ts": round(time.time(), 3),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("trace_id", "conversation_id", "tool", "status", "attempt", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return redact(json.dumps(payload, ensure_ascii=False), self._secrets)


class _CaptureHandler(logging.Handler):
    def emit(self, record: logging.Record) -> None:
        line = self.format(record)
        with _CAPTURE_LOCK:
            _CAPTURE.append(line)


def captured_logs() -> list[str]:
    with _CAPTURE_LOCK:
        return list(_CAPTURE)


def clear_captured_logs() -> None:
    with _CAPTURE_LOCK:
        _CAPTURE.clear()


def setup_logging(service: str, secrets: Iterable[str] = (), level: int = logging.INFO) -> None:
    """Install redacting JSON logging on the root logger (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level)
    formatter = _RedactingJsonFormatter(tuple(s for s in secrets if s))
    if not any(isinstance(h, _CaptureHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        capture = _CaptureHandler()
        capture.setFormatter(formatter)
        root.addHandler(stream)
        root.addHandler(capture)
    else:
        for handler in root.handlers:
            handler.setFormatter(formatter)
    logging.getLogger(service).debug("logging configured")
