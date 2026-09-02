from __future__ import annotations

import json

import pytest

from cbrs.proxy_provider import (
    GENERIC_STATIC_PROXY_PROVIDER,
    TWO_CAPTCHA_DEDICATED_ISP_PROVIDER,
    TWO_CAPTCHA_RESIDENTIAL_STICKY_PROVIDER,
    ProxyProviderError,
    normalize_proxy_provider,
    two_captcha_proxy_health,
)


def test_proxy_provider_defaults_to_generic_static() -> None:
    assert normalize_proxy_provider(None) == GENERIC_STATIC_PROXY_PROVIDER
    assert normalize_proxy_provider("") == GENERIC_STATIC_PROXY_PROVIDER
    assert normalize_proxy_provider("2CAPTCHA_DEDICATED_ISP") == TWO_CAPTCHA_DEDICATED_ISP_PROVIDER
    assert (
        normalize_proxy_provider("2CAPTCHA_RESIDENTIAL_STICKY")
        == TWO_CAPTCHA_RESIDENTIAL_STICKY_PROVIDER
    )


def test_proxy_provider_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="proxy_provider"):
        normalize_proxy_provider("rotating_residential")


def test_two_captcha_proxy_health_is_redacted_and_healthy() -> None:
    def request_json(url: str, api_key: str):
        assert url.endswith("/proxy")
        assert api_key == "private-key"
        return {
            "status": "OK",
            "data": {
                "username": "private-user",
                "status": 1,
                "total_flow": 1000,
                "use_flow": 250,
                "ip_white": ["203.0.113.42"],
            },
        }

    result = two_captcha_proxy_health("private-key", request_json=request_json)

    assert result["status"] == "healthy"
    assert result["ok"] is True
    assert result["account_active"] is True
    assert result["traffic_remaining"] is True
    assert result["remaining_ratio"] == 0.75
    serialized = json.dumps(result)
    assert "private-key" not in serialized
    assert "private-user" not in serialized
    assert "203.0.113.42" not in serialized
    assert "total_flow" not in serialized


def test_two_captcha_proxy_health_labels_residential_sticky_provider() -> None:
    result = two_captcha_proxy_health(
        "private-key",
        provider=TWO_CAPTCHA_RESIDENTIAL_STICKY_PROVIDER,
        request_json=lambda _url, _key: {
            "status": "OK",
            "data": {"status": 1, "total_flow": 1000, "use_flow": 1},
        },
    )

    assert result["provider"] == TWO_CAPTCHA_RESIDENTIAL_STICKY_PROVIDER
    assert result["ok"] is True


@pytest.mark.parametrize(
    ("provider_status", "total", "used", "expected_status"),
    [(0, 1000, 0, "inactive"), (1, 1000, 1000, "depleted")],
)
def test_two_captcha_proxy_health_fails_closed(
    provider_status: int, total: int, used: int, expected_status: str
) -> None:
    result = two_captcha_proxy_health(
        "private-key",
        request_json=lambda _url, _key: {
            "status": "OK",
            "data": {"status": provider_status, "total_flow": total, "use_flow": used},
        },
    )

    assert result["status"] == expected_status
    assert result["ok"] is False


def test_two_captcha_proxy_health_sanitizes_provider_failures() -> None:
    def fail(_url: str, _key: str):
        raise ProxyProviderError("NETWORK_ERROR")

    result = two_captcha_proxy_health("private-key", request_json=fail)

    assert result["status"] == "unavailable"
    assert result["error_code"] == "NETWORK_ERROR"
    assert result["ok"] is False


def test_two_captcha_proxy_health_requires_a_real_key() -> None:
    result = two_captcha_proxy_health("REPLACE_ME")

    assert result["status"] == "not_configured"
    assert result["error_code"] == "API_KEY_MISSING"
