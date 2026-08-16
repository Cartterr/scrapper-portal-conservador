from __future__ import annotations

import logging
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse


from .browser_runtime import detect_browser
from .cloak import apply_cloak_environment, cloak_launch_args, cloak_proxy
from .config import SETTINGS, Settings
from .safety import SafetyStopException, StopReason, classify_response

logger = logging.getLogger(__name__)
LOGIN_COOKIE_NAMES = {
    "auth_cbrs_token",
    "cbrs_refresh_token",
}
OFFSCREEN_CHROME_ARGS = [
    "--window-size=1366,900",
    "--window-position=-32000,-32000",
]


@dataclass(frozen=True)
class BrowserFetchResponse:
    status: int
    headers: dict[str, str]
    body_text: str | None = None
    body_base64: str | None = None


class CredentialsRejectedError(RuntimeError):
    """The portal rejected configured credentials without exposing their values."""


class BrowserSession:
    """Persistent headed browser profile used as the single trusted session."""

    def __init__(self, settings: Settings = SETTINGS, *, headless: bool = False) -> None:
        self.settings = settings
        self.headless = headless
        self._context: Any = None
        self._playwright: Any = None

    def open(self) -> BrowserSession:
        if self._context is not None:
            return self

        self.settings.profile_dir.mkdir(parents=True, exist_ok=True)

        if self.settings.browser_backend == "chrome":
            if self.settings.cloak_proxy_url:
                raise RuntimeError(
                    "CBRS_CLOAK_PROXY_URL is not allowed with the production chrome backend."
                )
            proxy = _playwright_proxy(self.settings.proxy_url)
            executable = detect_browser(self.settings)

            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            try:
                self._context = self._playwright.chromium.launch_persistent_context(
                    str(self.settings.profile_dir),
                    executable_path=str(executable.path),
                    headless=self.headless,
                    accept_downloads=True,
                    bypass_csp=False,
                    chromium_sandbox=True,
                    proxy=proxy,
                    args=_chrome_launch_args(self.settings, headless=self.headless),
                )
            except Exception:
                # A failed persistent-context launch otherwise leaves the sync
                # Playwright event loop alive and poisons the next account.
                self._playwright.stop()
                self._playwright = None
                raise
            return self

        if self.settings.browser_backend != "cloak":
            raise RuntimeError(
                f"Unsupported browser backend: {self.settings.browser_backend!r}. "
                "Production backend must be 'chrome'."
            )

        apply_cloak_environment(self.settings)
        from cloakbrowser import launch_persistent_context

        self._context = launch_persistent_context(
            str(self.settings.profile_dir),
            headless=self.headless,
            accept_downloads=True,
            bypass_csp=True,
            proxy=cloak_proxy(self.settings),
            args=cloak_launch_args(self.settings),
            humanize=True,
            human_preset="careful",
        )

        return self

    @property
    def context(self) -> Any:
        if self._context is None:
            self.open()
        assert self._context is not None
        return self._context

    @property
    def page(self) -> Any:
        pages = self.context.pages
        if pages:
            return pages[0]
        return self.context.new_page()

    def goto_index(self) -> None:
        if not self.page.url.startswith(self.settings.commerce_url):
            self.page.goto(self.settings.commerce_url, wait_until="domcontentloaded", timeout=60000)

    def has_login_cookie(self) -> bool:
        cookies = self.context.cookies(
            [
                self.settings.base_url,
                self.settings.commerce_url,
                self._url("/api/v1/auth/refresh"),
            ]
        )
        return any(cookie["name"] in LOGIN_COOKIE_NAMES for cookie in cookies)

    def wait_for_login(self, *, timeout_seconds: int | None = None) -> None:
        self.goto_index()
        waited_ms = 0
        timeout_ms = None if timeout_seconds is None else timeout_seconds * 1000
        while True:
            if self.has_active_login():
                return
            if timeout_ms is not None and waited_ms >= timeout_ms:
                raise SafetyStopException(
                    StopReason.AUTH_REQUIRED,
                    "Timed out waiting for manual login.",
                    context="init",
                )
            self.page.wait_for_timeout(1000)
            waited_ms += 1000

    def require_login_cookie(self) -> None:
        self.goto_index()
        if not self.has_login_cookie():
            raise SafetyStopException(
                StopReason.AUTH_REQUIRED,
                "No active CBRS login found in the persistent profile. Run `cbrs init` first.",
                context="auth",
            )

    def has_active_login(self) -> bool:
        if not self.has_login_cookie():
            return False
        # A newly opened persistent context starts on about:blank even when its
        # profile contains valid auth cookies.  Refresh is a browser-origin
        # request, so establish the CBRS origin before evaluating fetch().
        self.goto_index()
        response = self.fetch_json(
            "/api/v1/auth/refresh",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            body={},
        )
        if response.status != 200:
            return False
        try:
            data = json.loads(response.body_text or "")
        except json.JSONDecodeError:
            return False
        token = data.get("token")
        if not isinstance(token, str) or not token:
            return False
        self.set_auth_cookie(token)
        return True

    def ensure_authenticated(
        self,
        username: str | None,
        password: str | None,
        *,
        force: bool = False,
    ) -> str:
        """Refresh an existing session or perform one browser-origin login.

        Credentials are accepted only as in-memory values supplied by the caller.
        They are never persisted by this class or included in errors and logs.
        """
        self.open()
        if not force and self.has_active_login():
            return "refreshed"
        if not username or not password:
            raise SafetyStopException(
                StopReason.AUTH_REQUIRED,
                "No unattended credentials are configured for this account.",
                context="auth",
            )

        fetch_error: Exception | None = None
        try:
            self._login_with_fetch(username, password)
            if self.has_active_login():
                return "browser_fetch"
            fetch_error = RuntimeError("CBRS login completed without an active session.")
        except (CredentialsRejectedError, SafetyStopException):
            raise
        except Exception as exc:
            fetch_error = exc

        try:
            self._login_with_form(username, password)
            if self.has_active_login():
                return "browser_form"
        except (CredentialsRejectedError, SafetyStopException):
            raise
        except Exception as exc:
            raise SafetyStopException(
                StopReason.AUTH_REQUIRED,
                "Automatic CBRS login failed using both supported browser flows.",
                context="auth",
            ) from exc

        raise SafetyStopException(
            StopReason.AUTH_REQUIRED,
            "Automatic CBRS login did not establish an active session.",
            context="auth",
        ) from fetch_error

    def generate_recaptcha_token(self, action: str) -> str:
        self._ensure_recaptcha_ready()
        token = self.page.evaluate(
            """async ({ sitekey, action }) => {
                return await grecaptcha.enterprise.execute(sitekey, { action });
            }""",
            {"sitekey": self.settings.recaptcha_sitekey, "action": action},
        )
        if not isinstance(token, str) or not token:
            raise SafetyStopException(
                StopReason.CAPTCHA_REJECTED,
                "Browser did not return a reCAPTCHA token.",
                context="recaptcha",
            )
        logger.debug("Generated reCAPTCHA token for action=%s", action)
        return token

    def _login_with_fetch(self, username: str, password: str) -> None:
        self.goto_index()
        home = self.fetch_json(
            "/api/v1/home/start",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            body={"preHint": ""},
        )
        if home.status != 200:
            raise RuntimeError(f"CBRS home bootstrap returned HTTP {home.status}.")
        token = self.generate_recaptcha_token("login")
        response = self.fetch_json(
            "/api/v1/auth/login",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "recaptcha-token": token,
            },
            body={"email": username, "password": password},
        )
        self._check_login_response(response)

    def _login_with_form(self, username: str, password: str) -> None:
        self.page.goto(
            self._url("/login"),
            wait_until="domcontentloaded",
            timeout=60000,
        )
        email = self.page.locator("#email, input[type=email], input[name=email]").first
        password_input = self.page.locator(
            "#password, input[type=password], input[name=password]"
        ).first
        try:
            email.wait_for(state="visible", timeout=10000)
        except Exception:
            login_button = self.page.get_by_role("button", name=re.compile("iniciar sesi", re.I))
            login_button.first.click(timeout=5000)
            email.wait_for(state="visible", timeout=10000)
        email.fill(username)
        password_input.fill(password)
        submit = self.page.locator("button[type=submit]").first
        with self.page.expect_response(
            lambda response: "/api/v1/auth/login" in response.url,
            timeout=45000,
        ) as response_info:
            submit.click()
        response = response_info.value
        try:
            body_text = response.text()
        except Exception:
            body_text = ""
        self._check_login_response(
            BrowserFetchResponse(
                status=int(response.status),
                headers=dict(response.headers),
                body_text=body_text,
            )
        )

    @staticmethod
    def _check_login_response(response: BrowserFetchResponse) -> None:
        reason = classify_response(
            response.status,
            response.headers,
            response.body_text,
        )
        if reason in {StopReason.CAPTCHA_REJECTED, StopReason.WAF_CHALLENGE}:
            raise SafetyStopException(
                reason,
                f"CBRS login stopped: {reason.value}.",
                status=response.status,
                context="auth login",
            )
        if response.status == 200:
            return
        if response.status in {400, 401, 422}:
            raise CredentialsRejectedError("CBRS rejected the configured account credentials.")
        detail = f"CBRS login returned unexpected HTTP {response.status}."
        raise RuntimeError(detail)

    def fetch_json(
        self,
        path: str,
        *,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        body: Any = None,
    ) -> BrowserFetchResponse:
        result = self.page.evaluate(
            """async ({ url, method, headers, body }) => {
                const response = await fetch(url, {
                    method,
                    headers,
                    body: body === null ? undefined : JSON.stringify(body),
                    credentials: 'include'
                });
                return {
                    status: response.status,
                    headers: Object.fromEntries(response.headers.entries()),
                    bodyText: await response.text()
                };
            }""",
            {
                "url": self._url(path),
                "method": method,
                "headers": headers or {},
                "body": body,
            },
        )
        return BrowserFetchResponse(
            status=int(result["status"]),
            headers=dict(result["headers"]),
            body_text=str(result.get("bodyText") or ""),
        )

    def fetch_bytes(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> BrowserFetchResponse:
        result = self.page.evaluate(
            """async ({ url, headers }) => {
                const response = await fetch(url, {
                    method: 'GET',
                    headers,
                    credentials: 'include'
                });
                const buffer = await response.arrayBuffer();
                const bytes = new Uint8Array(buffer);
                let binary = '';
                const chunkSize = 0x8000;
                for (let i = 0; i < bytes.length; i += chunkSize) {
                    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
                }
                return {
                    status: response.status,
                    headers: Object.fromEntries(response.headers.entries()),
                    bodyBase64: btoa(binary)
                };
            }""",
            {"url": self._url(path), "headers": headers or {}},
        )
        return BrowserFetchResponse(
            status=int(result["status"]),
            headers=dict(result["headers"]),
            body_base64=str(result.get("bodyBase64") or ""),
        )

    def set_auth_cookie(self, token: str) -> None:
        domain = urlparse(self.settings.base_url).hostname
        if not domain:
            raise RuntimeError("Cannot determine CBRS cookie domain.")
        self.context.add_cookies(
            [
                {
                    "name": "auth_cbrs_token",
                    "value": f'"{token}"',
                    "domain": domain,
                    "path": "/",
                    "secure": self.settings.base_url.startswith("https://"),
                    "httpOnly": False,
                }
            ]
        )

    def export_cookies(self) -> list[dict[str, Any]]:
        return self.context.cookies([self.settings.base_url])

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
                self._context = None
        finally:
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None

    def __enter__(self) -> BrowserSession:
        return self.open()

    def __exit__(self, *args: object) -> None:
        self.close()

    def _ensure_recaptcha_ready(self) -> None:
        self.goto_index()
        ready = self.page.evaluate(
            """() => Boolean(
                window.grecaptcha &&
                window.grecaptcha.enterprise &&
                window.grecaptcha.enterprise.execute
            )"""
        )
        if not ready:
            self.page.add_script_tag(
                url=f"https://www.google.com/recaptcha/enterprise.js?render={self.settings.recaptcha_sitekey}"
            )
        self.page.wait_for_function(
            """() => Boolean(
                window.grecaptcha &&
                window.grecaptcha.enterprise &&
                window.grecaptcha.enterprise.execute
            )""",
            timeout=30000,
        )
        self.page.evaluate(
            """() => new Promise((resolve) => grecaptcha.enterprise.ready(resolve))"""
        )

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{self.settings.base_url}{path}"


def _chrome_launch_args(settings: Settings, *, headless: bool) -> list[str]:
    if headless:
        return []
    if settings.window_mode == "offscreen":
        return list(OFFSCREEN_CHROME_ARGS)
    return []


def _playwright_proxy(proxy_url: str | None) -> dict[str, str] | None:
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if parsed.scheme.lower() not in {"http", "https", "socks5"}:
        raise RuntimeError("CBRS_PROXY_URL must start with http://, https://, or socks5://.")
    if not parsed.hostname or not parsed.port:
        raise RuntimeError("CBRS_PROXY_URL must include a proxy host and port.")
    proxy: dict[str, str] = {
        "server": f"{parsed.scheme.lower()}://{parsed.hostname}:{parsed.port}",
    }
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy
