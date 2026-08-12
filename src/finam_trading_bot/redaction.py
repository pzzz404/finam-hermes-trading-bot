"""Central redaction helpers for logs, diagnostics, and public output."""

from __future__ import annotations

import re
from typing import Any


REDACTED = "[REDACTED]"
SENSITIVE_KEY_MARKERS = (
    "token",
    "secret",
    "password",
    "authorization",
    "cookie",
    "session_id",
    "account_id",
    "portfolio_id",
    "chat_id",
    "webhook",
    "api_key",
    "apikey",
    "private_key",
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|secret|password|api[_-]?key|authorization|cookie)\s*[:=]\s*[^\s,;]+"
)
_IDENTIFIER_RE = re.compile(
    r"(?i)\b(account[_ ]id|account with id|portfolio[_ ]id|chat[_ ]id)\s*[:=]?\s*[\"']?[A-Za-z0-9:._-]+"
)
_TELEGRAM_BOT_URL_RE = re.compile(r"(?i)(https://api\.telegram\.org/bot)[^/\s]+")


def sensitive_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(marker in normalized for marker in SENSITIVE_KEY_MARKERS)


def redact_text(value: object) -> str:
    text = str(value)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _IDENTIFIER_RE.sub(lambda match: f"{match.group(1)} [REDACTED]", text)
    return _TELEGRAM_BOT_URL_RE.sub(r"\1[REDACTED]", text)


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: REDACTED if sensitive_key(key) else scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub(item) for item in value)
    if isinstance(value, BaseException):
        return redact_text(value)
    if isinstance(value, str):
        return redact_text(value)
    return value
