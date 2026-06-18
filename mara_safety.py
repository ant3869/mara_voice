from __future__ import annotations

import re
from typing import Any


REDACTION = "<REDACTED>"

_LABELED_SECRET_RE = re.compile(
    r"(?is)(\b(?:api[_ -]?key|authorization|bearer(?:\s+token)?|token|secret|password)\b"
    r"(?:\s+for\s+authentication)?(?:\s+(?:is|was))?\s*[:=]?\s*[\r\n\s`]*)([A-Za-z0-9._~+/=-]{16,})(`?)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")
_OPENAI_STYLE_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
_JWT_RE = re.compile(r"\b[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b")
_LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_URL_CREDENTIAL_RE = re.compile(r"(?i)(https?://)([^/\s:@]+):([^/\s@]+)@")


def redact_sensitive_text(value: str) -> str:
    text = str(value)
    text = _URL_CREDENTIAL_RE.sub(r"\1<redacted>:<redacted>@", text)
    text = _LABELED_SECRET_RE.sub(lambda match: f"{match.group(1)}{REDACTION}{match.group(3)}", text)
    text = _BEARER_RE.sub(f"Bearer {REDACTION}", text)
    text = _OPENAI_STYLE_KEY_RE.sub(REDACTION, text)
    text = _JWT_RE.sub(REDACTION, text)
    text = _LONG_HEX_RE.sub(REDACTION, text)
    return text


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        return {key: redact_sensitive(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    return value
