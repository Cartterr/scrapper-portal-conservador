from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    OK = "ok"
    AUTH = "auth"
    CAPTCHA = "captcha"
    WAF = "waf"
    RATE_LIMIT = "rate_limited"
    DAILY_LIMIT = "daily_limit"
    SHAPE_DRIFT = "shape_drift"
    TRANSIENT = "transient"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassifiedResponse:
    code: ErrorCode
    retryable: bool
    message: str


class PortalCallError(RuntimeError):
    def __init__(self, status: int, classified: ClassifiedResponse, *, endpoint: str):
        super().__init__(f"{endpoint} failed with {status}: {classified.code} {classified.message}")
        self.status = status
        self.classified = classified
        self.endpoint = endpoint


def classify_response(
    status: int,
    data: Any = None,
    headers: dict[str, str] | None = None,
    *,
    endpoint: str | None = None,
) -> ClassifiedResponse:
    headers = headers or {}
    app_code = data.get("code") if isinstance(data, dict) else None
    app_msg = data.get("msg") or data.get("message") if isinstance(data, dict) else ""

    if app_code == "err-limite":
        return ClassifiedResponse(ErrorCode.DAILY_LIMIT, False, app_msg or "daily limit reached")
    if app_code == "intente-mas-tarde":
        return ClassifiedResponse(ErrorCode.CAPTCHA, True, app_msg or "captcha/session rejected")
    if status == 429:
        return ClassifiedResponse(ErrorCode.RATE_LIMIT, False, app_msg or "portal rate limit reached")
    if _looks_like_imperva(headers, data, endpoint=endpoint):
        return ClassifiedResponse(ErrorCode.WAF, True, "request rejected by edge/WAF")
    if 200 <= status < 300:
        return ClassifiedResponse(ErrorCode.OK, False, "ok")
    if status in {401, 403}:
        return ClassifiedResponse(ErrorCode.AUTH, False, app_msg or "authentication required")
    if status == 404:
        return ClassifiedResponse(ErrorCode.NOT_FOUND, False, "endpoint or resource not found")
    if status in {408, 409, 425, 500, 502, 503, 504}:
        return ClassifiedResponse(ErrorCode.TRANSIENT, True, app_msg or f"transient status {status}")
    if 400 <= status < 500:
        return ClassifiedResponse(ErrorCode.SHAPE_DRIFT, False, app_msg or f"client error {status}")
    return ClassifiedResponse(ErrorCode.UNKNOWN, True, app_msg or f"unexpected status {status}")


def _looks_like_imperva(headers: dict[str, str], data: Any, *, endpoint: str | None = None) -> bool:
    lowered = {k.lower(): str(v).lower() for k, v in headers.items()}
    if "imperva" in lowered.get("x-cdn", "") or "incap" in lowered.get("x-iinfo", ""):
        return True
    content_type = lowered.get("content-type", "")
    if isinstance(data, str):
        text = data.lower()
        if any(marker in text for marker in ("incapsula", "imperva", "captcha", "access denied")):
            return True
        if endpoint and endpoint.startswith("/api/") and "text/html" in content_type:
            return True
    if endpoint and endpoint.startswith("/api/") and "text/html" in content_type:
        return True
    return False
