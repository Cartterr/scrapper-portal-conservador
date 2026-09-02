from urllib.parse import unquote, urlparse

import pytest

from cbrs.dataimpulse import (
    build_dataimpulse_proxy_url,
    classify_dataimpulse_failure,
    next_unused_sticky_port,
)


def test_builds_encoded_chile_sticky_route_without_leaking_in_repr() -> None:
    url = build_dataimpulse_proxy_url(
        login="proxy user",
        password="p@ss:word",
        host="gw.dataimpulse.com",
        country="CL",
        ttl_minutes=120,
        port=10000,
    )
    parsed = urlparse(url)

    assert parsed.hostname == "gw.dataimpulse.com"
    assert parsed.port == 10000
    assert unquote(parsed.username or "") == "proxy user__cr.cl;sessttl.120"
    assert unquote(parsed.password or "") == "p@ss:word"


@pytest.mark.parametrize("ttl", [0, 121])
def test_rejects_invalid_sticky_ttl(ttl: int) -> None:
    with pytest.raises(ValueError, match="TTL"):
        build_dataimpulse_proxy_url(
            login="login",
            password="secret",
            host="gw.dataimpulse.com",
            country="cl",
            ttl_minutes=ttl,
            port=10000,
        )


def test_selects_next_unused_port_and_wraps() -> None:
    assert next_unused_sticky_port(
        10002, used_ports={10000, 10001, 10002}, minimum=10000, maximum=10004
    ) == 10003
    assert next_unused_sticky_port(
        10004, used_ports={10004, 10000}, minimum=10000, maximum=10004
    ) == 10001


@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        (502, "NO_HOST_CONNECTION", "transient_route"),
        (503, "NO_RAY", "transient_route"),
        (None, "connection reset", "transient_route"),
        (407, "TRAFFIC_EXHAUSTED", "provider_terminal"),
        (400, "portal response", "unknown"),
    ],
)
def test_classifies_provider_failures_without_echoing_details(
    status: int | None, detail: str, expected: str
) -> None:
    assert classify_dataimpulse_failure(status, detail) == expected
