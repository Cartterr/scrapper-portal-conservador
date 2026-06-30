from __future__ import annotations

from urllib.parse import unquote, urlparse, urlunparse
from urllib.request import (
    HTTPPasswordMgrWithDefaultRealm,
    ProxyBasicAuthHandler,
    ProxyHandler,
    build_opener,
)


def build_authenticated_proxy_opener(
    proxy_url: str | None,
    *,
    supported_schemes: set[str] | frozenset[str],
    error_prefix: str,
):
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    scheme = parsed.scheme.lower()
    if scheme not in supported_schemes:
        schemes = ", ".join(sorted(supported_schemes))
        raise RuntimeError(f"{error_prefix} supports only {schemes} proxy URLs")
    if not parsed.hostname or not parsed.port:
        raise RuntimeError(f"{error_prefix} proxy URL must include a proxy host and port")

    proxy_without_auth = _proxy_url_without_auth(parsed)
    proxy_for_handler = (
        _proxy_url_with_auth(parsed) if parsed.username is not None else proxy_without_auth
    )
    handlers = [ProxyHandler({"http": proxy_for_handler, "https": proxy_for_handler})]
    if parsed.username is not None:
        password_manager = HTTPPasswordMgrWithDefaultRealm()
        password_manager.add_password(
            None,
            proxy_without_auth,
            unquote(parsed.username),
            unquote(parsed.password or ""),
        )
        handlers.append(ProxyBasicAuthHandler(password_manager))
    return build_opener(*handlers)


def _proxy_url_without_auth(parsed) -> str:
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}"
    return urlunparse((parsed.scheme.lower(), netloc, "", "", "", ""))


def _proxy_url_with_auth(parsed) -> str:
    return urlunparse((parsed.scheme.lower(), parsed.netloc, "", "", "", ""))
