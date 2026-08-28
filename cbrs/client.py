from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

from .browser_session import BrowserSession, RecaptchaSolution
from .config import SETTINGS, Settings
from .safety import SafetyStopException, StopReason, ensure_safe_response

logger = logging.getLogger(__name__)


class BrowserOriginClient:
    def __init__(self, browser: BrowserSession, settings: Settings = SETTINGS) -> None:
        self.browser = browser
        self.settings = settings
        self._jwt: str | None = None
        self._jwt_expires_at: float | None = None
        self._image_session = None

    def close(self) -> None:
        if self._image_session is not None:
            self._image_session.close()
            self._image_session = None

    def invalidate_auth(self) -> None:
        self._jwt = None
        self._jwt_expires_at = None

    def ensure_auth(self, *, force: bool = False) -> str:
        self.browser.require_login_cookie()
        if self._jwt and not force and self._jwt_is_fresh():
            return self._jwt

        self._pace("auth refresh")
        response = self.browser.fetch_json(
            "/api/v1/auth/refresh",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            body={},
        )
        ensure_safe_response(
            response.status,
            response.headers,
            response.body_text,
            context="auth refresh",
        )
        data = self._parse_json(response.body_text, context="auth refresh")
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Auth refresh did not return a token.")
        self._jwt = token
        self._jwt_expires_at = _jwt_expires_at(token)
        self.browser.set_auth_cookie(token)
        return token

    def post_json(
        self,
        path: str,
        body: dict[str, Any],
        *,
        captcha_action: str | None = None,
        include_recaptcha_in_body: bool = False,
        auth: bool = True,
        context: str,
    ) -> Any:
        payload = dict(body)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
        }

        if auth:
            headers["Authorization"] = f"Bearer {self.ensure_auth()}"

        solution: RecaptchaSolution | None = None
        if captcha_action:
            solution = self._generate_recaptcha_solution(captcha_action)
            self._set_captcha_token(
                headers,
                payload,
                solution.token,
                include_in_body=include_recaptcha_in_body,
            )

        try:
            response = self._post_json_response(path, headers, payload, context=context)
        except Exception:
            self._record_external_outcome(
                solution,
                status="not_submitted",
                error_code="transport_error",
            )
            raise
        try:
            ensure_safe_response(
                response.status,
                response.headers,
                response.body_text,
                context=context,
            )
        except SafetyStopException as exc:
            self._record_external_outcome(
                solution,
                status=(
                    "rejected"
                    if exc.reason == StopReason.CAPTCHA_REJECTED
                    else "indeterminate"
                ),
                error_code=exc.reason.value,
            )
            if (
                exc.reason != StopReason.CAPTCHA_REJECTED
                or not captcha_action
                or (solution is not None and solution.source == "2captcha")
                or not self.browser.has_external_recaptcha_fallback
            ):
                raise
            logger.info("Retrying rejected %s CAPTCHA once with 2Captcha", context)
            solution = self._generate_recaptcha_solution(captcha_action, external=True)
            self._set_captcha_token(
                headers,
                payload,
                solution.token,
                include_in_body=include_recaptcha_in_body,
            )
            try:
                response = self._post_json_response(path, headers, payload, context=context)
                ensure_safe_response(
                    response.status,
                    response.headers,
                    response.body_text,
                    context=context,
                )
            except SafetyStopException as external_exc:
                self._record_external_outcome(
                    solution,
                    status=(
                        "rejected"
                        if external_exc.reason == StopReason.CAPTCHA_REJECTED
                        else "indeterminate"
                    ),
                    error_code=external_exc.reason.value,
                )
                raise
            except Exception:
                self._record_external_outcome(
                    solution,
                    status="not_submitted",
                    error_code="transport_error",
                )
                raise
        self._record_external_outcome(solution, status="accepted")
        return self._parse_json(response.body_text, context=context)

    def _generate_recaptcha_solution(
        self,
        action: str,
        *,
        external: bool = False,
    ) -> RecaptchaSolution:
        method_name = (
            "generate_external_recaptcha_solution"
            if external
            else "generate_recaptcha_solution"
        )
        method = getattr(self.browser, method_name, None)
        if callable(method):
            return method(action)
        token_method = (
            self.browser.generate_external_recaptcha_token
            if external
            else self.browser.generate_recaptcha_token
        )
        return RecaptchaSolution(
            token=token_method(action),
            source="2captcha" if external else "browser",
        )

    def _record_external_outcome(
        self,
        solution: RecaptchaSolution | None,
        *,
        status: str,
        error_code: str | None = None,
    ) -> None:
        if solution is None or solution.source != "2captcha":
            return
        recorder = getattr(self.browser, "record_external_recaptcha_outcome", None)
        if callable(recorder):
            recorder(solution, status=status, error_code=error_code)

    def _post_json_response(
        self,
        path: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        *,
        context: str,
    ):
        self._pace(context)
        return self.browser.fetch_json(path, headers=headers, body=payload)

    @staticmethod
    def _set_captcha_token(
        headers: dict[str, str],
        payload: dict[str, Any],
        token: str,
        *,
        include_in_body: bool,
    ) -> None:
        headers["recaptcha-token"] = token
        if include_in_body:
            payload["recaptchaToken"] = token

    def get_bytes(self, path: str, *, context: str) -> bytes:
        self.ensure_auth()
        if self.settings.use_curl_cffi_for_images:
            return self._get_bytes_with_curl_cffi(path, context=context)
        return self._get_bytes_with_browser(path, context=context)

    def _get_bytes_with_browser(self, path: str, *, context: str) -> bytes:
        self._pace(context)
        response = self.browser.fetch_bytes(
            path,
            headers={
                "Accept": "image/jpeg,image/*,*/*",
            },
        )
        content = base64.b64decode(response.body_base64 or "")
        ensure_safe_response(
            response.status,
            response.headers,
            content[:2048],
            expected="image",
            context=context,
        )
        if not content:
            raise RuntimeError(f"{context} returned an empty body.")
        return content

    def _get_bytes_with_curl_cffi(self, path: str, *, context: str) -> bytes:
        if self._image_session is None:
            from curl_cffi.requests import Session

            self._image_session = Session(impersonate=self.settings.curl_cffi_impersonate)

        domain = urlparse(self.settings.base_url).hostname
        if not domain:
            raise RuntimeError("Cannot determine CBRS cookie domain.")
        for cookie in self.browser.export_cookies():
            self._image_session.cookies.set(cookie["name"], cookie["value"], domain=domain)

        self._pace(context)
        response = self._image_session.get(
            f"{self.settings.base_url}{path}",
            headers={"Accept": "image/jpeg,image/*,*/*", "Referer": self.settings.commerce_url},
        )
        content = response.content
        ensure_safe_response(
            response.status_code,
            dict(response.headers),
            content[:2048],
            expected="image",
            context=context,
        )
        return content

    def _pace(self, context: str) -> None:
        delay = self.settings.delay_seconds()
        logger.debug("Waiting %.2fs before %s", delay, context)
        time.sleep(delay)

    def _jwt_is_fresh(self) -> bool:
        if self._jwt_expires_at is None:
            return True
        return self._jwt_expires_at > time.time() + 60

    @staticmethod
    def _parse_json(body_text: str | None, *, context: str) -> Any:
        try:
            return json.loads(body_text or "")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{context} did not return valid JSON.") from exc


def _jwt_expires_at(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        exp = data.get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        return None
