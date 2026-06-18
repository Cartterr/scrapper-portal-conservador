from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

from .browser_session import BrowserSession
from .config import SETTINGS, Settings
from .safety import ensure_safe_response

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

        if captcha_action:
            captcha_token = self.browser.generate_recaptcha_token(captcha_action)
            headers["recaptcha-token"] = captcha_token
            if include_recaptcha_in_body:
                payload["recaptchaToken"] = captcha_token

        self._pace(context)
        response = self.browser.fetch_json(path, headers=headers, body=payload)
        ensure_safe_response(
            response.status,
            response.headers,
            response.body_text,
            context=context,
        )
        return self._parse_json(response.body_text, context=context)

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
