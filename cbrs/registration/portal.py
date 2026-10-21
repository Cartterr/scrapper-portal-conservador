"""Portal client for the registration pipeline.

Drives a *new* headful regular-Chrome instance (fresh temporary profile, fresh
DataImpulse sticky exit). Registration fills the visible SPA form and clicks
its submit button, observing the response without duplicating the request.
Activation and verification use the portal's JSON API from that same page:

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
import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from ..browser_runtime import detect_browser, validate_native_chrome_executable
from ..browser_session import CHROME_IGNORED_DEFAULT_ARGS
from ..capsolver import CapSolverClient, CapSolverError
from ..captcha_solver import TwoCaptchaClient, TwoCaptchaError
from ..captcha_budget import CaptchaBudgetStore, CaptchaBudgetError
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
        route_retry_allowed: bool = False,
        submission_unknown: bool = False,
        response_code: str | None = None,
    ) -> None:
        self.stage = stage
        self.stop_reason = stop_reason
        self.http_status = http_status
        self.retryable = retryable
        self.route_retry_allowed = route_retry_allowed
        self.submission_unknown = submission_unknown
        self.response_code = response_code
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
            if exc.code == 407:
                raise PortalError("egress", "Proxy authentication rejected; stop candidate recovery.", http_status=407) from None
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
        self._screenshot_index = 0
        self._captcha_source = "browser"
        self._paid_solves = 0
        self._captcha_attempt: str | None = None
        self._budget: CaptchaBudgetStore | None = None
        self._capsolver_cdp: Any = None
        self._registration_payload: dict[str, Any] | None = None
        self._registration_succeeded = False
        self.egress_hash: str | None = None

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
        self._capture_screenshot("page_loaded")
        self._ensure_recaptcha_ready()
        return self

    def close(self) -> None:
        self._capture_screenshot("before_close")
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            logger.debug("Registration browser context close failed", exc_info=True)
        finally:
            if self._capsolver_cdp is not None:
                try:
                    self._capsolver_cdp.detach()
                except Exception:
                    pass
                self._capsolver_cdp = None
            self._context = None
            self._page = None
            self._stop_playwright()

    def _capture_screenshot(self, stage: str) -> None:
        """Best-effort evidence at real stages, without waiting or changing the page.

        Screenshots document the rendered page, not the submitted payload or
        whether the server accepted a reCAPTCHA token. Never record request
        bodies, browser storage, or credentials as screenshot diagnostics.
        """
        if not self.settings.capture_screenshots or self._page is None:
            return
        self._screenshot_index += 1
        path = self.profile_dir / "screenshots" / f"{self._screenshot_index:03d}-{stage}.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._page.screenshot(
                path=str(path),
                full_page=True,
                animations="disabled",
                timeout=5000,
                mask=[self._page.locator(
                    'input[type="password"], input[name*="password" i], '
                    'input[name*="token" i], textarea[name*="token" i]'
                )],
            )
        except Exception as exc:
            # A diagnostic failure must never prevent browser cleanup or mask
            # the result of an account-creation request.
            logger.warning("Registration screenshot %s failed (%s)", stage, type(exc).__name__)
        else:
            logger.info("Registration screenshot: %s", path)

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
        logger.info("Registration egress country verified: %s", country)
        raw_ip = str(data.get("ip") or "").strip()
        # Same digest format as production preflight; never log/store raw IPs.
        self.egress_hash = hashlib.sha256(raw_ip.encode()).hexdigest()[:12] if raw_ip else None
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
        self._capture_screenshot("bootstrap_ok")

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
        """Browser first; paid providers require the standalone CLI opt-in."""

        self._captcha_source = "browser"
        self._captcha_attempt = None

        try:
            token = self._page.evaluate(
                """async ({ sitekey, action }) => {
                    return await grecaptcha.enterprise.execute(sitekey, { action });
                }""",
                {"sitekey": self.settings.recaptcha_sitekey, "action": action},
            )
            if isinstance(token, str) and token:
                self._capture_screenshot(f"{action}_token_ready")
                return token
            logger.warning("Browser returned an empty reCAPTCHA token for %s", action)
        except Exception:
            logger.warning(
                "Browser reCAPTCHA execution failed for %s", action,
            )
        token = self._next_external_token(action, after="browser")
        self._capture_screenshot(f"{action}_token_ready")
        return token

    def _providers_after(self, source: str) -> list[str]:
        if not self.settings.external_captcha:
            return []
        providers = [
            name for name, key in (
                ("capsolver", self.settings.capsolver_api_key),
                ("2captcha", self.settings.two_captcha_api_key),
            ) if key
        ]
        if source == "browser":
            return providers
        return providers[providers.index(source) + 1:] if source in providers else []

    def _captcha_budget(self) -> CaptchaBudgetStore:
        if self._budget is None:
            self._budget = CaptchaBudgetStore(
                self.settings.profile_root.parent / "registration" / "captcha-budget.sqlite3",
                daily_limit=self.settings.captcha_daily_limit,
                circuit_seconds=300,
            )
        return self._budget

    def _next_external_token(self, action: str, *, after: str) -> str:
        for provider in self._providers_after(after):
            try:
                return self._external_token(action, provider)
            except (CapSolverError, TwoCaptchaError, ValueError) as exc:
                code = exc.code if isinstance(exc, (CapSolverError, TwoCaptchaError)) else "INVALID_TASK_DATA"
                logger.warning("Registration %s solver failed (%s)", provider, code)
        raise PortalError(
            "captcha", "No usable external CAPTCHA provider remains for this action.",
            stop_reason=StopReason.CAPTCHA_SOLVER, retryable=True,
        )

    def _external_token(self, action: str, provider: str) -> str:
        if self._paid_solves >= self.settings.max_paid_solves:
            raise PortalError("captcha", "Registration paid CAPTCHA task limit reached.",
                              stop_reason=StopReason.CAPTCHA_SOLVER, retryable=True)
        page_path = LOGIN_PATH if action == "login" else CREATE_PATH
        task: dict[str, Any] = dict(
            website_url=self._url(page_path), website_key=self.settings.recaptcha_sitekey,
            page_action=action, min_score=self.settings.capsolver_min_score,
        )
        if provider == "capsolver":
            user_agent = self._page.evaluate("() => navigator.userAgent")
            if not isinstance(user_agent, str) or not user_agent:
                raise ValueError("Browser user agent unavailable")
            task.update(proxy=self.proxy_url, user_agent=user_agent)
            client: Any = CapSolverClient(
                self.settings.capsolver_api_key,
                timeout_seconds=self.settings.capsolver_timeout_seconds,
                poll_seconds=self.settings.capsolver_poll_seconds,
            )
        else:
            # The repository's Enterprise-v3 2Captcha client is proxyless.
            # Never add the Chrome proxy to its task payload.
            client = TwoCaptchaClient(
                self.settings.two_captcha_api_key,
                timeout_seconds=self.settings.two_captcha_timeout_seconds,
                poll_seconds=self.settings.two_captcha_poll_seconds,
            )
        budget = self._captcha_budget()
        try:
            reservation = budget.reserve(
                account_id="registration", action=action, provider=provider,
            )
        except CaptchaBudgetError as exc:
            raise PortalError("captcha", f"Registration CAPTCHA budget stopped ({exc.code}).",
                              stop_reason=StopReason.CAPTCHA_SOLVER, retryable=True) from exc
        self._paid_solves += 1
        started = time.monotonic()
        try:
            result = client.solve_recaptcha_v3_enterprise_result(**task)
            if provider == "capsolver" and result.user_agent:
                # Same identity alignment as BrowserSession, only in this new
                # registration context. The exact browser/proxy stays alive.
                if self._capsolver_cdp is None:
                    self._capsolver_cdp = self._page.context.new_cdp_session(self._page)
                self._capsolver_cdp.send("Network.enable")
                self._capsolver_cdp.send("Network.setUserAgentOverride", {"userAgent": result.user_agent})
                if result.sec_ch_ua:
                    self._capsolver_cdp.send("Network.setExtraHTTPHeaders", {"headers": {"sec-ch-ua": result.sec_ch_ua}})
                if self._page.evaluate("() => navigator.userAgent") != result.user_agent:
                    raise CapSolverError("USER_AGENT_OVERRIDE_FAILED")
        except Exception as exc:
            code = exc.code if isinstance(exc, (CapSolverError, TwoCaptchaError)) else "SOLVER_ERROR"
            budget.finish(reservation, status="failed", error_code=code,
                          latency_seconds=time.monotonic() - started)
            budget.record_portal_outcome(reservation.attempt_id, status="not_submitted", error_code=code)
            if isinstance(exc, (CapSolverError, TwoCaptchaError, ValueError)):
                raise
            raise CapSolverError(code) from exc
        budget.finish(reservation, status="succeeded", cost_usd=result.cost_usd,
                      latency_seconds=time.monotonic() - started)
        self._captcha_source = provider
        self._captcha_attempt = reservation.attempt_id
        logger.info("Registration token generated: provider=%s action=%s", provider, action)
        self._capture_screenshot(f"{action}_{provider}_token_ready")
        return result.token

    def _captcha_request(
        self, path: str, *, action: str, payload: dict[str, Any], token: str,
        token_in_body: bool = True,
        submit: Callable[[str | None], PortalResponse] | None = None,
    ) -> PortalResponse:
        """Only explicit JSON CAPTCHA rejection may advance the provider chain.

        Never retry a success, transport failure, timeout, WAF, rate limit, or
        generic portal error. Every retry retains the exact identity and exit.
        """
        body = dict(payload)
        while True:
            attempt = self._captcha_attempt
            try:
                if token_in_body:
                    body["recaptchaToken"] = token
                if submit is not None:
                    response = submit(token if self._captcha_source != "browser" else None)
                else:
                    response = self.fetch_json(path, headers={
                        "Accept": "application/json, text/plain, */*",
                        "Content-Type": "application/json", "recaptcha-token": token,
                    }, body=body)
            except Exception:
                if attempt:
                    self._captcha_budget().record_portal_outcome(
                        attempt, status="indeterminate", error_code="transport_error")
                raise
            reason = classify_response(response.status, response.headers, response.body_text)
            rejected = (
                response.status in {200, 400} and isinstance(response.json(), dict)
                and reason == StopReason.CAPTCHA_REJECTED
            )
            code = sanitized_portal_response_code(response.body_text)
            if rejected and self._providers_after(self._captcha_source):
                source = self._captcha_source
                logger.info("Registration CAPTCHA rejected; advancing from %s for action=%s", source, action)
                try:
                    token = self._next_external_token(action, after=source)
                finally:
                    # Reserve the single secondary before recording the first
                    # rejection's cooldown, as in the worker's existing chain.
                    if attempt:
                        self._captcha_budget().record_portal_outcome(attempt, status="rejected", error_code=code)
                continue
            if attempt:
                outcome = "rejected" if rejected else (
                    "accepted" if response.status == 200 and reason is None else "indeterminate")
                self._captcha_budget().record_portal_outcome(attempt, status=outcome, error_code=code)
            return response

    # -- portal API ----------------------------------------------------------

    def prepare_registration_form(self, payload: dict[str, Any]) -> None:
        """Fill real controls and verify values without clicking submit."""
        page = self._page
        try:
            if self._registration_succeeded or urlparse(page.url).path.rstrip("/") != CREATE_PATH:
                page.goto(self._url(CREATE_PATH), wait_until="domcontentloaded", timeout=60000)
                self._ensure_recaptcha_ready()
            phone = str(payload.get("celular") or "")
            if len(phone) != 9 or not phone.startswith("9") or not phone.isdigit():
                raise PortalError("form", "Registration requires a nine-digit Chilean mobile number.", retryable=True)
            fields = {
                "email": payload["email"], "emailConfirmar": payload["emailConfirmar"],
                "password": payload["password"], "passwordConfirmar": payload["passwordConfirmar"],
                "rut": payload["rutDv"], "nombres": payload["nombres"],
                "apellidoPaterno": payload["apellidoPaterno"],
                "apellidoMaterno": payload.get("apellidoMaterno") or "",
                "direccion": payload["direccion"],
                # The visible control supplies +56 9; its model expects the
                # remaining eight digits, unlike the stored national number.
                "celular": phone[1:],
            }
            for name, value in fields.items():
                control = page.locator(f"input#{name}")
                control.fill(str(value), timeout=15000)
                control.press("Tab")
            page.locator("input#tos").check(timeout=15000)
            for name, value in fields.items():
                actual = page.locator(f"input#{name}").input_value()
                expected = str(value)
                if name == "rut":
                    actual, expected = actual.replace(".", "").upper(), expected.replace(".", "").upper()
                elif name == "celular":
                    actual = "".join(character for character in actual if character.isdigit())
                if actual != expected:
                    raise PortalError("form", f"Registration control {name} did not retain its value.", retryable=True)
            if not page.locator("input#tos").is_checked():
                raise PortalError("form", "Registration terms checkbox was not checked.", retryable=True)
            self._registration_payload = dict(payload)
            self._registration_succeeded = False
            self._capture_screenshot("form_filled")
            logger.info("Registration form filled and checked: 10 fields and terms checkbox.")
        except PortalError:
            raise
        except Exception:
            self._capture_screenshot("form_fill_failed")
            # Playwright exceptions can include field values: never expose them.
            raise PortalError("form", "Could not fill or verify the visible registration form.", retryable=True) from None

    def _submit_registration_form(self, token_override: str | None) -> PortalResponse:
        """Click the SPA button; observe its own request and response once.

        The route guard rejects mismatched form data before transmission. Only
        an explicitly rejected CAPTCHA permits substituting an external token
        into the next native submission; the SPA constructs every other field.
        """
        page = self._page
        expected = self._registration_payload
        if expected is None:
            raise PortalError("form", "Registration form has not been prepared.", retryable=True)
        sent = False
        guard_error = False

        def guard(route: Any) -> None:
            nonlocal sent, guard_error
            try:
                body = route.request.post_data_json
                keys = ("email", "emailConfirmar", "password", "passwordConfirmar", "nombres",
                        "apellidoPaterno", "direccion")
                matches = isinstance(body, dict) and all(body.get(k) == expected[k] for k in keys)
                matches = matches and (body.get("apellidoMaterno") or "") == (expected.get("apellidoMaterno") or "")
                matches = matches and str(body.get("rutDv", "")).replace(".", "").upper() == str(expected["rutDv"]).replace(".", "").upper()
                phone_digits = "".join(ch for ch in str(body.get("celular", "")) if ch.isdigit())
                matches = matches and phone_digits == str(expected["celular"])[-8:]
                matches = matches and body.get("tos") is True
                if not matches:
                    guard_error = True
                    route.abort()
                    return
                if token_override:
                    body["recaptchaToken"] = token_override
                    headers = {key: value for key, value in route.request.headers.items()
                               if key.lower() != "content-length"}
                    headers["recaptcha-token"] = token_override
                    route.continue_(headers=headers, post_data=json.dumps(body))
                else:
                    route.continue_()
                sent = True
            except Exception:
                guard_error = True
                route.abort()

        route_pattern = "**/api/v1/account/create"
        try:
            page.route(route_pattern, guard)
            self._capture_screenshot("before_create_click")
            with page.expect_response(
                lambda r: urlparse(r.url).path == "/api/v1/account/create" and r.request.method == "POST",
                timeout=60000,
            ) as pending:
                page.get_by_role("button", name="Crear cuenta", exact=True).click(timeout=15000)
            response = pending.value
            result = PortalResponse(response.status, dict(response.headers), response.text())
            self._log_response("/api/v1/account/create", result)
            return result
        except Exception:
            self._capture_screenshot("create_ui_stopped")
            if guard_error:
                raise PortalError("form", "Native registration payload did not match the filled identity; request blocked.", retryable=True) from None
            no_request_error = (
                not sent
                and page.get_by_text("Ha ocurrido un problema. Por favor, intente nuevamente.", exact=True).is_visible()
                and page.locator('input[aria-invalid="true"]').count() == 0
            )
            raise PortalError(
                "create" if sent else "form",
                "Native registration response is unknown; do not replay." if sent else "The filled form did not submit a registration request.",
                retryable=not sent,
                submission_unknown=sent,
                route_retry_allowed=no_request_error,
            ) from None
        finally:
            page.unroute(route_pattern, guard)

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
        response = PortalResponse(
            status=int(result["status"]),
            headers=dict(result["headers"]),
            body_text=str(result.get("bodyText") or ""),
        )
        self._log_response(path, response)
        return response

    def _log_response(self, path: str, response: PortalResponse) -> None:
        # Metadata only: response bodies may contain credentials or JWTs.
        logger.info(
            "Registration response: %s HTTP %d code=%s",
            path, response.status,
            sanitized_portal_response_code(response.body_text) or "none",
        )
        stage = path.rsplit("/", 1)[-1]
        self._capture_screenshot(f"{stage}_response_{response.status}")

    def create_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.prepare_registration_form(payload)
        self._captcha_source = "browser"
        self._captcha_attempt = None
        response = self._captcha_request(
            "/api/v1/account/create", action="signup", payload=payload, token="",
            submit=self._submit_registration_form,
        )
        self._raise_for_stop("create", response)
        data = response.json()
        if response.status == 200 and isinstance(data, dict) and data.get("status"):
            self._registration_succeeded = True
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
        response = self._captcha_request(
            "/api/v1/account/resend-activation-email", action="resend_activation_email",
            payload={"email": email}, token=token,
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
        response = self._captcha_request(
            "/api/v1/auth/login", action="login", payload={"email": email, "password": password},
            token=token, token_in_body=False,
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
            response_code=sanitized_portal_response_code(response.body_text),
            route_retry_allowed=(
                stage == "create" and response.status == 400
                and reason == StopReason.TEMPORARY_UNAVAILABLE
                and isinstance(response.json(), dict)
                and response.json().get("status") is not True
                and (
                    str(response.json().get("code") or "").lower() == "intente-mas-tarde"
                    or str(response.json().get("msg") or "").strip().lower()
                    == "no se pudo registrar el usuario, intente más tarde."
                )
            ),
            submission_unknown=(stage == "create" and response.status >= 500),
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
