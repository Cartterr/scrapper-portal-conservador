from urllib.request import ProxyBasicAuthHandler, ProxyHandler

from cbrs.proxy import build_authenticated_proxy_opener


def test_authenticated_proxy_opener_uses_basic_auth_handler() -> None:
    opener = build_authenticated_proxy_opener(
        "http://user:pass@example.test:8080",
        supported_schemes={"http", "https"},
        error_prefix="test",
    )

    assert opener is not None
    assert any(isinstance(handler, ProxyHandler) for handler in opener.handlers)
    assert any(isinstance(handler, ProxyBasicAuthHandler) for handler in opener.handlers)


def test_authenticated_proxy_opener_keeps_auth_in_proxy_handler() -> None:
    opener = build_authenticated_proxy_opener(
        "http://user:pass@example.test:8080",
        supported_schemes={"http", "https"},
        error_prefix="test",
    )

    handler = next(handler for handler in opener.handlers if isinstance(handler, ProxyHandler))

    assert handler.proxies["https"] == "http://user:pass@example.test:8080"


def test_unauthenticated_proxy_opener_does_not_add_basic_auth_handler() -> None:
    opener = build_authenticated_proxy_opener(
        "http://example.test:8080",
        supported_schemes={"http", "https"},
        error_prefix="test",
    )

    assert opener is not None
    assert any(isinstance(handler, ProxyHandler) for handler in opener.handlers)
    assert not any(isinstance(handler, ProxyBasicAuthHandler) for handler in opener.handlers)
