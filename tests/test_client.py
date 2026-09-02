from __future__ import annotations

import base64

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
                '{"code":"captcha-invalid","msg":"captcha verification failed"}',
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
        200,
        {"content-type": "application/json"},
        '{"code":"captcha-invalid","msg":"captcha verification failed"}',
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
    assert browser.outcomes == [
        ("rejected", "captcha_rejected:http_200:captcha-invalid")
    ]


def test_client_uses_one_secondary_2captcha_retry_after_capsolver_rejection(
    tmp_path,
) -> None:
    class ChainedBrowser(FakeBrowser):
        has_secondary_external_recaptcha_fallback = True

        def generate_recaptcha_solution(self, _action: str) -> RecaptchaSolution:
            self.tokens.append("browser")
            return RecaptchaSolution("browser-token", "browser")

        def generate_external_recaptcha_solution(self, _action: str) -> RecaptchaSolution:
            self.tokens.append("capsolver")
            return RecaptchaSolution("capsolver-token", "capsolver", "cap-attempt")

        def generate_secondary_external_recaptcha_solution(
            self, _action: str
        ) -> RecaptchaSolution:
            self.tokens.append("2captcha")
            return RecaptchaSolution("2captcha-token", "2captcha", "two-attempt")

        def fetch_json(self, _path: str, *, headers: dict[str, str], body: dict):
            self.requests.append((dict(headers), dict(body)))
            if len(self.requests) < 3:
                return BrowserFetchResponse(
                    200,
                    {"content-type": "application/json"},
                    '{"code":"captcha-invalid","msg":"captcha verification failed"}',
                )
            return BrowserFetchResponse(200, {"content-type": "application/json"}, "[]")

    browser = ChainedBrowser()
    client = BrowserOriginClient(browser, load_settings({}, root=tmp_path))
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
    assert browser.tokens == ["browser", "capsolver", "2captcha"]
    assert len(browser.requests) == 3
    assert browser.requests[2][0]["recaptcha-token"] == "2captcha-token"
    assert browser.outcomes == [
        ("rejected", "captcha_rejected:http_200:captcha-invalid"),
        ("accepted", None),
    ]


def test_generic_retry_code_does_not_spend_external_captcha(tmp_path) -> None:
    browser = FakeBrowser()
    browser.fetch_json = lambda *_args, **_kwargs: BrowserFetchResponse(
        400,
        {"content-type": "application/json"},
        '{"code":"intente-mas-tarde"}',
    )
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
    assert browser.tokens == ["browser"]
    assert browser.outcomes == []


@pytest.mark.parametrize("source", ["2captcha", "capsolver"])
def test_external_token_temporary_portal_failure_is_indeterminate_without_retry(
    tmp_path, source,
) -> None:
    browser = FakeBrowser()
    browser.generate_recaptcha_solution = lambda _action: RecaptchaSolution(
        token="external-token",
        source=source,
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
    assert browser.outcomes == [
        ("indeterminate", "temporary_unavailable:http_400:error")
    ]
    assert len(calls) == 1


def test_image_download_retries_one_browser_transport_failure(tmp_path) -> None:
    class ImageBrowser:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_bytes(self, _path: str, *, headers: dict[str, str]):
            self.calls += 1
            assert headers["Accept"].startswith("image/jpeg")
            if self.calls == 1:
                raise RuntimeError("Failed to fetch")
            return BrowserFetchResponse(
                200,
                {"content-type": "image/jpeg"},
                body_base64=base64.b64encode(b"jpeg-bytes").decode("ascii"),
            )

    browser = ImageBrowser()
    client = BrowserOriginClient(browser, load_settings({}, root=tmp_path))
    client._pace = lambda _context: None
    client.ensure_auth = lambda **_kwargs: "jwt"

    assert client.get_bytes("/image", context="image download") == b"jpeg-bytes"
    assert browser.calls == 2
