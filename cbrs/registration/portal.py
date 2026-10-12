"""Portal client for the registration pipeline.

Drives a *new* headful regular-Chrome instance (fresh temporary profile, fresh
DataImpulse sticky exit) and talks to the portal's JSON API from inside the
page, exactly like the production worker does for login:

* ``POST /api/v1/home/start``            bootstrap
* ``POST /api/v1/account/create``        registration (reCAPTCHA action ``signup``)
* ``POST /api/v1/account/activation``    email activation (no captcha)
* ``POST /api/v1/account/resend-activation-email`` (action ``resend_activation_email``)
* ``POST /api/v1/auth/login``            login (action ``login``) -> JWT
* ``POST /api/v1/user/me``               verification (Bearer JWT)

This module never touches production browsers, profiles, workers, or routes.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from ..browser_runtime import detect_browser, validate_native_chrome_executable
from ..browser_session import CHROME_IGNORED_DEFAULT_ARGS
from ..capsolver import CapSolverClient, CapSolverError
from ..preflight import EGRESS_INFO_URLS
from ..proxy import build_authenticated_proxy_opener
from ..safety import (
    StopReason,
    classify_response,
    sanitized_portal_response_code,
)
from .config import RegistrationSettings

logger = logging.getLogger(__name__)

CREATE_PATH = "/crear-cuenta"
LOGIN_PATH = "/login"
RECAPTCHA_ENTERPRISE_READY = """() => Boolean(
    window.grecaptcha &&
    window.grecaptcha.enterprise &&
    window.grecaptcha.enterprise.execute
)"""


class PortalError(RuntimeError):
    """A portal interaction failed; ``stage`` says where."""

    def __init__(
        self,
        stage: str,
        message: str,
        *,
        stop_reason: StopReason | None = None,
        http_status: int | None = None,
        retryable: bool = False,
    ) -> None:
        self.stage = stage
        self.stop_reason = stop_reason
        self.http_status = http_status
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True)
class PortalResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body_text: str = ""

    def json(self) -> Any:
        try:
            return json.loads(self.body_text or "{}")
        except json.JSONDecodeError:
            return None


def fetch_egress_country(proxy_url: str | None, *, timeout: float = 20.0) -> dict[str, Any]:
    """Resolve the public exit (ip, country) through the proxy, like preflight."""

    opener = build_authenticated_proxy_opener(
        proxy_url,
        supported_schemes={"http", "https"},
        error_prefix="Registration egress check:",
    )
    open_fn = opener.open if opener else urlopen
    errors: list[str] = []
    for url in EGRESS_INFO_URLS:
        request = Request(url, headers={"user-agent": "cbrs-registration/1.0"})
        try:
            with open_fn(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            errors.append(f"{urlparse(url).hostname} HTTP {exc.code}")
            continue
        except (URLError, TimeoutError, OSError) as exc:
            raise PortalError("egress", f"Egress lookup failed through proxy: {exc}") from exc
        data = json.loads(payload)
        if isinstance(data, dict):
            return data
    raise PortalError("egress", "Egress lookup services unavailable: " + "; ".join(errors))


def _playwright_proxy(proxy_url: str) -> dict[str, str]:
    parsed = urlparse(proxy_url)
    proxy: dict[str, str] = {
        "server": f"{parsed.scheme.lower()}://{parsed.hostname}:{parsed.port}"
    }
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


class RegistrationPortal:
    """One headful Chrome session bound to one fresh sticky exit."""

    def __init__(
        self,
        settings: RegistrationSettings,
        *,
        proxy_url: str,
        profile_dir: Path,
        sleep=time.sleep,
    ) -> None:
        self.settings = settings
        self.proxy_url = proxy_url
        self.profile_dir = Path(profile_dir)
        self._sleep = sleep
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> "RegistrationPortal":
        if self._context is not None:
            return self
        if self.settings.headless:
            logger.warning(
                "Headless Chrome is rejected by the portal reCAPTCHA (D29/D30); "
                "continuing because CBRS_REGISTRATION_HEADLESS forced it."
            )
        executable = detect_browser()
        validate_native_chrome_executable(executable.path)
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                executable_path=str(executable.path),
                headless=self.settings.headless,
                accept_downloads=False,
                bypass_csp=False,
                chromium_sandbox=True,
                proxy=_playwright_proxy(self.proxy_url),
                args=[],
                ignore_default_args=CHROME_IGNORED_DEFAULT_ARGS,
                handle_sigint=False,
            )
        except Exception:
            self._stop_playwright()
            raise
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        self._page.goto(
            self._url(CREATE_PATH), wait_until="domcontentloaded", timeout=60000
        )
        self._ensure_recaptcha_ready()
        return self

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            logger.debug("Registration browser context close failed", exc_info=True)
        finally:
            self._context = None
            self._page = None
            self._stop_playwright()

    def _stop_playwright(self) -> None:
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                logger.debug("Playwright stop failed", exc_info=True)
            self._playwright = None

    def __enter__(self) -> "RegistrationPortal":
        return self.open()

    def __exit__(self, *args: object) -> None:
        self.close()

    # -- infrastructure steps ----------------------------------------------

    def check_egress_country(self, expected: str) -> str:
        data = fetch_egress_country(self.proxy_url)
        country = str(data.get("country") or "").upper()
        if country != expected.upper():
            raise PortalError(
                "egress",
                f"Sticky exit country is {country or 'unknown'}, expected {expected.upper()}.",
                retryable=True,
            )
        return country

    def bootstrap(self) -> None:
        response = self.fetch_json(
            "/api/v1/home/start",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            body={"preHint": ""},
        )
        if response.status != 200:
            raise PortalError(
                "bootstrap",
                f"CBRS home bootstrap returned HTTP {response.status}.",
                http_status=response.status,
                retryable=True,
            )

    def _ensure_recaptcha_ready(self) -> None:
        ready = self._page.evaluate(RECAPTCHA_ENTERPRISE_READY)
        if not ready:
            self._page.add_script_tag(
                url=(
                    "https://www.google.com/recaptcha/enterprise.js"
                    f"?render={self.settings.recaptcha_sitekey}"
                )
            )
        self._page.wait_for_function(RECAPTCHA_ENTERPRISE_READY, timeout=30000)
        self._page.evaluate(
            "() => new Promise((resolve) => grecaptcha.enterprise.ready(resolve))"
        )

    def recaptcha_token(self, action: str) -> str:
        """In-browser Enterprise token first; CapSolver proxy-bound fallback."""

        try:
            token = self._page.evaluate(
                """async ({ sitekey, action }) => {
                    return await grecaptcha.enterprise.execute(sitekey, { action });
                }""",
                {"sitekey": self.settings.recaptcha_sitekey, "action": action},
            )
            if isinstance(token, str) and token:
                return token
            logger.warning("Browser returned an empty reCAPTCHA token for %s", action)
        except Exception:
            logger.warning(
                "Browser reCAPTCHA execution failed for %s; trying CapSolver", action,
                exc_info=True,
            )
        return self._capsolver_token(action)

    def _capsolver_token(self, action: str) -> str:
        if not self.settings.capsolver_api_key:
            raise PortalError(
                "captcha",
                "Browser reCAPTCHA failed and no CapSolver key is configured.",
                stop_reason=StopReason.CAPTCHA_SOLVER,
            )
        user_agent = self._page.evaluate("() => navigator.userAgent")
        if not isinstance(user_agent, str) or not user_agent:
            raise PortalError(
                "captcha", "Could not read the browser user agent for CapSolver."
            )
        page_path = LOGIN_PATH if action == "login" else CREATE_PATH
        client = CapSolverClient(
            self.settings.capsolver_api_key,
            timeout_seconds=self.settings.capsolver_timeout_seconds,
            poll_seconds=self.settings.capsolver_poll_seconds,
        )
        try:
            return client.solve_recaptcha_v3_enterprise(
                website_url=self._url(page_path),
                website_key=self.settings.recaptcha_sitekey,
                page_action=action,
                min_score=self.settings.capsolver_min_score,
                proxy=self.proxy_url,
                user_agent=user_agent,
            )
        except CapSolverError as exc:
            raise PortalError(
                "captcha",
                f"CapSolver could not produce a {action} token ({exc.code}).",
                stop_reason=StopReason.CAPTCHA_SOLVER,
            ) from exc

    # -- portal API ----------------------------------------------------------

    def fetch_json(
        self,
        path: str,
        *,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        body: Any = None,
    ) -> PortalResponse:
        result = self._page.evaluate(
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
        return PortalResponse(
            status=int(result["status"]),
            headers=dict(result["headers"]),
            body_text=str(result.get("bodyText") or ""),
        )

    def create_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = str(payload.get("recaptchaToken") or "")
        response = self.fetch_json(
            "/api/v1/account/create",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "recaptcha-token": token,
            },
            body=payload,
        )
        self._raise_for_stop("create", response)
        data = response.json()
        if response.status == 200 and isinstance(data, dict) and data.get("status"):
            return data
        message = ""
        if isinstance(data, dict):
            message = str(data.get("msg") or "")[:200]
        raise PortalError(
            "create",
            message
            or f"Account creation rejected (HTTP {response.status}, "
            f"code {sanitized_portal_response_code(response.body_text) or 'none'}).",
            http_status=response.status,
            retryable=_retryable_create_message(message),
        )

    def activate(self, email: str, token: str) -> dict[str, Any]:
        response = self.fetch_json(
            "/api/v1/account/activation",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
            },
            body={"email": email, "token": token},
        )
        self._raise_for_stop("activate", response)
        data = response.json()
        # The SPA treats any non-exception 200 from /account/activation as
        # "Su cuenta ha sido activada con exito".
        if response.status == 200:
            return data if isinstance(data, dict) else {}
        message = str(data.get("msg"))[:200] if isinstance(data, dict) else ""
        raise PortalError(
            "activate",
            message or f"Activation rejected (HTTP {response.status}).",
            http_status=response.status,
        )

    def resend_activation(self, email: str) -> None:
        token = self.recaptcha_token("resend_activation_email")
        response = self.fetch_json(
            "/api/v1/account/resend-activation-email",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "recaptcha-token": token,
            },
            body={"email": email, "recaptchaToken": token},
        )
        self._raise_for_stop("resend", response)
        if response.status != 200:
            raise PortalError(
                "resend",
                f"Resend activation email failed (HTTP {response.status}).",
                http_status=response.status,
                retryable=True,
            )

    def login(self, email: str, password: str) -> str:
        token = self.recaptcha_token("login")
        response = self.fetch_json(
            "/api/v1/auth/login",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "recaptcha-token": token,
            },
            body={"email": email, "password": password},
        )
        self._raise_for_stop("login", response)
        data = response.json()
        jwt = data.get("token") if isinstance(data, dict) else None
        if response.status == 200 and isinstance(jwt, str) and jwt:
            return jwt
        if response.status in {400, 401, 422}:
            raise PortalError(
                "login",
                "Portal rejected the new credentials "
                f"(code {sanitized_portal_response_code(response.body_text) or 'none'}).",
                http_status=response.status,
            )
        raise PortalError(
            "login",
            f"Login returned unexpected HTTP {response.status}.",
            http_status=response.status,
            retryable=True,
        )

    def fetch_me(self, jwt: str) -> dict[str, Any]:
        response = self.fetch_json(
            "/api/v1/user/me",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {jwt}",
            },
            body={},
        )
        self._raise_for_stop("verify", response)
        data = response.json()
        if response.status != 200 or not isinstance(data, dict):
            raise PortalError(
                "verify",
                f"user/me returned unexpected HTTP {response.status}.",
                http_status=response.status,
                retryable=True,
            )
        return data

    # -- helpers -------------------------------------------------------------

    def _raise_for_stop(self, stage: str, response: PortalResponse) -> None:
        reason = classify_response(
            response.status, response.headers, response.body_text
        )
        if reason is None:
            return
        retryable = reason in {
            StopReason.TEMPORARY_UNAVAILABLE,
            StopReason.RATE_LIMIT,
            StopReason.WAF_CHALLENGE,
        }
        raise PortalError(
            stage,
            f"Portal stopped {stage}: {reason.value} "
            f"(code {sanitized_portal_response_code(response.body_text) or 'none'}).",
            stop_reason=reason,
            http_status=response.status,
            retryable=retryable,
        )

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{self.settings.base_url}{path}"


def _retryable_create_message(message: str) -> bool:
    lowered = (message or "").lower()
    retryable_markers = ("intente", "mas tarde", "más tarde", "momentos", "recaptcha")
    terminal_markers = ("existe", "registrado", "ya tiene", "bloquead", "inválid", "invalid")
    if any(marker in lowered for marker in terminal_markers):
        return False
    return any(marker in lowered for marker in retryable_markers)
