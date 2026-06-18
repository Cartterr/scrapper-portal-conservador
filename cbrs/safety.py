from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any, Mapping


class StopReason(str, Enum):
    CAPTCHA_REJECTED = "captcha_rejected"
    DAILY_LIMIT = "daily_limit"
    RATE_LIMIT = "rate_limit"
    WAF_CHALLENGE = "waf_challenge"
    AUTH_REQUIRED = "auth_required"
    EGRESS_PREFLIGHT = "egress_preflight_failed"
    TEMPORARY_UNAVAILABLE = "temporary_unavailable"
    UNEXPECTED_HTML = "unexpected_html"
    UNEXPECTED_STATUS = "unexpected_status"


class SafetyStopException(RuntimeError):
    def __init__(
        self,
        reason: StopReason,
        message: str,
        *,
        status: int | None = None,
        context: str | None = None,
    ) -> None:
        self.reason = reason
        self.status = status
        self.context = context
        super().__init__(message)


SENSITIVE_KEY_PARTS = (
    "authorization",
    "auth_key",
    "cookie",
    "csrf",
    "g-recaptcha-response",
    "ip_address",
    "password",
    "public_ip",
    "raw_ip",
    "recaptcha",
    "secret",
    "fingerprint_seed",
    "cloak_fingerprint_seed",
    "cloak_proxy_url",
    "proxy_url",
    "proxy_password",
    "ticket",
    "token",
)

JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
COOKIE_VALUE_RE = re.compile(
    r"(?i)\b(cbrs_refresh_token|auth_cbrs_token|recaptcha-token)=([^;\s]+)"
)
HEADER_VALUE_RE = re.compile(
    r"(?i)(authorization|recaptcha-token|recaptchaToken|set-cookie|cookie)(\s*[:=]\s*)([^\s,;}]+)"
)
TOKEN_VALUE_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*token[A-Za-z0-9_.-]*)(\s*[:=]\s*)([^\s,;}]+)"
)
FINGERPRINT_ARG_RE = re.compile(r"(?i)(--fingerprint=)([A-Za-z0-9_-]+)")
PROXY_URL_RE = re.compile(
    r"(?i)\b((?:https?|socks5)://)([^@\s/]+)@([^\s/]+)"
)
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

CHALLENGE_MARKERS = (
    "_Incapsula_Resource",
    "incapsula_main_message",
    "Incapsula incident ID",
    "Imperva",
    "Request unsuccessful",
)


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in SENSITIVE_KEY_PARTS):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    text = BEARER_RE.sub("Bearer [REDACTED]", text)
    text = JWT_RE.sub("[REDACTED_JWT]", text)
    text = COOKIE_VALUE_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = HEADER_VALUE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    text = TOKEN_VALUE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    text = FINGERPRINT_ARG_RE.sub(r"\1[REDACTED]", text)
    text = PROXY_URL_RE.sub(r"\1[REDACTED]@\3", text)
    text = IPV4_RE.sub("[REDACTED_IP]", text)
    return text


def response_preview(body: Any, *, limit: int = 500) -> str:
    text = _body_to_text(redact(body))
    text = " ".join(text.split())
    if len(text) > limit:
        return f"{text[:limit]}..."
    return text


def classify_response(
    status: int,
    headers: Mapping[str, str] | None,
    body: Any,
    *,
    expected: str = "json",
) -> StopReason | None:
    headers = headers or {}
    header_text = json.dumps(dict(headers), ensure_ascii=False).lower()
    text = _body_to_text(body)
    lower_text = text.lower()
    data = body if isinstance(body, Mapping) else _try_json(text)

    if isinstance(data, Mapping):
        code = str(data.get("code", "")).lower()
        if code == "err-limite":
            return StopReason.DAILY_LIMIT
        if code == "intente-mas-tarde":
            return StopReason.CAPTCHA_REJECTED
        message = str(data.get("msg", "")).lower()
        if code == "error" and "intente" in message and "tarde" in message:
            return StopReason.TEMPORARY_UNAVAILABLE

    if any(marker.lower() in lower_text for marker in CHALLENGE_MARKERS):
        return StopReason.WAF_CHALLENGE

    if status == 401:
        return StopReason.AUTH_REQUIRED
    if status == 403:
        return StopReason.WAF_CHALLENGE
    if status == 429:
        return StopReason.RATE_LIMIT

    content_type = _header(headers, "content-type").lower()
    looks_html = _looks_like_html(text)
    if expected == "image" and ("text/html" in content_type or looks_html):
        return StopReason.UNEXPECTED_HTML
    if expected == "json" and looks_html:
        return StopReason.UNEXPECTED_HTML

    if status != 200:
        return StopReason.UNEXPECTED_STATUS

    return None


def ensure_safe_response(
    status: int,
    headers: Mapping[str, str] | None,
    body: Any,
    *,
    expected: str = "json",
    context: str = "request",
) -> None:
    reason = classify_response(status, headers, body, expected=expected)
    if reason is None:
        return
    preview = response_preview(body)
    detail = f"{context} stopped: {reason.value}"
    if status:
        detail = f"{detail} (HTTP {status})"
    if preview:
        detail = f"{detail}. Response: {preview}"
    raise SafetyStopException(reason, detail, status=status, context=context)


def _header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def _body_to_text(body: Any) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="ignore")
    if isinstance(body, str):
        return body
    try:
        return json.dumps(body, ensure_ascii=False)
    except TypeError:
        return str(body)


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def _looks_like_html(text: str) -> bool:
    stripped = text.lstrip().lower()
    return stripped.startswith("<!doctype html") or stripped.startswith("<html")
