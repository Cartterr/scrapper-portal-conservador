from __future__ import annotations

import pytest

from cbrs.browser_session import BrowserFetchResponse, RecaptchaSolution
from cbrs.client import BrowserOriginClient
from cbrs.config import load_settings
from cbrs.safety import SafetyStopException, StopReason


class FakeBrowser:
    has_external_recaptcha_fallback = True

    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.requests: list[tuple[dict[str, str], dict]] = []
        self.outcomes: list[tuple[str, str | None]] = []

    def generate_recaptcha_token(self, _action: str) -> str:
        self.tokens.append("browser")
        return "browser-token"

    def generate_external_recaptcha_token(self, _action: str) -> str:
        self.tokens.append("2captcha")
        return "external-token"

    def record_external_recaptcha_outcome(
        self, _solution, *, status: str, error_code: str | None = None
    ) -> None:
        self.outcomes.append((status, error_code))

    def fetch_json(self, _path: str, *, headers: dict[str, str], body: dict):
        self.requests.append((dict(headers), dict(body)))
        if len(self.requests) == 1:
            return BrowserFetchResponse(
                200,
                {"content-type": "application/json"},
                '{"code":"intente-mas-tarde"}',
            )
        return BrowserFetchResponse(200, {"content-type": "application/json"}, "[]")


def test_client_retries_rejected_captcha_once_with_external_solver(tmp_path) -> None:
    browser = FakeBrowser()
    settings = load_settings(
        {"CBRS_REQUEST_DELAY_SECONDS": "3.5"},
        root=tmp_path,
    )
    client = BrowserOriginClient(browser, settings)
    client._pace = lambda _context: None
    client.ensure_auth = lambda **_kwargs: "jwt"

    result = client.post_json(
        "/api/v1/comercio/indice/texto",
        {"recaptchaToken": None},
        captcha_action="indice_com_texto",
        include_recaptcha_in_body=True,
        context="commerce search",
    )

    assert result == []
    assert browser.tokens == ["browser", "2captcha"]
    assert len(browser.requests) == 2
    assert browser.requests[0][0]["recaptcha-token"] == "browser-token"
    assert browser.requests[0][1]["recaptchaToken"] == "browser-token"
    assert browser.requests[1][0]["recaptcha-token"] == "external-token"
    assert browser.requests[1][1]["recaptchaToken"] == "external-token"
    assert browser.outcomes == [("accepted", None)]


def test_client_marks_double_captcha_rejection_after_exactly_one_paid_retry(tmp_path) -> None:
    browser = FakeBrowser()
    settings = load_settings({"CBRS_REQUEST_DELAY_SECONDS": "3.5"}, root=tmp_path)
    client = BrowserOriginClient(browser, settings)
    client._pace = lambda _context: None
    client.ensure_auth = lambda **_kwargs: "jwt"
    browser.fetch_json = lambda *_args, **_kwargs: BrowserFetchResponse(
        200, {"content-type": "application/json"}, '{"code":"intente-mas-tarde"}'
    )

    with pytest.raises(SafetyStopException) as stopped:
        client.post_json(
            "/api/v1/comercio/indice/texto",
            {"recaptchaToken": None},
            captcha_action="indice_com_texto",
            include_recaptcha_in_body=True,
            context="commerce search",
        )

    assert stopped.value.reason == StopReason.CAPTCHA_REJECTED
    assert browser.tokens == ["browser", "2captcha"]
    assert browser.outcomes == [("rejected", "captcha_rejected")]


def test_external_token_temporary_portal_failure_is_indeterminate_without_retry(
    tmp_path,
) -> None:
    browser = FakeBrowser()
    browser.generate_recaptcha_solution = lambda _action: RecaptchaSolution(
        token="external-token",
        source="2captcha",
        attempt_id="captcha-safe-id",
    )
    calls = []

    def temporary_failure(*_args, **_kwargs):
        calls.append(1)
        return BrowserFetchResponse(
            400,
            {"content-type": "application/json"},
            '{"code":"error","msg":"Problemas obteniendo índice, intente más tarde."}',
        )

    browser.fetch_json = temporary_failure
    client = BrowserOriginClient(browser, load_settings({}, root=tmp_path))
    client._pace = lambda _context: None
    client.ensure_auth = lambda **_kwargs: "jwt"

    with pytest.raises(SafetyStopException) as stopped:
        client.post_json(
            "/api/v1/comercio/indice/texto",
            {"recaptchaToken": None},
            captcha_action="indice_com_texto",
            include_recaptcha_in_body=True,
            context="commerce search",
        )

    assert stopped.value.reason == StopReason.TEMPORARY_UNAVAILABLE
    assert browser.outcomes == [("indeterminate", "temporary_unavailable")]
    assert len(calls) == 1
