from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .dataimpulse import DATAIMPULSE_RESIDENTIAL_STICKY_PROVIDER

GENERIC_STATIC_PROXY_PROVIDER = "generic_static"
TWO_CAPTCHA_DEDICATED_ISP_PROVIDER = "2captcha_dedicated_isp"
TWO_CAPTCHA_RESIDENTIAL_STICKY_PROVIDER = "2captcha_residential_sticky"
PROXY_PROVIDERS = frozenset(
    {
        GENERIC_STATIC_PROXY_PROVIDER,
        TWO_CAPTCHA_DEDICATED_ISP_PROVIDER,
        TWO_CAPTCHA_RESIDENTIAL_STICKY_PROVIDER,
        DATAIMPULSE_RESIDENTIAL_STICKY_PROVIDER,
    }
)
TWO_CAPTCHA_PROXY_ACCOUNT_URL = "https://api.2captcha.com/proxy"
DEFAULT_CACHE_SECONDS = 300.0

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


class ProxyProviderError(RuntimeError):
    """Sanitized proxy-provider error that never contains credentials."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Proxy provider request failed ({code}).")


def dataimpulse_configuration_health(
    login: str | None,
    password: str | None,
) -> dict[str, Any]:
    """Return redacted configuration health for a normal residential plan.

    DataImpulse residential routing is controlled by proxy credentials and
    username parameters. It does not require the reseller account API.
    """
    configured = bool(str(login or "").strip() and str(password or ""))
    return _public_health(
        "configured" if configured else "not_configured",
        provider=DATAIMPULSE_RESIDENTIAL_STICKY_PROVIDER,
        account_active=configured,
        traffic_remaining=configured,
        error_code=None if configured else "PROXY_CREDENTIALS_MISSING",
    ) | {"ok": configured}


def normalize_proxy_provider(value: object) -> str:
    provider = str(value or GENERIC_STATIC_PROXY_PROVIDER).strip().lower()
    if provider not in PROXY_PROVIDERS:
        allowed = ", ".join(sorted(PROXY_PROVIDERS))
        raise ValueError(f"proxy_provider must be one of: {allowed}")
    return provider


def two_captcha_proxy_health(
    api_key: str | None,
    *,
    provider: str = TWO_CAPTCHA_DEDICATED_ISP_PROVIDER,
    force: bool = False,
    cache_seconds: float = DEFAULT_CACHE_SECONDS,
    request_json: Callable[[str, str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return redacted 2Captcha proxy account health.

    Only booleans, a remaining-traffic ratio, timestamps, and sanitized status
    codes leave this module. Provider usernames, IP allowlists, traffic totals,
    and the API key are intentionally discarded.
    """
    provider = normalize_proxy_provider(provider)
    if provider == GENERIC_STATIC_PROXY_PROVIDER:
        raise ValueError("2Captcha health requires a 2Captcha proxy provider")
    key = str(api_key or "").strip()
    if not key or key.upper().startswith("REPLACE_"):
        return _public_health(
            "not_configured", provider=provider, error_code="API_KEY_MISSING"
        )

    cache_key = hashlib.sha256(f"{provider}:{key}".encode("utf-8")).hexdigest()
    now = time.monotonic()
    if not force and request_json is None:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached and cached[0] > now:
                return dict(cached[1])

    requester = request_json or _get_json
    try:
        response = requester(TWO_CAPTCHA_PROXY_ACCOUNT_URL, key)
        health = _parse_account_response(response, provider=provider)
    except ProxyProviderError as exc:
        health = _public_health(
            "unavailable", provider=provider, error_code=exc.code
        )

    if request_json is None:
        with _CACHE_LOCK:
            _CACHE[cache_key] = (now + max(0.0, float(cache_seconds)), dict(health))
    return health


def _parse_account_response(
    response: Mapping[str, Any], *, provider: str
) -> dict[str, Any]:
    if str(response.get("status") or "").upper() != "OK":
        raise ProxyProviderError("API_ERROR")
    data = response.get("data")
    if not isinstance(data, Mapping):
        raise ProxyProviderError("INVALID_RESPONSE")

    active = _active_status(data.get("status"))
    total = _number(data.get("total_flow"))
    used = _number(data.get("use_flow"))
    remaining = max(0.0, total - used) if total is not None and used is not None else None
    remaining_ratio = (
        max(0.0, min(1.0, remaining / total))
        if remaining is not None and total is not None and total > 0
        else None
    )
    traffic_remaining = bool(remaining is not None and remaining > 0)
    status = "healthy" if active and traffic_remaining else "depleted" if active else "inactive"
    return _public_health(
        status,
        provider=provider,
        account_active=active,
        traffic_remaining=traffic_remaining,
        remaining_ratio=remaining_ratio,
    )


def _public_health(
    status: str,
    *,
    provider: str,
    account_active: bool = False,
    traffic_remaining: bool = False,
    remaining_ratio: float | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "status": status,
        "ok": status == "healthy",
        "account_active": account_active,
        "traffic_remaining": traffic_remaining,
        "remaining_ratio": remaining_ratio,
        "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "error_code": error_code,
    }


def _active_status(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) == 1
    return str(value or "").strip().lower() in {"1", "active", "enabled"}


def _number(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _get_json(url: str, api_key: str) -> Mapping[str, Any]:
    request = Request(
        f"{url}?{urlencode({'key': api_key})}",
        headers={"accept": "application/json", "user-agent": "cbrs-proxy-provider/1.0"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=20) as response:
            status = int(response.status)
            body = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise ProxyProviderError(f"HTTP_{exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ProxyProviderError("NETWORK_ERROR") from exc
    if status != 200:
        raise ProxyProviderError(f"HTTP_{status}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProxyProviderError("INVALID_RESPONSE") from exc
    if not isinstance(data, Mapping):
        raise ProxyProviderError("INVALID_RESPONSE")
    return data
