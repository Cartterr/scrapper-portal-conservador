from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

from .browser_session import BrowserSession
from .config import AccountConfig, Settings
from .errors import ErrorCode, PortalCallError, classify_response
from .safety import LiveSafetyGovernor, SafetyPolicy, SafetyStop

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PortalResponse:
    status: int
    data: Any
    headers: dict[str, str]


class PortalClient:
    """curl_cffi portal client synchronized with the persistent browser session."""

    def __init__(self, settings: Settings, browser: BrowserSession, store):
        self.settings = settings
        self.browser = browser
        self.safety = LiveSafetyGovernor(
            SafetyPolicy(
                min_request_delay_ms=settings.min_request_delay_ms,
                request_jitter_percent=settings.request_jitter_percent,
                transient_backoff_ms=settings.transient_backoff_ms,
            ),
            profile_path=settings.browser_profile_dir,
            store=store,
            owner=f"{os.getpid()}:{uuid.uuid4().hex[:8]}",
        )
        self._jwt: str | None = None
        try:
            from curl_cffi.requests import Session
        except Exception as exc:  # pragma: no cover - dependency check path
            raise RuntimeError("curl_cffi is not installed. Run: python -m pip install -e .") from exc
        self._session = Session(impersonate=settings.curl_impersonate)

    def close(self) -> None:
        self._session.close()

    def __enter__(self):
        self.safety.acquire()
        return self

    def __exit__(self, *args):
        self.safety.release()
        self.close()

    def sync_browser_cookies(self) -> None:
        for name, value in self.browser.cookies().items():
            self._session.cookies.set(name, value, domain="nuevo-portal.conservador.cl")

    def set_jwt_from_browser(self) -> bool:
        token = self.browser.current_jwt()
        if not token:
            return False
        self._jwt = token
        self._session.cookies.set("auth_cbrs_token", f'"{token}"', domain="nuevo-portal.conservador.cl")
        return True

    def ensure_auth(self, account: AccountConfig | None = None) -> None:
        self.browser.start()
        self.sync_browser_cookies()
        if self.refresh_auth():
            logger.info("CBRS auth refreshed from persistent browser/session state")
            return
        if self.set_jwt_from_browser():
            logger.info("CBRS auth loaded from persistent browser profile")
            return
        if account:
            logger.info("CBRS auth requires credential login for configured account %s", account.label)
            state = self.browser.login_with_credentials(account.email, account.password)
            if not state.logged_in or not self.set_jwt_from_browser():
                raise RuntimeError("CBRS login failed")
            self.sync_browser_cookies()
            return
        self.safety.store.set_safety_state(
            state="auth_required",
            signal=str(ErrorCode.AUTH),
            endpoint="/api/v1/auth/refresh",
            status=401,
            reason="CBRS login required",
            profile_path=self.settings.browser_profile_dir,
            operator_action="run `cbrs init` or configure CBRS_USER_1/CBRS_PASSWORD_1",
        )
        raise SafetyStop("CBRS login required. Run `cbrs init` or configure credentials.")

    def refresh_auth(self) -> bool:
        try:
            result = self.post_json("/api/v1/auth/refresh", {}, auth=False, check=False)
        except SafetyStop:
            raise
        except Exception:
            return False
        if result.status == 200 and isinstance(result.data, dict) and result.data.get("token"):
            self._jwt = result.data["token"]
            self._session.cookies.set(
                "auth_cbrs_token", f'"{self._jwt}"', domain="nuevo-portal.conservador.cl"
            )
            return True
        return False

    def home_start(self) -> PortalResponse:
        return self.post_json("/api/v1/home/start", {"preHint": ""}, auth=False)

    def user_me(self) -> PortalResponse:
        return self.post_json("/api/v1/user/me", {}, auth=True)

    def post_json(
        self,
        path: str,
        body: dict[str, Any],
        *,
        auth: bool,
        captcha_token: str | None = None,
        check: bool = True,
    ) -> PortalResponse:
        self.safety.before_request(path)
        self._throttle()
        headers = self._headers(path, auth=auth, captcha_token=captcha_token)
        response = self._session.post(
            f"{self.settings.base_url}{path}",
            headers=headers,
            json=body,
            timeout=60,
        )
        data = _decode_response(response)
        portal_response = PortalResponse(response.status_code, data, dict(response.headers))
        classified = classify_response(
            portal_response.status, portal_response.data, portal_response.headers, endpoint=path
        )
        self.safety.after_response(path, status=portal_response.status, classified=classified)
        if check:
            self._raise_if_error(path, portal_response, classified=classified)
        return portal_response

    def get_bytes(self, path: str, *, auth: bool = False, check: bool = True) -> tuple[int, bytes, dict[str, str]]:
        self.safety.before_request(path)
        self._throttle()
        headers = self._headers(path, auth=auth, captcha_token=None)
        response = self._session.get(
            f"{self.settings.base_url}{path}",
            headers=headers,
            timeout=60,
        )
        headers_out = dict(response.headers)
        data_for_classification = response.text if _is_text_response(headers_out) else None
        classified = classify_response(
            response.status_code,
            data_for_classification,
            headers_out,
            endpoint=path,
        )
        self.safety.after_response(path, status=response.status_code, classified=classified)
        if check:
            if classified.code is not ErrorCode.OK:
                raise PortalCallError(response.status_code, classified, endpoint=path)
        return response.status_code, response.content, headers_out

    def _headers(self, path: str, *, auth: bool, captcha_token: str | None) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Origin": self.settings.base_url,
            "Referer": f"{self.settings.base_url}/",
        }
        if path.startswith("/api/v1/comercio/indice"):
            headers["Referer"] = (
                f"{self.settings.base_url}"
                "/consultas-en-linea/indices/indice-del-registro-de-comercio"
            )
        if auth:
            if not self._jwt and not self.set_jwt_from_browser():
                raise RuntimeError("No JWT available for authenticated request")
            headers["Authorization"] = f"Bearer {self._jwt}"
        if captcha_token:
            headers["recaptcha-token"] = captcha_token
        return headers

    def _raise_if_error(
        self,
        endpoint: str,
        response: PortalResponse,
        *,
        classified=None,
    ) -> None:
        classified = classified or classify_response(
            response.status, response.data, response.headers, endpoint=endpoint
        )
        if classified.code is not ErrorCode.OK:
            raise PortalCallError(response.status, classified, endpoint=endpoint)

def _decode_response(response) -> Any:
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            return response.json()
        except json.JSONDecodeError:
            return response.text
    text = response.text
    if text and len(text) < 500:
        return text
    return {"contentType": content_type, "bytes": len(response.content)}


def _is_text_response(headers: dict[str, str]) -> bool:
    content_type = headers.get("content-type", "")
    return "text/" in content_type or "html" in content_type or "json" in content_type
