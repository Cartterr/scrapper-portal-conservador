from __future__ import annotations

import json
import logging
import re
from typing import Any

TOKEN_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._-]+")
JWT_RE = re.compile(r"eyJ[A-Za-z0-9._-]+")
LONG_OPAQUE_RE = re.compile(r"[A-Za-z0-9_-]{80,}")


def sanitize_text(value: str) -> str:
    value = TOKEN_RE.sub(r"\1[REDACTED]", value)
    value = JWT_RE.sub("[JWT_REDACTED]", value)
    value = LONG_OPAQUE_RE.sub("[LONG_OPAQUE_REDACTED]", value)
    return value


def sanitize_obj(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_obj(item) for item in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if re.search(r"(?i)(password|token|cookie|ticket|secret|auth|jwt)", str(key)):
                out[key] = "[REDACTED]"
            else:
                out[key] = sanitize_obj(item)
        return out
    return value


def dumps_safe(value: Any) -> str:
    return json.dumps(sanitize_obj(value), ensure_ascii=False, sort_keys=True)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return sanitize_text(super().format(record))


def configure_logging(verbose: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
