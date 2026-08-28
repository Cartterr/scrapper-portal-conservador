from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CREATE_TASK_URL = "https://api.2captcha.com/createTask"
GET_TASK_RESULT_URL = "https://api.2captcha.com/getTaskResult"
GET_BALANCE_URL = "https://api.2captcha.com/getBalance"


class TwoCaptchaError(RuntimeError):
    """Sanitized 2Captcha failure that never includes keys or solution tokens."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or f"2Captcha request failed ({code}).")


@dataclass(frozen=True)
class TwoCaptchaResult:
    token: str = field(repr=False)
    cost_usd: float | None = None
    task_id: int | None = field(default=None, repr=False)


class TwoCaptchaClient:
    """Small API v2 client for reCAPTCHA v3 Enterprise tasks."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 120.0,
        poll_seconds: float = 5.0,
        request_json: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise ValueError("A 2Captcha API key is required.")
        if timeout_seconds <= 0:
            raise ValueError("2Captcha timeout must be greater than zero.")
        if poll_seconds < 0:
            raise ValueError("2Captcha poll interval cannot be negative.")
        self._api_key = api_key
        self.timeout_seconds = float(timeout_seconds)
        self.poll_seconds = float(poll_seconds)
        self._request_json = request_json or _post_json
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn

    def get_balance(self) -> float:
        response = self._request_json(GET_BALANCE_URL, {"clientKey": self._api_key})
        _raise_for_api_error(response)
        try:
            return float(response["balance"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TwoCaptchaError("INVALID_RESPONSE") from exc

    def solve_recaptcha_v3_enterprise(
        self,
        *,
        website_url: str,
        website_key: str,
        page_action: str,
        min_score: float,
    ) -> str:
        return self.solve_recaptcha_v3_enterprise_result(
            website_url=website_url,
            website_key=website_key,
            page_action=page_action,
            min_score=min_score,
        ).token

    def solve_recaptcha_v3_enterprise_result(
        self,
        *,
        website_url: str,
        website_key: str,
        page_action: str,
        min_score: float,
    ) -> TwoCaptchaResult:
        create = self._request_json(
            CREATE_TASK_URL,
            {
                "clientKey": self._api_key,
                "task": {
                    "type": "RecaptchaV3TaskProxyless",
                    "websiteURL": website_url,
                    "websiteKey": website_key,
                    "minScore": min_score,
                    "pageAction": page_action,
                    "isEnterprise": True,
                    "apiDomain": "google.com",
                },
            },
        )
        _raise_for_api_error(create)
        task_id = create.get("taskId")
        if not isinstance(task_id, int):
            raise TwoCaptchaError("INVALID_RESPONSE")

        deadline = self._monotonic() + self.timeout_seconds
        while self._monotonic() < deadline:
            remaining = deadline - self._monotonic()
            self._sleep(min(self.poll_seconds, max(0.0, remaining)))
            result = self._request_json(
                GET_TASK_RESULT_URL,
                {"clientKey": self._api_key, "taskId": task_id},
            )
            _raise_for_api_error(result)
            status = result.get("status")
            if status == "processing":
                continue
            if status != "ready":
                raise TwoCaptchaError("INVALID_RESPONSE")
            solution = result.get("solution")
            token = solution.get("gRecaptchaResponse") if isinstance(solution, Mapping) else None
            if not isinstance(token, str) or not token:
                raise TwoCaptchaError("INVALID_RESPONSE")
            try:
                cost = float(result["cost"]) if result.get("cost") is not None else None
            except (TypeError, ValueError):
                cost = None
            return TwoCaptchaResult(token=token, cost_usd=cost, task_id=task_id)
        raise TwoCaptchaError("TIMEOUT")


def _raise_for_api_error(response: Mapping[str, Any]) -> None:
    try:
        error_id = int(response.get("errorId", 0))
    except (TypeError, ValueError) as exc:
        raise TwoCaptchaError("INVALID_RESPONSE") from exc
    if error_id:
        code = response.get("errorCode")
        raise TwoCaptchaError(str(code) if code else f"ERROR_{error_id}")


def _post_json(url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "user-agent": "cbrs-2captcha/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            status = int(response.status)
            body = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise TwoCaptchaError(f"HTTP_{exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise TwoCaptchaError("NETWORK_ERROR") from exc
    if status != 200:
        raise TwoCaptchaError(f"HTTP_{status}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise TwoCaptchaError("INVALID_RESPONSE") from exc
    if not isinstance(data, Mapping):
        raise TwoCaptchaError("INVALID_RESPONSE")
    return data
