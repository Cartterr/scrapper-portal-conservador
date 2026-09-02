import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from cbrs.browser_session import (
    BrowserFetchResponse,
    BrowserSession,
    CommerceAuthState,
    CredentialsRejectedError,
    RecaptchaSolution,
)
from cbrs.capsolver import CapSolverError, CapSolverResult
from cbrs.captcha_solver import TwoCaptchaError
from cbrs.captcha_budget import CaptchaBudgetStore
from cbrs.safety import SafetyStopException, StopReason
from cbrs.config import load_settings


def test_browser_session_defaults_to_headless_settings(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)

    session = BrowserSession(settings)

    assert session.headless is True


def test_browser_session_launches_chrome_persistent_context(tmp_path: Path, monkeypatch) -> None:
    settings = load_settings({}, root=tmp_path)
    browser = tmp_path / "chrome.exe"
    captured = {}

    class FakeContext:
        pages = []

        def close(self):
            captured["closed"] = True

    class FakeChromium:
        def launch_persistent_context(self, user_data_dir, **kwargs):
            captured["user_data_dir"] = user_data_dir
            captured["kwargs"] = kwargs
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

        def stop(self):
            captured["stopped"] = True

    class FakeSyncPlaywright:
        def start(self):
            captured["started"] = True
            return FakePlaywright()

    monkeypatch.setattr(
        "cbrs.browser_session.detect_browser",
        lambda loaded_settings: SimpleNamespace(path=browser, family="chrome", source="auto"),
    )
    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: FakeSyncPlaywright()),
    )

    session = BrowserSession(settings, headless=False)
    session.open()
    session.close()

    assert captured["user_data_dir"] == str(settings.profile_dir)
    assert captured["kwargs"]["executable_path"] == str(browser)
    assert captured["kwargs"]["headless"] is False
    assert captured["kwargs"]["accept_downloads"] is True
    assert captured["kwargs"]["bypass_csp"] is False
    assert captured["kwargs"]["chromium_sandbox"] is True
    assert captured["closed"] is True
    assert captured["stopped"] is True


def test_browser_sessions_share_one_sync_runtime_until_last_context_closes(
    tmp_path: Path, monkeypatch
) -> None:
    starts = []
    stops = []
    contexts = []

    class FakeContext:
        pages = []

        def close(self):
            return None

    class FakeChromium:
        def launch_persistent_context(self, user_data_dir, **kwargs):
            context = FakeContext()
            contexts.append((user_data_dir, context))
            return context

    class FakePlaywright:
        chromium = FakeChromium()

        def stop(self):
            stops.append(True)

    class FakeSyncPlaywright:
        def start(self):
            starts.append(True)
            return FakePlaywright()

    monkeypatch.setattr(
        "cbrs.browser_session.detect_browser",
        lambda _settings: SimpleNamespace(
            path=tmp_path / "chrome.exe", family="chrome", source="auto"
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: FakeSyncPlaywright()),
    )
    first = BrowserSession(load_settings({}, root=tmp_path / "one"), headless=True)
    second = BrowserSession(load_settings({}, root=tmp_path / "two"), headless=True)

    first.open()
    second.open()
    assert len(starts) == 1
    assert len(contexts) == 2
    first.close()
    assert stops == []
    second.close()
    assert stops == [True]


def test_browser_session_stops_playwright_when_context_launch_fails(
    tmp_path: Path, monkeypatch
) -> None:
    settings = load_settings({}, root=tmp_path)
    captured = {}

    class FakeChromium:
        def launch_persistent_context(self, user_data_dir, **kwargs):
            raise RuntimeError("launch failed")

    class FakePlaywright:
        chromium = FakeChromium()

        def stop(self):
            captured["stopped"] = True

    class FakeSyncPlaywright:
        def start(self):
            return FakePlaywright()

    monkeypatch.setattr(
        "cbrs.browser_session.detect_browser",
        lambda loaded_settings: SimpleNamespace(
            path=tmp_path / "chrome", family="chrome", source="auto"
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: FakeSyncPlaywright()),
    )

    session = BrowserSession(settings)
    with pytest.raises(RuntimeError, match="launch failed"):
        session.open()

    assert captured["stopped"] is True
    assert session._playwright is None


def test_browser_session_launches_chrome_offscreen_when_configured(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = load_settings({"CBRS_WINDOW_MODE": "offscreen"}, root=tmp_path)
    browser = tmp_path / "chrome.exe"
    captured = {}

    class FakeContext:
        pages = []

        def close(self):
            pass

    class FakeChromium:
        def launch_persistent_context(self, user_data_dir, **kwargs):
            captured["kwargs"] = kwargs
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

        def stop(self):
            pass

    class FakeSyncPlaywright:
        def start(self):
            return FakePlaywright()

    monkeypatch.setattr(
        "cbrs.browser_session.detect_browser",
        lambda loaded_settings: SimpleNamespace(path=browser, family="chrome", source="auto"),
    )
    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: FakeSyncPlaywright()),
    )

    with BrowserSession(settings, headless=False):
        pass

    assert captured["kwargs"]["headless"] is False
    assert "--window-size=1366,900" in captured["kwargs"]["args"]
    assert "--window-position=-32000,-32000" in captured["kwargs"]["args"]


def test_browser_session_launches_chrome_with_proxy(tmp_path: Path, monkeypatch) -> None:
    settings = load_settings(
        {
            "CBRS_EGRESS_MODE": "dedicated_static_isp",
            "CBRS_PROXY_URL": "http://proxy-user:proxy-pass@example.test:33335",
        },
        root=tmp_path,
    )
    browser = tmp_path / "chrome.exe"
    captured = {}

    class FakeContext:
        pages = []

        def close(self):
            pass

    class FakeChromium:
        def launch_persistent_context(self, user_data_dir, **kwargs):
            captured["kwargs"] = kwargs
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

        def stop(self):
            pass

    class FakeSyncPlaywright:
        def start(self):
            return FakePlaywright()

    monkeypatch.setattr(
        "cbrs.browser_session.detect_browser",
        lambda loaded_settings: SimpleNamespace(path=browser, family="chrome", source="auto"),
    )
    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: FakeSyncPlaywright()),
    )

    with BrowserSession(settings, headless=False):
        pass

    assert captured["kwargs"]["proxy"] == {
        "server": "http://example.test:33335",
        "username": "proxy-user",
        "password": "proxy-pass",
    }


def test_browser_session_rejects_proxy_in_chrome_backend(tmp_path: Path) -> None:
    settings = load_settings(
        {"CBRS_CLOAK_PROXY_URL": "socks5://user:pass@example.test:1234"},
        root=tmp_path,
    )
    session = BrowserSession(settings)

    try:
        session.open()
    except RuntimeError as exc:
        assert "CBRS_CLOAK_PROXY_URL" in str(exc)
    else:
        raise AssertionError("Expected chrome backend to reject proxy config")


def test_browser_session_accepts_actual_portal_login_cookie(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)

    class FakeContext:
        def cookies(self, urls):
            return [{"name": "auth_cbrs_token", "value": "[REDACTED]"}]

    session = BrowserSession(settings)
    session._context = FakeContext()

    assert session.has_login_cookie() is True


def test_browser_session_accepts_refresh_cookie(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)

    class FakeContext:
        def cookies(self, urls):
            return [{"name": "cbrs_refresh_token", "value": "[REDACTED]"}]

    session = BrowserSession(settings)
    session._context = FakeContext()

    assert session.has_login_cookie() is True


def test_browser_session_checks_auth_refresh_cookie_path(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)
    captured = {}

    class FakeContext:
        def cookies(self, urls):
            captured["urls"] = urls
            if any(url.endswith("/api/v1/auth/refresh") for url in urls):
                return [{"name": "cbrs_refresh_token", "value": "[REDACTED]"}]
            return []

    session = BrowserSession(settings)
    session._context = FakeContext()

    assert session.has_login_cookie() is True
    assert f"{settings.base_url}/api/v1/auth/refresh" in captured["urls"]


def test_browser_session_rejects_stay_signed_in_cookie_without_tokens(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)

    class FakeContext:
        def cookies(self, urls):
            return [{"name": "auth_cbrs_stay_signed_in", "value": "[REDACTED]"}]

    session = BrowserSession(settings)
    session._context = FakeContext()

    assert session.has_login_cookie() is False


def test_browser_session_rejects_stale_login_cookie(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)
    session = BrowserSession(settings)
    session.has_login_cookie = lambda: True
    session.goto_index = lambda: None
    session.wait_for_commerce_auth_state = (
        lambda: CommerceAuthState.AUTHENTICATED_FORM
    )
    session.fetch_json = lambda *args, **kwargs: BrowserFetchResponse(
        status=401,
        headers={},
        body_text='{"detail":"Token requerido"}',
    )

    assert session.has_active_login() is False


def test_browser_session_accepts_cookie_after_successful_auth_refresh(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)
    captured = {}
    session = BrowserSession(settings)
    session.has_login_cookie = lambda: True
    origin_loaded = []
    session.goto_index = lambda: origin_loaded.append(True)
    session.wait_for_commerce_auth_state = (
        lambda: CommerceAuthState.AUTHENTICATED_FORM
    )
    session.fetch_json = lambda *args, **kwargs: BrowserFetchResponse(
        status=200,
        headers={},
        body_text='{"token":"fresh.jwt.token"}',
    )
    session.set_auth_cookie = lambda token: captured.setdefault("token", token)
    session.reload_current_page = lambda: captured.setdefault("reloaded", True)
    session._context = SimpleNamespace(
        pages=[SimpleNamespace(wait_for_timeout=lambda _milliseconds: None)]
    )

    assert session.has_active_login() is True
    assert origin_loaded == [True]
    assert captured["token"] == "fresh.jwt.token"
    assert captured["reloaded"] is True


def test_browser_session_detects_explicit_commerce_login_gate(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)

    class FakePage:
        def evaluate(self, script):
            assert "div.m3-card-outlined" in script
            assert "h2.m3-title-large" in script
            assert "a[href^='/login/']" in script
            assert "a[href='/crear-cuenta']" in script
            assert "Para acceder debe iniciar sesión" in script
            assert "section.m3-card-outlined" in script
            assert "#input-fojas" in script
            assert "#input-numero" in script
            assert "#input-ano" in script
            assert 'buttons.includes("Buscar")' in script
            assert 'buttons.includes("Limpiar")' in script
            return "login_gate"

    session = BrowserSession(settings)
    session._context = SimpleNamespace(pages=[FakePage()])

    assert session.page_requires_login() is True


def test_browser_session_does_not_infer_login_gate_without_complete_card(
    tmp_path: Path,
) -> None:
    settings = load_settings({}, root=tmp_path)
    session = BrowserSession(settings)
    session._context = SimpleNamespace(
        pages=[SimpleNamespace(evaluate=lambda _script: "unknown")]
    )

    assert session.page_requires_login() is False


def test_browser_session_rejects_login_gate_after_successful_refresh(
    tmp_path: Path,
) -> None:
    settings = load_settings({}, root=tmp_path)
    session = BrowserSession(settings)
    session.has_login_cookie = lambda: True
    session.goto_index = lambda: None
    session.wait_for_commerce_auth_state = lambda: CommerceAuthState.LOGIN_GATE
    session.fetch_json = lambda *args, **kwargs: BrowserFetchResponse(
        status=200,
        headers={},
        body_text='{"token":"fresh.jwt.token"}',
    )
    captured = {"reloads": 0}
    session.set_auth_cookie = lambda _token: None
    session.reload_current_page = lambda: captured.update(
        {"reloads": captured["reloads"] + 1}
    )
    session._context = SimpleNamespace(
        pages=[SimpleNamespace(wait_for_timeout=lambda _milliseconds: None)]
    )

    assert session.has_active_login() is False
    assert captured["reloads"] == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("authenticated_form", CommerceAuthState.AUTHENTICATED_FORM),
        ("login_gate", CommerceAuthState.LOGIN_GATE),
        ("conflict", CommerceAuthState.CONFLICT),
        ("unknown", CommerceAuthState.UNKNOWN),
        ("partial_form", CommerceAuthState.UNKNOWN),
    ],
)
def test_commerce_auth_state_is_explicit_and_fail_closed(
    tmp_path: Path, raw: str, expected: CommerceAuthState
) -> None:
    session = BrowserSession(load_settings({}, root=tmp_path))
    session._context = SimpleNamespace(
        pages=[SimpleNamespace(evaluate=lambda _script: raw)]
    )

    assert session.detect_commerce_auth_state() is expected


def test_wait_for_commerce_auth_state_accepts_delayed_protected_form(
    tmp_path: Path,
) -> None:
    session = BrowserSession(load_settings({}, root=tmp_path))
    states = iter(
        [CommerceAuthState.UNKNOWN, CommerceAuthState.AUTHENTICATED_FORM]
    )
    session.detect_commerce_auth_state = lambda: next(states)
    waits: list[int] = []
    session._context = SimpleNamespace(
        pages=[SimpleNamespace(wait_for_timeout=lambda value: waits.append(value))]
    )

    assert (
        session.wait_for_commerce_auth_state(timeout_ms=1000, poll_ms=100)
        is CommerceAuthState.AUTHENTICATED_FORM
    )
    assert waits == [100]


def test_ensure_authenticated_reuses_a_valid_persistent_session(tmp_path: Path) -> None:
    session = BrowserSession(load_settings({}, root=tmp_path))
    session.open = lambda: session
    session.has_active_login = lambda: True
    session._login_with_fetch = lambda *_args: (_ for _ in ()).throw(
        AssertionError("login should not run")
    )

    assert session.ensure_authenticated(None, None) == "refreshed"


def test_ensure_authenticated_uses_browser_fetch_and_confirms_refresh(tmp_path: Path) -> None:
    session = BrowserSession(load_settings({}, root=tmp_path))
    session.open = lambda: session
    states = iter([False, True])
    session.has_active_login = lambda: next(states)
    captured = {}
    session._login_with_fetch = lambda username, password: captured.update(
        {"username": username, "password": password}
    )

    assert session.ensure_authenticated("operator@example.test", "private") == "browser_fetch"
    assert captured == {"username": "operator@example.test", "password": "private"}


def test_ensure_authenticated_does_not_form_retry_rejected_credentials(tmp_path: Path) -> None:
    session = BrowserSession(load_settings({}, root=tmp_path))
    session.open = lambda: session
    session.has_active_login = lambda: False
    session._login_with_fetch = lambda *_args: (_ for _ in ()).throw(
        CredentialsRejectedError("rejected")
    )
    session._login_with_form = lambda *_args: (_ for _ in ()).throw(
        AssertionError("form fallback must not repeat rejected credentials")
    )

    with pytest.raises(CredentialsRejectedError):
        session.ensure_authenticated("operator@example.test", "private")


def test_login_response_preserves_generic_retry_as_temporary_stop(tmp_path: Path) -> None:
    session = BrowserSession(load_settings({}, root=tmp_path))
    with pytest.raises(SafetyStopException) as error:
        session._check_login_response(
            BrowserFetchResponse(
                status=400,
                headers={"content-type": "application/json"},
                body_text='{"code":"intente-mas-tarde"}',
            )
        )
    assert error.value.reason == StopReason.TEMPORARY_UNAVAILABLE


def test_login_response_preserves_sanitized_rejection_metadata(tmp_path: Path) -> None:
    session = BrowserSession(load_settings({}, root=tmp_path))
    with pytest.raises(CredentialsRejectedError) as error:
        session._check_login_response(
            BrowserFetchResponse(
                status=422,
                headers={"content-type": "application/json"},
                body_text='{"code":"AUTH-INVALID","msg":"private portal detail"}',
            )
        )
    assert error.value.status == 422
    assert error.value.response_code == "auth-invalid"
    assert "private portal detail" not in str(error.value)


def test_external_solver_uses_current_portal_page_and_enterprise_action(
    tmp_path: Path, monkeypatch
) -> None:
    settings = load_settings(
        {
            "CBRS_CAPTCHA_SOLVER_MODE": "2captcha",
            "CBRS_2CAPTCHA_API_KEY": "private-key",
        },
        root=tmp_path,
    )
    session = BrowserSession(settings)
    session._context = SimpleNamespace(
        pages=[SimpleNamespace(url=f"{settings.base_url}/login")]
    )
    captured = {}

    class FakeSolver:
        def __init__(self, api_key, **kwargs):
            captured["api_key"] = api_key
            captured["client"] = kwargs

        def solve_recaptcha_v3_enterprise(self, **kwargs):
            captured["task"] = kwargs
            return "external-token"

    monkeypatch.setattr("cbrs.browser_session.TwoCaptchaClient", FakeSolver)

    assert session.generate_recaptcha_token("login") == "external-token"
    assert captured["api_key"] == "private-key"
    assert captured["task"] == {
        "website_url": f"{settings.base_url}/login",
        "website_key": settings.recaptcha_sitekey,
        "page_action": "login",
        "min_score": 0.9,
    }


def test_capsolver_uses_account_proxy_and_active_browser_user_agent(
    tmp_path: Path, monkeypatch
) -> None:
    settings = replace(
        load_settings(
            {
                "CBRS_CAPTCHA_SOLVER_MODE": "capsolver",
                "CBRS_CAPSOLVER_API_KEY": "CAP-private-key",
                "CBRS_PROXY_URL": "http://user:pass@proxy.example.test:8080",
            },
            root=tmp_path,
        ),
        account_id="a1",
    )
    session = BrowserSession(settings)
    session._context = SimpleNamespace(
        pages=[
            SimpleNamespace(
                url=settings.commerce_url,
                evaluate=lambda _script: "browser-agent",
            )
        ]
    )
    captured = {}

    class FakeSolver:
        def __init__(self, api_key, **kwargs):
            captured["api_key"] = api_key
            captured["client"] = kwargs

        def solve_recaptcha_v3_enterprise(self, **kwargs):
            captured["task"] = kwargs
            return "external-token"

    monkeypatch.setattr("cbrs.browser_session.CapSolverClient", FakeSolver)

    solution = session.generate_external_recaptcha_solution("indice_com_texto")

    assert solution.token == "external-token"
    assert solution.source == "capsolver"
    assert captured["api_key"] == "CAP-private-key"
    assert captured["task"] == {
        "website_url": settings.commerce_url,
        "website_key": settings.recaptcha_sitekey,
        "page_action": "indice_com_texto",
        "min_score": 0.9,
        "proxy": "http://user:pass@proxy.example.test:8080",
        "user_agent": "browser-agent",
    }


def test_capsolver_provider_failure_falls_back_once_to_configured_2captcha(
    tmp_path: Path, monkeypatch
) -> None:
    settings = replace(
        load_settings(
            {
                "CBRS_CAPTCHA_SOLVER_MODE": "capsolver_manual",
                "CBRS_CAPSOLVER_API_KEY": "CAP-private-key",
                "CBRS_2CAPTCHA_API_KEY": "two-private-key",
                "CBRS_PROXY_URL": "http://user:pass@proxy.example.test:8080",
            },
            root=tmp_path,
        ),
        account_id="a1",
    )
    session = BrowserSession(settings)
    session._context = SimpleNamespace(
        pages=[
            SimpleNamespace(
                url=settings.commerce_url,
                evaluate=lambda _script: "browser-agent",
            )
        ]
    )
    providers: list[str] = []

    class FailingCapSolver:
        def __init__(self, *_args, **_kwargs):
            providers.append("capsolver")

        def solve_recaptcha_v3_enterprise_result(self, **_kwargs):
            raise CapSolverError("USER_AGENT_MISMATCH")

    class WorkingTwoCaptcha:
        def __init__(self, *_args, **_kwargs):
            providers.append("2captcha")

        def solve_recaptcha_v3_enterprise(self, **_kwargs):
            return "secondary-token"

    monkeypatch.setattr("cbrs.browser_session.CapSolverClient", FailingCapSolver)
    monkeypatch.setattr("cbrs.browser_session.TwoCaptchaClient", WorkingTwoCaptcha)
    budget = CaptchaBudgetStore(
        settings.captcha_state_path,
        daily_limit=settings.two_captcha_daily_limit,
        circuit_seconds=settings.two_captcha_circuit_breaker_seconds,
    )
    budget.set_automatic_enabled(True)

    solution = session.generate_external_recaptcha_solution("indice_com_texto")

    assert solution.source == "2captcha"
    assert solution.token == "secondary-token"
    assert providers == ["capsolver", "2captcha"]
    assert budget.status()["attempts"] == 2


def test_capsolver_returned_identity_is_applied_until_session_close(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)
    sent: list[tuple[str, dict | None]] = []

    class FakeCdp:
        detached = False

        def send(self, method, payload=None):
            sent.append((method, payload))

        def detach(self):
            self.detached = True

    cdp = FakeCdp()

    class FakePage:
        context = SimpleNamespace(new_cdp_session=lambda _page: cdp)

        def evaluate(self, _script):
            return "provider-agent"

    session = BrowserSession(settings)
    session._context = SimpleNamespace(
        pages=[FakePage()],
        close=lambda: None,
    )

    session._apply_capsolver_browser_identity(
        CapSolverResult(
            "private-token",
            user_agent="provider-agent",
            sec_ch_ua='"Chromium";v="140"',
        )
    )

    assert sent == [
        ("Network.enable", None),
        ("Network.setUserAgentOverride", {"userAgent": "provider-agent"}),
        (
            "Network.setExtraHTTPHeaders",
            {"headers": {"sec-ch-ua": '"Chromium";v="140"'}},
        ),
    ]
    assert cdp.detached is False

    session.close()
    assert cdp.detached is True


def test_external_solver_failure_is_sanitized_safety_stop(tmp_path: Path, monkeypatch) -> None:
    settings = load_settings(
        {
            "CBRS_CAPTCHA_SOLVER_MODE": "2captcha",
            "CBRS_2CAPTCHA_API_KEY": "private-key",
        },
        root=tmp_path,
    )
    session = BrowserSession(settings)
    session._context = SimpleNamespace(pages=[SimpleNamespace(url=settings.commerce_url)])

    class FakeSolver:
        def __init__(self, *_args, **_kwargs):
            pass

        def solve_recaptcha_v3_enterprise(self, **_kwargs):
            raise TwoCaptchaError("ERROR_ZERO_BALANCE", "private-key")

    monkeypatch.setattr("cbrs.browser_session.TwoCaptchaClient", FakeSolver)

    with pytest.raises(SafetyStopException) as error:
        session.generate_recaptcha_token("login")

    assert error.value.reason == StopReason.CAPTCHA_SOLVER
    assert "private-key" not in str(error.value)


def test_external_solver_reports_only_definitive_portal_feedback(
    tmp_path: Path, monkeypatch
) -> None:
    settings = replace(
        load_settings(
            {"CBRS_2CAPTCHA_API_KEY": "private-key"},
            root=tmp_path,
        ),
        account_id="a1",
    )
    session = BrowserSession(settings)
    reports: list[tuple[str, int]] = []

    class FakeSolver:
        def __init__(self, *_args, **_kwargs):
            pass

        def report_correct(self, task_id: int) -> None:
            reports.append(("correct", task_id))

        def report_incorrect(self, task_id: int) -> None:
            reports.append(("incorrect", task_id))

    monkeypatch.setattr("cbrs.browser_session.TwoCaptchaClient", FakeSolver)
    solution = RecaptchaSolution(
        token="external-token",
        source="2captcha",
        attempt_id="attempt-1",
        provider_task_id=123,
    )

    session.record_external_recaptcha_outcome(solution, status="indeterminate")
    session.record_external_recaptcha_outcome(solution, status="accepted")
    session.record_external_recaptcha_outcome(solution, status="rejected")

    assert reports == [("correct", 123), ("incorrect", 123)]


def test_manual_solver_uses_browser_first_and_consumes_authorization_only_for_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    settings = replace(
        load_settings(
            {
                "CBRS_CAPTCHA_SOLVER_MODE": "2captcha_manual",
                "CBRS_2CAPTCHA_API_KEY": "private-key",
            },
            root=tmp_path,
        ),
        account_id="a1",
    )
    session = BrowserSession(settings)
    page = SimpleNamespace(
        url=settings.commerce_url,
        evaluate=lambda *_args, **_kwargs: "browser-token",
    )
    session._context = SimpleNamespace(pages=[page])
    session._ensure_recaptcha_ready = lambda: None

    class FakeSolver:
        def __init__(self, *_args, **_kwargs):
            pass

        def solve_recaptcha_v3_enterprise(self, **_kwargs):
            return "external-token"

    monkeypatch.setattr("cbrs.browser_session.TwoCaptchaClient", FakeSolver)
    assert session.has_external_recaptcha_fallback is False
    budget = CaptchaBudgetStore(
        settings.captcha_state_path,
        daily_limit=settings.two_captcha_daily_limit,
        circuit_seconds=settings.two_captcha_circuit_breaker_seconds,
    )
    budget.arm_manual(account_id="a1")
    assert session.has_external_recaptcha_fallback is True
    solution = session.generate_recaptcha_solution("indice_com_texto")
    assert solution.token == "browser-token"
    assert solution.source == "browser"
    assert session.has_external_recaptcha_fallback is True

    solution = session.generate_external_recaptcha_solution("indice_com_texto")
    assert solution.token == "external-token"
    assert solution.source == "2captcha"
    assert solution.attempt_id
    assert session.has_external_recaptcha_fallback is False


def test_automatic_solver_allows_fallback_without_manual_authorization(
    tmp_path: Path, monkeypatch
) -> None:
    settings = replace(
        load_settings(
            {
                "CBRS_CAPTCHA_SOLVER_MODE": "2captcha_manual",
                "CBRS_2CAPTCHA_API_KEY": "private-key",
            },
            root=tmp_path,
        ),
        account_id="a1",
    )
    session = BrowserSession(settings)

    class FakeSolver:
        def __init__(self, *_args, **_kwargs):
            pass

        def solve_recaptcha_v3_enterprise(self, **_kwargs):
            return "external-token"

    monkeypatch.setattr("cbrs.browser_session.TwoCaptchaClient", FakeSolver)
    budget = CaptchaBudgetStore(
        settings.captcha_state_path,
        daily_limit=settings.two_captcha_daily_limit,
        circuit_seconds=settings.two_captcha_circuit_breaker_seconds,
    )
    budget.set_automatic_enabled(True)

    assert session.has_external_recaptcha_fallback is True
    solution = session.generate_external_recaptcha_solution("indice_com_texto")
    assert solution.source == "2captcha"
    assert solution.token == "external-token"
    assert budget.status()["attempts"] == 1


def test_prepare_interactive_login_prefills_without_submitting(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)
    captured: dict[str, object] = {}

    class FakeLocator:
        def __init__(self, name: str) -> None:
            self.name = name
            self.first = self

        def wait_for(self, **kwargs) -> None:
            captured[f"wait_{self.name}"] = kwargs

        def fill(self, value: str) -> None:
            captured[f"fill_{self.name}"] = value

    class FakePage:
        url = "about:blank"

        def goto(self, url: str, **kwargs) -> None:
            captured["goto"] = (url, kwargs)

        def locator(self, selector: str) -> FakeLocator:
            name = "password" if "password" in selector else "email"
            return FakeLocator(name)

    session = BrowserSession(settings)
    session._context = SimpleNamespace(pages=[FakePage()])

    session.prepare_interactive_login("operator@example.test", "private")

    assert captured["goto"][0].endswith("/login")
    assert captured["fill_email"] == "operator@example.test"
    assert captured["fill_password"] == "private"


def test_visible_form_login_submits_and_confirms_session(tmp_path: Path) -> None:
    session = BrowserSession(load_settings({}, root=tmp_path))
    session.open = lambda: session
    captured: dict[str, str] = {}
    session._login_with_form = lambda username, password: captured.update(
        {"username": username, "password": password}
    )
    session.has_active_login = lambda: True

    assert session.login_with_visible_form("operator@example.test", "private") == "browser_form"
    assert captured == {"username": "operator@example.test", "password": "private"}
